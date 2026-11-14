"""Opt-in installer Export Limit controls and configuration verification."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import math
import time
from typing import Any, NoReturn, cast
from weakref import WeakValueDictionary

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from homeassistant.util.json import SerializationError
from homeassistant.helpers import issue_registry as ir

from .api import ActivationAccessDenied, Unauthorized
from .api_client.errors import ActivationSessionExpired, EnphaseLoginWallUnauthorized
from .grid_profile_runtime import request_session_reauthentication
from .api_client.export_limit_surface import form_matches_configuration
from .const import (
    DOMAIN,
    OPT_EXPORT_LIMIT_CONTROLS_ENABLED,
    BATTERY_PROFILE_PENDING_TIMEOUT_S,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_SLOW_POLL_INTERVAL,
    OPT_FAST_POLL_INTERVAL,
    OPT_SLOW_POLL_INTERVAL,
)
from .device_types import member_is_retired
from .runtime_helpers import normalize_poll_intervals

OBSERVATION_SECONDS = BATTERY_PROFILE_PENDING_TIMEOUT_S
_ENTRY_LOCKS = f"{DOMAIN}.export_limit.locks"


def fail(key: str) -> NoReturn:
    """Raise a localized error without exposing a cloud response."""
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key=key)


def validate_watts(value: object) -> int:
    """Accept integer watts only, including zero but excluding booleans."""
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 <= value <= 100000
        or not float(value).is_integer()
    ):
        fail("export_limit_invalid")
    return int(value)


def validate_slew_rate(value: object) -> float:
    """Require a finite positive rate with at most two decimal places."""
    if (
        type(value) not in (int, float)
        or not math.isfinite(cast(float, value))
        or cast(float, value) <= 0
        or round(cast(float, value), 2) != value
    ):
        fail("export_limit_invalid_slew")
    return float(value)


@dataclass(frozen=True)
class ExportLimitSnapshot:
    """Identity-bound settings; never expose gateway identity in diagnostics."""

    gateway: str
    enabled: bool
    export: bool
    dynamic: bool
    reference: float
    watts: float
    slew: float

    @property
    def gateway_identity(self) -> str:
        """Identify the gateway without exposing its upstream identifier."""
        return sha256(self.gateway.encode()).hexdigest()

    @property
    def supported(self) -> bool:
        return (
            not self.dynamic
            and self.slew > 0
            and (
                (not self.enabled and self.reference in {1, 2, 3})
                or (
                    self.export
                    and self.reference == 3
                    and self.watts.is_integer()
                    and 0 <= self.watts <= 100000
                )
            )
        )

    @property
    def state(self) -> str:
        if not self.supported:
            return "unsupported"
        if not self.enabled:
            return "disabled"
        return "zero_export" if self.watts == 0 else "limited"


def _form_matches_snapshot(
    fields: list[tuple[str, str]],
    snapshot: ExportLimitSnapshot,
    *,
    allow_zero_slew: bool = False,
) -> bool:
    """Compare the installer form with all supported gateway settings."""
    return form_matches_configuration(
        fields,
        enabled=snapshot.enabled,
        watts=snapshot.watts,
        slew=snapshot.slew,
        export_target=snapshot.export,
        reference=snapshot.reference,
        allow_zero_slew=allow_zero_slew,
    )


def parse_settings(
    payload: object, site_id: str, *, gateway_id: str | None = None
) -> ExportLimitSnapshot | None:
    """Require a single identified gateway; never guess from array ordering."""
    if not isinstance(payload, dict) or payload.get("errors"):
        return None
    data = payload.get("data")
    records = data.get("gateway_settings") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return None
    if gateway_id is not None:
        records = [
            record
            for record in records
            if isinstance(record, dict) and record.get("device_id") == gateway_id
        ]
    if len(records) != 1:
        return None
    record = records[0]
    if (
        not isinstance(record, dict)
        or record.get("site_id") != site_id
        or record.get("device_type") != "ENVOY"
    ):
        return None
    gateway = record.get("device_id")
    settings = record.get("pel_settings_infos")
    if not isinstance(gateway, str) or not gateway or not isinstance(settings, dict):
        return None
    if any(
        type(settings.get(k)) is not bool
        for k in ("enable", "export_limit", "enable_dynamic_limiting")
    ):
        return None
    numbers = [
        settings.get(k) for k in ("reference_value", "free_limit_value", "slew_rate")
    ]
    if any(
        type(v) not in (int, float) or not math.isfinite(cast(float, v))
        for v in numbers
    ):
        return None
    return ExportLimitSnapshot(
        gateway,
        settings["enable"],
        settings["export_limit"],
        settings["enable_dynamic_limiting"],
        *(float(cast(float, v)) for v in numbers),
    )


class ExportLimitIntentStore(Store[dict[str, Any]]):  # type: ignore[misc]
    """Acknowledge atomic intent writes before the runtime changes authority."""

    def __init__(self, hass: Any, version: int, key: str) -> None:
        super().__init__(hass, version, key, atomic_writes=True)
        self._written = False

    async def async_save(self, data: dict[str, Any]) -> None:
        # The runtime lock serializes saves. Drain a cancelled disk write
        # before releasing that lock or trying to clear the record.
        task = asyncio.create_task(self._async_save_checked(data))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _async_save_checked(self, data: dict[str, Any]) -> None:
        self._written = False
        await super().async_save(data)
        if not self._written:
            # Core may have deferred the save until final_write.
            await self._async_handle_write_data()
        if not self._written:
            raise OSError("Export Limit intent write was not acknowledged")

    async def _async_write_data(self, data: dict[str, Any]) -> None:
        try:
            await super()._async_write_data(data)
        except (WriteError, SerializationError) as err:
            # HA Store logs these and returns. Surface the failure to the caller.
            raise OSError("Could not persist Export Limit intent") from err
        self._written = True


class ExportLimitRuntime:
    """Own all write gates and one durable pending request per config entry."""

    verifies_export_limit_gateway = True

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self.snapshot: ExportLimitSnapshot | None = None
        self.last_readback: float | None = None
        self.pending: dict[str, Any] | None = None
        self.request_status = "idle"
        self._loaded = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        entry = coordinator.config_entry
        storage_key = (
            f"{DOMAIN}.export_limit.{getattr(entry, 'entry_id', coordinator.site_id)}"
        )
        # A retiring runtime can still hold a durable reservation while a new
        # runtime starts. Serialize Store access across both lifetimes.
        locks: WeakValueDictionary[str, asyncio.Lock] = (
            coordinator.hass.data.setdefault(_ENTRY_LOCKS, WeakValueDictionary())
        )
        self._lock = locks.setdefault(storage_key, asyncio.Lock())
        # The entry may reload while its old Store still contains unsent intent.
        self._unsent_cleanup_key = f"{storage_key}.unsent_cleanup"
        self._store: Store[dict[str, Any]] = ExportLimitIntentStore(
            coordinator.hass, 1, storage_key
        )

    @property
    def enabled(self) -> bool:
        entry = self.coordinator.config_entry
        return bool(
            entry and entry.options.get(OPT_EXPORT_LIMIT_CONTROLS_ENABLED, False)
        )

    @property
    def stopped(self) -> bool:
        """Distinguish a retiring runtime from a disabled control selector."""
        return self._stopped

    def _require_enabled(self) -> None:
        if (
            not self.enabled
            or self._stopped
            or not getattr(self.coordinator, "runtime_active", True)
        ):
            fail("export_limit_disabled")

    def _publish(self) -> None:
        self._sync_pending_issue()
        self.coordinator.async_update_listeners()

    def _sync_pending_issue(self) -> None:
        entry = self.coordinator.config_entry
        issue_id = f"export_limit_pending_{getattr(entry, 'entry_id', self.coordinator.site_id)}"
        if (
            self.enabled
            and not self._stopped
            and self.pending
            and time.time() - self.pending["started"] >= OBSERVATION_SECONDS
        ):
            ir.async_create_issue(
                self.coordinator.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="export_limit_pending",
                translation_placeholders={
                    "pending_timeout_minutes": str(int(OBSERVATION_SECONDS // 60))
                },
            )
        else:
            ir.async_delete_issue(self.coordinator.hass, DOMAIN, issue_id)

    def _poll_delay(self) -> float:
        entry = self.coordinator.config_entry
        options = entry.options if entry is not None else {}
        fast, slow = normalize_poll_intervals(
            options.get(OPT_FAST_POLL_INTERVAL, DEFAULT_FAST_POLL_INTERVAL),
            options.get(
                OPT_SLOW_POLL_INTERVAL,
                getattr(
                    self.coordinator,
                    "_configured_slow_poll_interval",
                    DEFAULT_SLOW_POLL_INTERVAL,
                ),
            ),
        )
        if self.pending:
            remaining = OBSERVATION_SECONDS - (time.time() - self.pending["started"])
            if remaining > 0:
                return min(float(fast), float(remaining))
        return float(slow)

    async def _wait_for_poll(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except TimeoutError:
            pass
        self._wake.clear()

    def attributes(self) -> dict[str, object]:
        snapshot = self.snapshot
        return {
            "confirmed_watts": (
                int(snapshot.watts)
                if snapshot and snapshot.supported and snapshot.enabled
                else None
            ),
            "requested_watts": self.pending.get("watts") if self.pending else None,
            "requested_action": (
                ("disable" if self.pending["watts"] is None else "set")
                if self.pending
                else None
            ),
            "request_status": self.request_status,
            "pending": self.pending is not None,
            "pending_requested_at": (
                datetime.fromtimestamp(
                    self.pending["started"], timezone.utc
                ).isoformat()
                if self.pending
                else None
            ),
            "slew_rate": snapshot.slew if snapshot else None,
            "requested_slew_rate": self.pending.get("slew") if self.pending else None,
            "last_successful_readback": self.last_readback,
            "gateway_identity": snapshot.gateway_identity if snapshot else None,
            "gateway_identity_verified": snapshot is not None,
        }

    async def _load(self) -> None:
        if self._unsent_cleanup_key in self.coordinator.hass.data:
            try:
                await self._clear_unsent_intent()
            except Exception as err:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="export_limit_unavailable",
                ) from err
        if self._loaded:
            return
        saved = await self._store.async_load()
        if isinstance(saved, dict) and isinstance(saved.get("pending"), dict):
            pending = saved["pending"]
            if (
                "watts" in pending
                and isinstance(pending.get("gateway"), str)
                and bool(pending["gateway"])
                and type(pending.get("started")) in (int, float)
                and math.isfinite(pending["started"])
                and type(pending.get("slew")) in (int, float)
                and math.isfinite(pending["slew"])
                and pending["slew"] > 0
                and (
                    pending.get("watts") is None
                    or (
                        type(pending["watts"]) is int
                        and 0 <= pending["watts"] <= 100000
                    )
                )
            ):
                self.pending = pending
                self.request_status = "unconfirmed"
        self._loaded = True

    async def _save(self) -> None:
        await self._store.async_save({"pending": self.pending})

    async def _clear_unsent_intent(self) -> None:
        """Keep rejected intent blocked until its removal is acknowledged."""
        self.coordinator.hass.data[self._unsent_cleanup_key] = True
        try:
            await self._clear_intent("rejected")
        except BaseException:
            self._publish()
            self._schedule()
            raise

    async def _clear_intent(self, status: str) -> None:
        await self._store.async_save({"pending": None})
        self.pending = None
        self.coordinator.hass.data.pop(self._unsent_cleanup_key, None)
        self.request_status = status
        self._publish()

    async def _retire_rejected_intent(self) -> None:
        """Preserve the provider result while retaining failed cleanup for retry."""
        try:
            await self._clear_unsent_intent()
        except (OSError, asyncio.CancelledError):
            # The shared marker and any pending intent remain until the Store
            # acknowledges the clear, including when a replacement starts.
            pass

    async def _reject_write(self, translation_key: str) -> NoReturn:
        await self._retire_rejected_intent()
        fail(translation_key)

    async def _active_gateway_id(self) -> str | None:
        details = await self.coordinator.client.devices_details("envoy")
        envoys = details.get("envoys") if isinstance(details, dict) else None
        if isinstance(envoys, list) and len(envoys) == 1:
            envoy = envoys[0]
            if (
                isinstance(envoy, dict)
                and not member_is_retired(envoy)
                and isinstance(envoy.get("serial_number"), str)
                and envoy["serial_number"].strip()
                and type(envoy.get("id")) in (int, str)
                and str(envoy["id"]).isdigit()
                and int(envoy["id"]) > 0
            ):
                return str(envoy["id"])
        return None

    async def _read(self, *, require_gateway_identity: bool = False) -> None:
        self._require_enabled()
        try:
            payload = await self.coordinator.client.async_get_export_limit_settings()
        except Exception:
            self.snapshot = None
            self._publish()
            raise
        self.snapshot = None
        # Even a single settings row can belong to a replaced gateway. Polls
        # and installer preparation can settle requests without a write.
        try:
            gateway_id = await self._active_gateway_id()
        except Exception:
            self._publish()
            raise
        if gateway_id is not None:
            self.snapshot = parse_settings(
                payload, str(self.coordinator.site_id), gateway_id=gateway_id
            )
        elif require_gateway_identity:
            self._publish()
            fail("export_limit_unavailable")
        if self.snapshot is not None:
            self.last_readback = time.time()
        snapshot, pending = self.snapshot, self.pending
        if snapshot is not None and pending is not None:
            matches = (
                snapshot.gateway == pending["gateway"]
                and snapshot.slew == pending["slew"]
            )
            if pending["watts"] is None:
                matches &= not snapshot.enabled
            else:
                matches &= (
                    snapshot.supported
                    and snapshot.enabled
                    and snapshot.watts == pending["watts"]
                )
            if matches:
                await self._clear_intent("confirmed")
            elif time.time() - pending["started"] >= OBSERVATION_SECONDS:
                self.request_status = "unconfirmed"
        self._publish()

    async def async_refresh(self) -> dict[str, object]:
        async with self._lock:
            self._require_enabled()
            await self._load()
            if (
                self.pending
                and time.time() - self.pending["started"] >= OBSERVATION_SECONDS
            ):
                self.request_status = "unconfirmed"
                self._publish()
            try:
                await self.coordinator.client.async_prepare_activation_auth()
                await self.coordinator.client.async_get_activation_device_list()
                await self._read()
            except Exception as err:
                if isinstance(
                    err,
                    (
                        ActivationSessionExpired,
                        EnphaseLoginWallUnauthorized,
                        ConfigEntryAuthFailed,
                    ),
                ):
                    request_session_reauthentication(self.coordinator)
                self.snapshot = None
                self._publish()
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key=(
                        "export_limit_session_expired"
                        if isinstance(
                            err,
                            (
                                ActivationSessionExpired,
                                EnphaseLoginWallUnauthorized,
                                ConfigEntryAuthFailed,
                            ),
                        )
                        else "export_limit_unavailable"
                    ),
                ) from err
            self._schedule()
            return self.attributes()

    async def async_prepare(self) -> ExportLimitSnapshot:
        """Check installer and PEL form access without changing configuration."""
        self._require_enabled()
        try:
            await self.async_refresh()
            await self.coordinator.client.async_get_export_limit_form()
        except ServiceValidationError:
            raise
        except Exception as err:
            if isinstance(
                err,
                (
                    ActivationSessionExpired,
                    EnphaseLoginWallUnauthorized,
                    ConfigEntryAuthFailed,
                ),
            ):
                self.snapshot = None
                self._publish()
                request_session_reauthentication(self.coordinator)
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=(
                    "export_limit_session_expired"
                    if isinstance(
                        err,
                        (
                            ActivationSessionExpired,
                            EnphaseLoginWallUnauthorized,
                            ConfigEntryAuthFailed,
                        ),
                    )
                    else "export_limit_unavailable"
                ),
            ) from err
        if self.snapshot is None:
            fail("export_limit_unavailable")
        assert self.snapshot is not None
        return self.snapshot

    async def async_apply_guarded(
        self,
        watts: int | None,
        *,
        expected: ExportLimitSnapshot,
        before_write: Callable[[], None],
    ) -> dict[str, object]:
        """Apply a prepared setting only while the caller still authorizes it.

        The synchronous guard may raise ServiceValidationError to revoke the
        write. Run it with the provider lock held after persisting intent.
        """
        return await self.async_apply(
            watts, confirm=True, expected=expected, before_write=before_write
        )

    async def async_apply(
        self,
        watts: int | None,
        *,
        confirm: bool,
        expected: ExportLimitSnapshot | None = None,
        expected_gateway: str | None = None,
        slew_rate: float | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        if confirm is not True:
            fail("export_limit_confirmation")
        if watts is not None:
            watts = validate_watts(watts)
        if slew_rate is not None:
            slew_rate = validate_slew_rate(slew_rate)
        async with self._lock:
            self._require_enabled()
            await self._load()
            try:
                await self.coordinator.client.async_prepare_activation_auth()
                await self.coordinator.client.async_get_activation_device_list()
                await self._read(
                    require_gateway_identity=(
                        expected is not None or expected_gateway is not None
                    )
                )
                if self.pending is not None:
                    fail("export_limit_pending")
                # Acquire the token after any read-side session renewal.
                fields = await self.coordinator.client.async_get_export_limit_form()
                snapshot = self.snapshot
                if snapshot is None or not snapshot.supported:
                    fail("export_limit_unavailable")
                assert snapshot is not None
                if (expected is not None and snapshot != expected) or (
                    expected_gateway is not None
                    and snapshot.gateway_identity != expected_gateway
                ):
                    fail("export_limit_changed")
                requested_slew = snapshot.slew if slew_rate is None else slew_rate
                if requested_slew == snapshot.slew and (
                    (watts is None and not snapshot.enabled)
                    or (
                        watts is not None
                        and snapshot.enabled
                        and snapshot.watts == watts
                    )
                ):
                    return self.attributes()
                if not _form_matches_snapshot(fields, snapshot):
                    # Enphase can return a zero form default while the gateway has
                    # a positive slew rate. Preserve that rate only when every other
                    # form setting matches, then verify identity and settings again.
                    if requested_slew != snapshot.slew or not _form_matches_snapshot(
                        fields, snapshot, allow_zero_slew=True
                    ):
                        fail("export_limit_mismatch")
                    await self._read(require_gateway_identity=True)
                    if self.snapshot != snapshot:
                        fail("export_limit_changed")
                    # The read may renew the session, so acquire a fresh form token.
                    fields = await self.coordinator.client.async_get_export_limit_form()
                    if not _form_matches_snapshot(
                        fields, snapshot, allow_zero_slew=True
                    ):
                        fail("export_limit_mismatch")
            except ServiceValidationError:
                raise
            except Exception as err:
                session_expired = isinstance(
                    err,
                    (
                        ActivationSessionExpired,
                        EnphaseLoginWallUnauthorized,
                        ConfigEntryAuthFailed,
                    ),
                )
                if session_expired:
                    self.snapshot = None
                    self._publish()
                    request_session_reauthentication(self.coordinator)
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key=(
                        "export_limit_session_expired"
                        if session_expired
                        else "export_limit_unavailable"
                    ),
                ) from err
            self.pending = {
                "gateway": snapshot.gateway,
                "watts": watts,
                "slew": requested_slew,
                "started": time.time(),
            }
            self.request_status = "pending"
            try:
                await self._save()
            except asyncio.CancelledError:
                await self._clear_unsent_intent()
                raise
            except Exception as err:
                # Nothing has been sent when durable intent cannot be recorded.
                await self._retire_rejected_intent()
                if before_write is not None:
                    raise
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="export_limit_unavailable",
                ) from err
            self._publish()
            dispatched = False

            def authorize() -> None:
                nonlocal dispatched
                self._require_enabled()
                if before_write is not None:
                    before_write()
                dispatched = True

            try:
                self._require_enabled()
                if expected_gateway is not None or before_write is not None:
                    # The provider's form targets the site. Recheck inventory
                    # after the form and Store awaits, just before sending it.
                    try:
                        gateway_id = await self._active_gateway_id()
                    except Exception as err:
                        self.snapshot = None
                        raise ServiceValidationError(
                            translation_domain=DOMAIN,
                            translation_key="export_limit_unavailable",
                        ) from err
                    if gateway_id != snapshot.gateway:
                        self.snapshot = None
                        fail("export_limit_changed")
                    await self.coordinator.client.async_set_export_limit(
                        fields, watts, requested_slew, before_write=authorize
                    )
                else:
                    dispatched = True
                    await self.coordinator.client.async_set_export_limit(
                        fields, watts, requested_slew
                    )
            except asyncio.CancelledError:
                if dispatched:
                    self.request_status = "unconfirmed"
                else:
                    await self._clear_unsent_intent()
                raise
            except ServiceValidationError:
                if dispatched:
                    self.request_status = "unconfirmed"
                else:
                    await self._retire_rejected_intent()
                raise
            except (Unauthorized, ActivationAccessDenied) as err:
                session_expired = isinstance(
                    err, (ActivationSessionExpired, EnphaseLoginWallUnauthorized)
                )
                if session_expired:
                    self.snapshot = None
                    self._publish()
                    request_session_reauthentication(self.coordinator)
                await self._reject_write(
                    "export_limit_session_expired"
                    if session_expired
                    else "export_limit_unavailable"
                )
            except aiohttp.ClientResponseError as err:
                if err.status in (400, 403, 422):
                    await self._reject_write("export_limit_unavailable")
                self.request_status = "unconfirmed"
            except Exception:
                if not dispatched:
                    await self._retire_rejected_intent()
                    raise
                self.request_status = "unconfirmed"
            finally:
                self._publish()
                if self._task is not None:
                    self._wake.set()
                self._schedule()
            try:
                await self._read()
            except Exception:
                self.request_status = "unconfirmed"
                self._publish()
            return self.attributes()

    def _schedule(self) -> None:
        if self.enabled and not self._stopped and self._task is None:
            self._task = self.coordinator.hass.async_create_background_task(
                self._poll(), "enphase_export_limit"
            )

    async def async_start(self) -> None:
        self._stopped = False
        self._sync_pending_issue()
        if self.enabled:
            try:
                await self.async_refresh()
            except ServiceValidationError:
                self._schedule()

    async def _poll(self) -> None:
        try:
            while self.enabled and not self._stopped:
                await self._wait_for_poll(self._poll_delay())
                try:
                    await self.async_refresh()
                except ServiceValidationError:
                    pass
        finally:
            self._task = None

    async def async_wait_for_intent_transactions(self) -> None:
        """Drain operations on the shared file before replacing this runtime."""
        async with self._lock:
            if self._unsent_cleanup_key in self.coordinator.hass.data:
                await self._retire_rejected_intent()

    def stop(self) -> asyncio.Task[None] | None:
        self._stopped = True
        self._sync_pending_issue()
        task = self._task
        if task is not None:
            task.cancel()
        return task
