"""Coordinate HEMS heat-pump runtime refreshes and diagnostics snapshots."""

from __future__ import annotations

import inspect
import logging
import time
from datetime import datetime, time as dt_time, timedelta
from datetime import timezone as _tz
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_FAST_POLL_INTERVAL,
    HEMS_SUPPORT_PREFLIGHT_CACHE_TTL,
    HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL,
    HEATPUMP_DAILY_CONSUMPTION_FAILURE_BACKOFF_S,
    HEATPUMP_DAILY_CONSUMPTION_STALE_AFTER_S,
    HEATPUMP_POWER_STALE_AFTER_S,
    HEATPUMP_RUNTIME_DIAGNOSTICS_CACHE_TTL,
    HEATPUMP_RUNTIME_STATE_CACHE_TTL,
    HEATPUMP_RUNTIME_STATE_FAILURE_BACKOFF_S,
    HEATPUMP_RUNTIME_STATE_STALE_AFTER_S,
    OPT_FAST_POLL_INTERVAL,
)
from .device_types import normalize_type_key
from .log_redaction import redact_site_id, redact_text, truncate_identifier
from .parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_float,
    coerce_optional_text,
    heatpump_device_state,
    heatpump_member_device_type,
    heatpump_pairing_status,
    heatpump_status_bucket,
    heatpump_status_text,
    parse_inverter_last_report,
    type_member_text,
)
from .runtime_helpers import (
    copy_diagnostics_value,
    redact_battery_payload,
    resolve_site_local_current_date,
    resolve_site_timezone_name,
)
from .state_models import install_state_descriptors

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)

# Heat-pump power is derived from cumulative energy, so watts need a delta window.
_HEATPUMP_IDLE_POWER_MAX_W = 20.0
_HEATPUMP_IDLE_HIGH_DELTA_PENDING = "idle_high_delta_pending_smoothing"
_HEATPUMP_POWER_DEFAULT_WINDOW_S = 300.0
_HEATPUMP_POWER_MIN_DELTA_WH = 0.5
_HEATPUMP_IDLE_SMOOTHING_MIN_WINDOW_S = 15 * 60.0
_HEATPUMP_IDLE_SMOOTHING_MAX_WINDOW_S = 30 * 60.0
_HEATPUMP_ACTIVE_SMOOTHING_MIN_WINDOW_S = 15 * 60.0
_HEATPUMP_ACTIVE_SMOOTHING_MAX_WINDOW_S = 30 * 60.0
_HEATPUMP_POWER_SEEDED_STALE_AFTER_S = 30 * 60.0
_HEATPUMP_POWER_HOLD_STALE_ERRORS = {"seeded_waiting_for_delta", "repeated_sample"}
_HEATPUMP_INACTIVE_RUNTIME_MODES = {"IDLE", "OFF", "STOPPED", "STANDBY"}
_HEATPUMP_INACTIVE_STATUS_BUCKETS = {"error", "not_reporting"}
_SITE_TODAY_HEATPUMP_TOTAL_KEYS = (
    "consumed_wh",
    "consumption_wh",
    "energy_wh",
    "total_wh",
    "wh",
    "consumed",
    "consumption",
    "value",
    "total",
)


def _heatpump_status_is_inactive(value: object) -> bool:
    return heatpump_status_bucket(value) in _HEATPUMP_INACTIVE_STATUS_BUCKETS


class HeatpumpRuntime:
    """HEMS preflight and heat-pump runtime helpers."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self.inventory_state = coordinator.inventory_state
        self.heatpump_state = coordinator.heatpump_state

    @property
    def client(self):
        return self.coordinator.client

    @property
    def site_id(self) -> str:
        return self.coordinator.site_id

    def _type_device_buckets_map(self) -> dict[str, object]:
        """Return current topology buckets from the coordinator when available."""

        buckets = getattr(self.coordinator, "_type_device_buckets", None)
        if not isinstance(buckets, dict):
            buckets = getattr(self, "_type_device_buckets", None)
        return buckets if isinstance(buckets, dict) else {}

    def has_type(self, type_key: object) -> bool:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return False
        bucket = self._type_device_buckets_map().get(normalized)
        if not isinstance(bucket, dict):
            return False
        try:
            return int(bucket.get("count", 0)) > 0
        except Exception:
            return False

    def _heatpump_mark_known_present(self) -> None:
        self._heatpump_known_present = True

    def heatpump_entities_established(self) -> bool:
        if self.has_type("heatpump"):
            return True
        if bool(getattr(self, "_heatpump_known_present", False)):
            return True
        if isinstance(getattr(self, "_heatpump_runtime_state", None), dict):
            return True
        if isinstance(getattr(self, "_heatpump_daily_consumption", None), dict):
            return True
        return self._heatpump_power_w is not None

    def _type_bucket_members(self, type_key: object) -> list[dict[str, object]]:
        normalized = normalize_type_key(type_key)
        if not normalized:
            return []
        bucket = self._type_device_buckets_map().get(normalized)
        if not isinstance(bucket, dict):
            return []
        members = bucket.get("devices")
        if not isinstance(members, list):
            return []
        return [dict(item) for item in members if isinstance(item, dict)]

    def _site_timezone_name(self) -> str:
        return resolve_site_timezone_name(
            getattr(self.coordinator, "_battery_timezone", None)
        )

    def _site_local_current_date(self) -> str:
        return resolve_site_local_current_date(
            getattr(self, "_devices_inventory_payload", None),
            getattr(self.coordinator, "_battery_timezone", None),
        )

    def _copy_diagnostics_value(self, value: object) -> object:
        return copy_diagnostics_value(value)

    def _redact_battery_payload(self, payload: object) -> object:
        return redact_battery_payload(payload)

    def _debug_truncate_identifier(self, value: object) -> str | None:
        return truncate_identifier(value)

    def _heatpump_power_redaction_identifiers(
        self, *extra_identifiers: object
    ) -> tuple[str, ...]:
        identifiers: list[str] = []
        seen: set[str] = set()

        def _add(value: object) -> None:
            text = coerce_optional_text(value)
            if not text or text in seen:
                return
            seen.add(text)
            identifiers.append(text)

        for member in self._type_bucket_members("heatpump"):
            for alias in self._heatpump_member_aliases(member):
                _add(alias)
        for value in extra_identifiers:
            _add(value)
        return tuple(identifiers)

    @staticmethod
    def _heatpump_snapshot_is_fresh(
        last_success_mono: object, stale_after_s: float, now: float
    ) -> bool:
        if not isinstance(last_success_mono, (int, float)):
            return False
        age = now - float(last_success_mono)
        return 0 <= age < stale_after_s

    def _heatpump_mark_runtime_state_stale(self, *, now: float, error: str) -> bool:
        if (
            self._heatpump_runtime_state is not None
            and self._heatpump_snapshot_is_fresh(
                self._heatpump_runtime_state_last_success_mono,
                HEATPUMP_RUNTIME_STATE_STALE_AFTER_S,
                now,
            )
        ):
            self._heatpump_runtime_state_using_stale = True
            self._heatpump_runtime_state_last_error = error
            return True
        self._heatpump_runtime_state = None
        self._heatpump_runtime_state_using_stale = False
        self._heatpump_runtime_state_last_error = error
        return False

    def _heatpump_mark_daily_consumption_stale(self, *, now: float, error: str) -> bool:
        if (
            self._heatpump_daily_consumption is not None
            and self._heatpump_snapshot_is_fresh(
                self._heatpump_daily_consumption_last_success_mono,
                HEATPUMP_DAILY_CONSUMPTION_STALE_AFTER_S,
                now,
            )
        ):
            self._heatpump_daily_consumption_using_stale = True
            self._heatpump_daily_consumption_last_error = error
            return True
        self._heatpump_daily_consumption = None
        self._heatpump_daily_consumption_using_stale = False
        self._heatpump_daily_consumption_last_error = error
        return False

    def _heatpump_mark_daily_split_stale(self, *, now: float, error: str) -> bool:
        if self._heatpump_daily_split_available(
            getattr(self, "_heatpump_daily_consumption", None)
        ) and self._heatpump_snapshot_is_fresh(
            self._heatpump_daily_split_last_success_mono,
            HEATPUMP_DAILY_CONSUMPTION_STALE_AFTER_S,
            now,
        ):
            self._heatpump_daily_split_using_stale = True
            self._heatpump_daily_split_last_error = error
            return True
        self._heatpump_daily_split_using_stale = False
        self._heatpump_daily_split_last_error = error
        return False

    def _heatpump_mark_power_stale(
        self,
        *,
        now: float,
        error: str,
        power_snapshot: dict[str, object] | None = None,
        stale_after_s: float = HEATPUMP_POWER_STALE_AFTER_S,
    ) -> bool:
        if self._heatpump_power_w is not None and self._heatpump_snapshot_is_fresh(
            self._heatpump_power_last_success_mono,
            stale_after_s,
            now,
        ):
            self._heatpump_power_using_stale = True
            self._heatpump_power_last_error = error
            if isinstance(power_snapshot, dict):
                power_snapshot["using_stale"] = True
                power_snapshot["last_success_utc"] = (
                    self._heatpump_power_last_success_utc.isoformat()
                    if isinstance(self._heatpump_power_last_success_utc, datetime)
                    else None
                )
            return True
        self._heatpump_power_w = None
        self._heatpump_power_sample_utc = None
        self._heatpump_power_start_utc = None
        self._heatpump_power_device_uid = None
        self._heatpump_power_source = None
        self._heatpump_power_raw_w = None
        self._heatpump_power_window_seconds = None
        self._heatpump_power_validation = None
        self._heatpump_power_smoothed = False
        self._heatpump_power_using_stale = False
        self._heatpump_power_last_error = error
        return False

    def _heatpump_cleanup_due(self, *attrs: str) -> bool:
        for attr in attrs:
            value = getattr(self, attr, None)
            if isinstance(value, bool):
                if value:
                    return True
                continue
            if attr == "_heatpump_power_sample_history" and isinstance(value, list):
                if value:
                    return True
                continue
            if value is not None:
                return True
        return False

    def hems_refresh_floor_s(self) -> float:
        floor = float(DEFAULT_FAST_POLL_INTERVAL)
        config_entry = getattr(self.coordinator, "config_entry", None)
        options = getattr(config_entry, "options", None)
        if options is None:
            return floor
        try:
            configured = float(options.get(OPT_FAST_POLL_INTERVAL, floor))
        except (AttributeError, TypeError, ValueError):
            return floor
        return max(floor, configured)

    def hems_support_preflight_cache_ttl_s(self) -> float:
        return max(HEMS_SUPPORT_PREFLIGHT_CACHE_TTL, self.hems_refresh_floor_s())

    @staticmethod
    async def _async_call_refreshable_fetcher(
        fetcher,
        *,
        force: bool = False,
        allow_reauth: bool | None = None,
    ) -> object:
        try:
            signature = inspect.signature(fetcher)
        except (TypeError, ValueError):
            signature = None
        kwargs: dict[str, object] = {}
        if signature is not None:
            supports_var_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if force and (
                "refresh_data" in signature.parameters or supports_var_kwargs
            ):
                kwargs["refresh_data"] = True
            if allow_reauth is not None and (
                "allow_reauth" in signature.parameters or supports_var_kwargs
            ):
                kwargs["allow_reauth"] = allow_reauth
        if kwargs:
            return await fetcher(**kwargs)
        return await fetcher()

    async def _async_refresh_hems_support_preflight(
        self, *, force: bool = False
    ) -> None:
        if self.coordinator._skip_hems_polling_due_to_auth_circuit(
            endpoint="hems_support_preflight"
        ):
            return
        if getattr(self.client, "hems_site_supported", None) is not None:
            return

        now = time.monotonic()
        cache_ttl = self.hems_support_preflight_cache_ttl_s()
        if not force and self._hems_support_preflight_cache_until is not None:
            if now < self._hems_support_preflight_cache_until:
                return

        fetcher = getattr(self.client, "system_dashboard_summary", None)
        if not callable(fetcher):
            self._hems_support_preflight_cache_until = now + cache_ttl
            return

        try:
            payload = await self._async_call_refreshable_fetcher(
                fetcher,
                force=force,
                allow_reauth=False,
            )
        except Exception as err:  # noqa: BLE001
            if self.coordinator._note_hems_auth_failure(
                err,
                endpoint="hems_support_preflight",
            ):
                self._hems_support_preflight_cache_until = None
                return
            _LOGGER.debug(
                "HEMS support preflight failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
            self._hems_support_preflight_cache_until = now + cache_ttl
            return

        if isinstance(payload, dict):
            is_hems = coerce_optional_bool(payload.get("is_hems"))
            if is_hems is not None:
                self.client._hems_site_supported = is_hems  # noqa: SLF001

        self._hems_support_preflight_cache_until = now + cache_ttl

    async def async_refresh_hems_support_preflight(
        self, *, force: bool = False
    ) -> None:
        await self._async_refresh_hems_support_preflight(force=force)

    async def async_ensure_heatpump_runtime_diagnostics(
        self, *, force: bool = False
    ) -> None:
        """Capture optional live/detail payloads used by heat-pump runtime views."""

        if not self.has_type("heatpump"):
            # Home Assistant keeps entities registered after discovery, so clear runtime
            # payloads without treating a missing inventory type as an unload event.
            self._heatpump_runtime_diagnostics_cache_until = None
            self._show_livestream_payload = None
            self._heatpump_events_payloads = []
            self._heatpump_runtime_diagnostics_error = None
            self._heatpump_runtime_state = None
            self._heatpump_runtime_state_cache_until = None
            self._heatpump_runtime_state_backoff_until = None
            self._heatpump_runtime_state_last_error = None
            self._heatpump_daily_consumption = None
            self._heatpump_daily_consumption_cache_until = None
            self._heatpump_daily_consumption_backoff_until = None
            self._heatpump_daily_consumption_last_error = None
            self._heatpump_daily_consumption_cache_key = None
            self._heatpump_daily_split_last_error = None
            self._heatpump_daily_split_last_success_mono = None
            self._heatpump_daily_split_last_success_utc = None
            self._heatpump_daily_split_using_stale = False
            self._heatpump_power_snapshot = None
            return

        now = time.monotonic()
        if not force and self._heatpump_runtime_diagnostics_cache_until is not None:
            if now < self._heatpump_runtime_diagnostics_cache_until:
                return

        try:
            await self._async_refresh_heatpump_runtime_state(force=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Heat pump runtime-state diagnostics refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
        try:
            await self._async_refresh_heatpump_daily_consumption(force=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Heat pump daily-consumption diagnostics refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )
        try:
            await self._async_refresh_heatpump_power(force=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Heat pump power diagnostics refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=self._heatpump_power_redaction_identifiers(),
                ),
            )

        self._heatpump_runtime_diagnostics_error = None

        show_livestream = getattr(self.client, "show_livestream", None)
        if callable(show_livestream):
            if self.coordinator._skip_hems_polling_due_to_auth_circuit(
                endpoint="show_livestream"
            ):
                self._show_livestream_payload = None
            else:
                try:
                    payload = await self._async_call_refreshable_fetcher(
                        show_livestream,
                        allow_reauth=False,
                    )
                except Exception as err:  # noqa: BLE001
                    self._show_livestream_payload = None
                    self._heatpump_runtime_diagnostics_error = (
                        "HEMS auth backoff active"
                        if self.coordinator._note_hems_auth_failure(
                            err,
                            endpoint="show_livestream",
                        )
                        else (
                            redact_text(err, site_ids=(self.site_id,))
                            or err.__class__.__name__
                        )
                    )
                else:
                    # Diagnostics may include account-scoped battery data from shared APIs.
                    redacted_payload = self._redact_battery_payload(payload)
                    if isinstance(redacted_payload, dict):
                        self._show_livestream_payload = redacted_payload
                    elif redacted_payload is None:
                        self._show_livestream_payload = None
                    else:
                        self._show_livestream_payload = {"value": redacted_payload}
        elif self.coordinator._skip_hems_polling_due_to_auth_circuit(
            endpoint="show_livestream"
        ):
            self._show_livestream_payload = None

        heat_pump_events_fetcher = getattr(self.client, "heat_pump_events_json", None)
        iq_er_events_fetcher = getattr(self.client, "iq_er_events_json", None)
        if callable(heat_pump_events_fetcher) or callable(iq_er_events_fetcher):
            payloads: list[dict[str, object]] = []
            seen_uids: set[str] = set()
            for member in self._type_bucket_members("heatpump"):
                uid = type_member_text(member, "device_uid", "uid")
                if not uid or uid in seen_uids:
                    continue
                seen_uids.add(uid)
                device_type = heatpump_member_device_type(member)
                payload_entry: dict[str, object] = {
                    "device_uid": uid,
                    "device_type": device_type,
                    "name": type_member_text(member, "name"),
                }

                namespace = "heat_pump" if device_type == "HEAT_PUMP" else "iq_er"
                events_fetcher = (
                    heat_pump_events_fetcher
                    if namespace == "heat_pump"
                    else iq_er_events_fetcher
                )
                payload_entry["events_namespace"] = namespace

                if callable(events_fetcher):
                    endpoint = f"{namespace}_events"
                    if self.coordinator._skip_hems_polling_due_to_auth_circuit(
                        endpoint=endpoint
                    ):
                        payload_entry["error"] = "HEMS auth backoff active"
                    else:
                        try:
                            payload = await events_fetcher(uid)
                        except Exception as err:  # noqa: BLE001
                            if self.coordinator._note_hems_auth_failure(
                                err,
                                endpoint=endpoint,
                            ):
                                payload_entry["error"] = "HEMS auth backoff active"
                            else:
                                payload_entry["error"] = (
                                    redact_text(err, site_ids=(self.site_id,))
                                    or err.__class__.__name__
                                )
                        else:
                            self.coordinator._note_hems_auth_success(endpoint=endpoint)
                            # Event payloads can include opaque device links and
                            # identifiers.
                            redacted_payload = self._redact_battery_payload(payload)
                            if redacted_payload is None:
                                payload_entry["payload"] = None
                            elif isinstance(redacted_payload, (dict, list)):
                                payload_entry["payload"] = redacted_payload
                            else:
                                payload_entry["payload"] = {"value": redacted_payload}
                payloads.append(payload_entry)
            self._heatpump_events_payloads = payloads

        self._heatpump_runtime_diagnostics_cache_until = (
            now + HEATPUMP_RUNTIME_DIAGNOSTICS_CACHE_TTL
        )

    def _heatpump_primary_member(self) -> dict[str, object] | None:
        members = self._type_bucket_members("heatpump")
        for member in members:
            if heatpump_member_device_type(member) == "HEAT_PUMP":
                return member
        if members:
            return members[0]
        return None

    def _heatpump_primary_device_uid(self) -> str | None:
        members = self._type_bucket_members("heatpump")
        if not members:
            return None
        preferred_types = ("HEAT_PUMP", "ENERGY_METER", "SG_READY_GATEWAY")
        for preferred in preferred_types:
            for member in members:
                if heatpump_member_device_type(member) != preferred:
                    continue
                uid = type_member_text(member, "device_uid")
                if uid:
                    return uid
        for member in members:
            uid = type_member_text(member, "device_uid")
            if uid:
                return uid
        return None

    def _heatpump_runtime_device_uid(self) -> str | None:
        for member in self._type_bucket_members("heatpump"):
            if heatpump_member_device_type(member) != "HEAT_PUMP":
                continue
            uid = type_member_text(member, "device_uid")
            if uid:
                self._heatpump_mark_known_present()
                return uid
        snapshot = getattr(self, "_heatpump_runtime_state", None)
        if isinstance(snapshot, dict):
            return coerce_optional_text(snapshot.get("device_uid"))
        return None

    def _heatpump_runtime_member(self) -> dict[str, object] | None:
        runtime_uid = self._heatpump_runtime_device_uid()
        if runtime_uid:
            member = self._heatpump_member_for_uid(runtime_uid)
            if member is not None:
                return member
        primary = self._heatpump_primary_member()
        if primary is not None:
            return primary
        return None

    def _mark_heatpump_runtime_state_auth_backoff(
        self,
        *,
        now: float,
        cache_until: bool = True,
    ) -> None:
        """Mark heat-pump runtime state stale while HEMS auth backoff is active."""

        self._heatpump_mark_runtime_state_stale(
            now=now,
            error="HEMS auth backoff active",
        )
        backoff_until = getattr(self.coordinator, "_hems_auth_backoff_until", None)
        self._heatpump_runtime_state_cache_until = (
            (backoff_until or now + HEATPUMP_RUNTIME_STATE_CACHE_TTL)
            if cache_until
            else None
        )
        self._heatpump_runtime_state_backoff_until = (
            backoff_until or now + HEATPUMP_RUNTIME_STATE_FAILURE_BACKOFF_S
        )

    async def _async_refresh_heatpump_runtime_state(
        self, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            if self.heatpump_entities_established():
                self._heatpump_mark_runtime_state_stale(
                    now=now,
                    error="Heat pump type temporarily missing from inventory",
                )
                self._heatpump_runtime_state_cache_until = (
                    now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
                )
                self._heatpump_runtime_state_backoff_until = None
                return
            self._heatpump_runtime_state = None
            self._heatpump_runtime_state_cache_until = None
            self._heatpump_runtime_state_backoff_until = None
            self._heatpump_runtime_state_last_error = None
            self._heatpump_runtime_state_last_success_mono = None
            self._heatpump_runtime_state_last_success_utc = None
            self._heatpump_runtime_state_using_stale = False
            return
        if (
            not force
            and self._heatpump_runtime_state_cache_until is not None
            and now < self._heatpump_runtime_state_cache_until
        ):
            return
        if (
            not force
            and self._heatpump_runtime_state_backoff_until is not None
            and now < self._heatpump_runtime_state_backoff_until
        ):
            return

        if self.coordinator._skip_hems_polling_due_to_auth_circuit(
            endpoint="hems_heatpump_state"
        ):
            self._mark_heatpump_runtime_state_auth_backoff(now=now)
            return

        await self._async_refresh_hems_support_preflight(force=force)
        if self.coordinator._skip_hems_polling_due_to_auth_circuit(
            endpoint="hems_heatpump_state"
        ):
            self._mark_heatpump_runtime_state_auth_backoff(now=now)
            return
        if getattr(self.client, "hems_site_supported", None) is False:
            # Enphase returns HEMS-only runtime data for supported sites only.
            self._heatpump_mark_runtime_state_stale(
                now=now,
                error="HEMS runtime endpoint unavailable for this site",
            )
            self._heatpump_runtime_state_cache_until = (
                now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
            )
            self._heatpump_runtime_state_backoff_until = None
            return

        device_uid = self._heatpump_runtime_device_uid()
        if not device_uid:
            self._heatpump_mark_runtime_state_stale(
                now=now,
                error="Heat pump runtime device UID temporarily unavailable",
            )
            self._heatpump_runtime_state_cache_until = (
                now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
            )
            self._heatpump_runtime_state_backoff_until = None
            return

        fetcher = getattr(self.client, "hems_heatpump_state", None)
        if not callable(fetcher):
            return

        try:
            payload = await fetcher(
                device_uid=device_uid,
                timezone=self._site_timezone_name(),
            )
        except Exception as err:  # noqa: BLE001
            if self.coordinator._note_hems_auth_failure(
                err,
                endpoint="hems_heatpump_state",
            ):
                self._mark_heatpump_runtime_state_auth_backoff(
                    now=now,
                    cache_until=False,
                )
                return
            error = redact_text(err, site_ids=(self.site_id,)) or err.__class__.__name__
            self._heatpump_mark_runtime_state_stale(now=now, error=error)
            # Backoff preserves recent snapshots while the cloud endpoint is unhealthy.
            self._heatpump_runtime_state_backoff_until = (
                now + HEATPUMP_RUNTIME_STATE_FAILURE_BACKOFF_S
            )
            self._heatpump_runtime_state_cache_until = None
            return

        self._heatpump_runtime_state_cache_until = (
            now + HEATPUMP_RUNTIME_STATE_CACHE_TTL
        )
        self._heatpump_runtime_state_backoff_until = None
        if not isinstance(payload, dict):
            self._heatpump_mark_runtime_state_stale(
                now=now, error="No usable HEMS runtime payload"
            )
            return
        snapshot = dict(payload)
        member = self._heatpump_runtime_member()
        if isinstance(member, dict):
            snapshot.setdefault("member_name", type_member_text(member, "name"))
            snapshot.setdefault(
                "member_device_type", heatpump_member_device_type(member)
            )
            snapshot.setdefault("pairing_status", heatpump_pairing_status(member))
            snapshot.setdefault("device_state", heatpump_device_state(member))
        snapshot["source"] = f"hems_heatpump_state:{device_uid}"
        self._heatpump_runtime_state = snapshot
        self._heatpump_mark_known_present()
        self._heatpump_runtime_state_last_error = None
        self._heatpump_runtime_state_using_stale = False
        self._heatpump_runtime_state_last_success_mono = now
        self._heatpump_runtime_state_last_success_utc = dt_util.utcnow()
        self.coordinator._note_hems_auth_success(endpoint="hems_heatpump_state")

    async def async_refresh_heatpump_runtime_state(
        self, *, force: bool = False
    ) -> None:
        await self._async_refresh_heatpump_runtime_state(force=force)

    def heatpump_runtime_state_refresh_due(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            return self._heatpump_cleanup_due(
                "_heatpump_known_present",
                "_heatpump_runtime_state",
                "_heatpump_runtime_state_cache_until",
                "_heatpump_runtime_state_backoff_until",
                "_heatpump_runtime_state_last_error",
                "_heatpump_runtime_state_last_success_mono",
                "_heatpump_runtime_state_last_success_utc",
                "_heatpump_runtime_state_using_stale",
            )
        if (
            not force
            and self._heatpump_runtime_state_cache_until is not None
            and now < self._heatpump_runtime_state_cache_until
        ):
            return False
        if (
            not force
            and self._heatpump_runtime_state_backoff_until is not None
            and now < self._heatpump_runtime_state_backoff_until
        ):
            return False
        fetcher = getattr(self.client, "hems_heatpump_state", None)
        return callable(fetcher)

    def _heatpump_daily_window(self) -> tuple[str, str, str, tuple[str, str]] | None:
        tz_name = self._site_timezone_name()
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz_name = "UTC"
            tz = _tz.utc
        day_key = self._site_local_current_date()
        try:
            day_date = datetime.fromisoformat(day_key).date()
        except Exception:
            return None
        marker = (day_key, tz_name)
        day_start_local = datetime.combine(day_date, dt_time.min, tzinfo=tz)
        day_end_local = day_start_local + timedelta(days=1) - timedelta(milliseconds=1)
        day_start = day_start_local.astimezone(_tz.utc)
        day_end = day_end_local.astimezone(_tz.utc)
        return (
            day_start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            day_end.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            tz_name,
            marker,
        )

    @staticmethod
    def _sum_optional_values(values: object) -> float | None:
        if not isinstance(values, list):
            return None
        total = 0.0
        found = False
        for item in values:
            if item is None:
                continue
            try:
                numeric = float(item)
            except Exception:
                continue
            if numeric != numeric or numeric in (float("inf"), float("-inf")):
                continue
            total += numeric
            found = True
        return total if found else None

    @staticmethod
    def _first_optional_numeric_value(values: object) -> float | None:
        if not isinstance(values, list):
            return None
        for item in values:
            if item is None:
                continue
            try:
                numeric = float(item)
            except Exception:
                continue
            if numeric != numeric or numeric in (float("inf"), float("-inf")):
                continue
            return numeric
        return None

    def _select_heatpump_energy_consumption_entry(
        self, payload: object
    ) -> tuple[dict[str, object], dict[str, object] | None, dict[str, object]] | None:
        if not isinstance(payload, dict):
            return None
        families = payload.get("data")
        if not isinstance(families, dict):
            return None
        family = families.get("heat-pump")
        if not isinstance(family, list) or not family:
            return None

        preferred_uid = self._heatpump_runtime_device_uid()
        selected: dict[str, object] | None = None
        for entry in family:
            if not isinstance(entry, dict):
                continue
            if preferred_uid and entry.get("device_uid") == preferred_uid:
                selected = entry
                break
            if preferred_uid is None and selected is None:
                selected = entry
        if not isinstance(selected, dict):
            return None

        member = self._heatpump_member_for_uid(selected.get("device_uid"))
        if member is None:
            member = self._heatpump_runtime_member()

        buckets = selected.get("consumption")
        first_bucket = None
        if isinstance(buckets, list):
            for bucket in buckets:
                if isinstance(bucket, dict):
                    first_bucket = bucket
                    break
        if not isinstance(first_bucket, dict):
            return None

        return selected, member, first_bucket

    @staticmethod
    def _heatpump_daily_split_available(snapshot: object) -> bool:
        if not isinstance(snapshot, dict):
            return False
        if snapshot.get("split_source") is not None:
            return True
        for key in (
            "split_device_uid",
            "split_device_name",
            "split_daily_energy_wh",
            "daily_solar_wh",
            "daily_battery_wh",
            "daily_grid_wh",
            "split_endpoint_type",
            "split_endpoint_timestamp",
        ):
            if snapshot.get(key) is not None:
                return True
        details = snapshot.get("details")
        return isinstance(details, list) and bool(details)

    @classmethod
    def _heatpump_clear_daily_split_fields(cls, snapshot: dict[str, object]) -> None:
        snapshot["split_device_uid"] = None
        snapshot["split_device_name"] = None
        snapshot["split_daily_energy_wh"] = None
        snapshot["daily_solar_wh"] = None
        snapshot["daily_battery_wh"] = None
        snapshot["daily_grid_wh"] = None
        snapshot["details"] = []
        snapshot["split_source"] = None
        snapshot["split_endpoint_type"] = None
        snapshot["split_endpoint_timestamp"] = None

    @classmethod
    def _heatpump_copy_daily_split_fields(
        cls, target: dict[str, object], source: object
    ) -> bool:
        if not cls._heatpump_daily_split_available(source):
            cls._heatpump_clear_daily_split_fields(target)
            return False
        assert isinstance(source, dict)
        for key in (
            "split_device_uid",
            "split_device_name",
            "split_daily_energy_wh",
            "daily_solar_wh",
            "daily_battery_wh",
            "daily_grid_wh",
            "split_source",
            "split_endpoint_type",
            "split_endpoint_timestamp",
        ):
            target[key] = source.get(key)
        target["details"] = (
            list(source.get("details"))
            if isinstance(source.get("details"), list)
            else []
        )
        return True

    def _build_heatpump_daily_consumption_snapshot(
        self,
        split_payload: object,
        site_today_payload: object,
    ) -> dict[str, object] | None:
        site_today_total_wh = self._site_today_heatpump_total_wh(site_today_payload)
        if site_today_total_wh is None:
            return None
        site_today_timestamp = None
        site_today_timestamp_source = None
        if isinstance(site_today_payload, dict):
            for key in ("last_report_date", "timestamp", "lastReportDate"):
                if site_today_payload.get(key) is None:
                    continue
                site_today_timestamp = site_today_payload.get(key)
                site_today_timestamp_source = key
                break
        if site_today_timestamp is None:
            site_today_timestamp = dt_util.utcnow().isoformat()
            site_today_timestamp_source = "coordinator_refresh"
        first_stat = None
        if isinstance(site_today_payload, dict):
            stats = site_today_payload.get("stats")
            if isinstance(stats, list):
                first_stat = next(
                    (item for item in stats if isinstance(item, dict)), None
                )
        snapshot: dict[str, object] = {
            "device_uid": None,
            "device_name": None,
            "split_device_uid": None,
            "split_device_name": None,
            "member_name": None,
            "member_device_type": None,
            "pairing_status": None,
            "device_state": None,
            "daily_energy_wh": site_today_total_wh,
            "split_daily_energy_wh": None,
            "daily_solar_wh": None,
            "daily_battery_wh": None,
            "daily_grid_wh": None,
            "details": [],
            "source": "site_today_heatpump",
            "split_source": None,
            "endpoint_type": (
                site_today_payload.get("type")
                if isinstance(site_today_payload, dict)
                else None
            ),
            "endpoint_timestamp": site_today_timestamp,
            "sample_timestamp_source": site_today_timestamp_source,
            "site_interval_seconds": (
                first_stat.get("interval_length")
                if isinstance(first_stat, dict)
                else None
            ),
            "site_start_time": (
                first_stat.get("start_time") if isinstance(first_stat, dict) else None
            ),
            "split_endpoint_type": None,
            "split_endpoint_timestamp": None,
            "sampled_at_utc": (
                parsed.isoformat()
                if (parsed := parse_inverter_last_report(site_today_timestamp))
                is not None
                else None
            ),
        }
        selection = self._select_heatpump_energy_consumption_entry(split_payload)
        if selection is None:
            return snapshot

        selected, member, first_bucket = selection
        daily_solar_wh = coerce_optional_float(first_bucket.get("solar"))
        daily_battery_wh = coerce_optional_float(first_bucket.get("battery"))
        daily_grid_wh = coerce_optional_float(first_bucket.get("grid"))
        split_daily_energy_wh = self._sum_optional_values(first_bucket.get("details"))
        if split_daily_energy_wh is None and all(
            value is not None
            for value in (daily_solar_wh, daily_battery_wh, daily_grid_wh)
        ):
            split_daily_energy_wh = daily_solar_wh + daily_battery_wh + daily_grid_wh

        snapshot.update(
            {
                "split_device_uid": selected.get("device_uid"),
                "split_device_name": selected.get("device_name"),
                "member_name": (
                    type_member_text(member, "name")
                    if isinstance(member, dict)
                    else None
                ),
                "member_device_type": (
                    heatpump_member_device_type(member)
                    if isinstance(member, dict)
                    else None
                ),
                "pairing_status": (
                    heatpump_pairing_status(member)
                    if isinstance(member, dict)
                    else None
                ),
                "device_state": (
                    heatpump_device_state(member) if isinstance(member, dict) else None
                ),
                "member_status": (
                    heatpump_status_text(member) if isinstance(member, dict) else None
                ),
                "split_daily_energy_wh": split_daily_energy_wh,
                "daily_solar_wh": daily_solar_wh,
                "daily_battery_wh": daily_battery_wh,
                "daily_grid_wh": daily_grid_wh,
                "details": (
                    list(first_bucket.get("details"))
                    if isinstance(first_bucket.get("details"), list)
                    else []
                ),
                "split_source": (
                    f"hems_energy_consumption:{selected.get('device_uid')}"
                    if selected.get("device_uid")
                    else "hems_energy_consumption"
                ),
                "split_endpoint_type": (
                    split_payload.get("endpoint_type")
                    if isinstance(split_payload, dict)
                    and split_payload.get("endpoint_type") is not None
                    else (
                        split_payload.get("type")
                        if isinstance(split_payload, dict)
                        else None
                    )
                ),
                "split_endpoint_timestamp": (
                    split_payload.get("endpoint_timestamp")
                    if isinstance(split_payload, dict)
                    and split_payload.get("endpoint_timestamp") is not None
                    else (
                        split_payload.get("timestamp")
                        if isinstance(split_payload, dict)
                        else None
                    )
                ),
            }
        )
        return snapshot

    @classmethod
    def _site_today_heatpump_numeric_total(cls, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, dict):
            for key in _SITE_TODAY_HEATPUMP_TOTAL_KEYS:
                if key not in value:
                    continue
                nested_total = cls._site_today_heatpump_numeric_total(value.get(key))
                if nested_total is not None:
                    return nested_total
            return None
        if isinstance(value, list):
            total = 0.0
            found = False
            for nested in value:
                nested_total = cls._site_today_heatpump_numeric_total(nested)
                if nested_total is None:
                    continue
                total += nested_total
                found = True
            return total if found else None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @classmethod
    def _site_today_heatpump_total_wh(cls, payload: object) -> float | None:
        if not isinstance(payload, dict):
            return None
        stats = payload.get("stats")
        if not isinstance(stats, list) or not stats:
            return None
        first_stat = next((item for item in stats if isinstance(item, dict)), None)
        if not isinstance(first_stat, dict):
            return None
        for key in ("heatpump", "heat_pump", "heat-pump"):
            total = cls._site_today_heatpump_numeric_total(first_stat.get(key))
            if total is not None:
                return total
        return None

    @staticmethod
    def _heatpump_runtime_mode(snapshot: object) -> str | None:
        if not isinstance(snapshot, dict):
            return None
        raw = coerce_optional_text(snapshot.get("heatpump_status"))
        if not raw:
            return None
        return raw.replace("-", "_").replace(" ", "_").upper()

    def _heatpump_power_history(self) -> list[dict[str, object]]:
        history = getattr(self, "_heatpump_power_sample_history", None)
        if isinstance(history, list):
            return history
        self._heatpump_power_sample_history = []
        return self._heatpump_power_sample_history

    @staticmethod
    def _heatpump_power_history_sample_time(entry: object) -> datetime | None:
        if not isinstance(entry, dict):
            return None
        value = entry.get("sample_utc")
        return value if isinstance(value, datetime) else None

    def _record_heatpump_power_history_sample(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return
        energy_wh = coerce_optional_float(snapshot.get("daily_energy_wh"))
        sample_utc = parse_inverter_last_report(snapshot.get("sampled_at_utc"))
        if sample_utc is None:
            sample_utc = parse_inverter_last_report(snapshot.get("endpoint_timestamp"))
        if energy_wh is None or sample_utc is None:
            return
        device_uid = coerce_optional_text(snapshot.get("split_device_uid"))
        day_key = coerce_optional_text(snapshot.get("day_key"))
        timezone_name = coerce_optional_text(snapshot.get("timezone"))
        cutoff = sample_utc - timedelta(seconds=_HEATPUMP_IDLE_SMOOTHING_MAX_WINDOW_S)
        history = self._heatpump_power_history()
        retained: list[dict[str, object]] = []
        for entry in history:
            entry_ts = self._heatpump_power_history_sample_time(entry)
            if entry_ts is None or entry_ts < cutoff:
                continue
            if (
                coerce_optional_text(entry.get("device_uid")) == device_uid
                and coerce_optional_text(entry.get("day_key")) == day_key
                and coerce_optional_text(entry.get("timezone")) == timezone_name
                and entry_ts == sample_utc
            ):
                continue
            retained.append(entry)
        retained.append(
            {
                "device_uid": device_uid,
                "day_key": day_key,
                "timezone": timezone_name,
                "energy_wh": energy_wh,
                "sample_utc": sample_utc,
            }
        )
        retained.sort(
            key=lambda entry: self._heatpump_power_history_sample_time(entry)
            or datetime.min.replace(tzinfo=_tz.utc)
        )
        history[:] = retained

    def _heatpump_smoothed_power_from_history(
        self,
        snapshot: dict[str, object],
        *,
        current_energy_wh: float,
        current_sample_utc: datetime,
        min_window_s: float,
        max_window_s: float,
        validation: str,
        max_power_w: float | None = None,
    ) -> dict[str, object] | None:
        device_uid = coerce_optional_text(snapshot.get("split_device_uid"))
        day_key = coerce_optional_text(snapshot.get("day_key"))
        timezone_name = coerce_optional_text(snapshot.get("timezone"))
        history = self._heatpump_power_history()
        candidates: list[tuple[datetime, float]] = []
        for entry in history:
            sample_utc = self._heatpump_power_history_sample_time(entry)
            if sample_utc is None:
                continue
            age_s = (current_sample_utc - sample_utc).total_seconds()
            if not (min_window_s <= age_s <= max_window_s):
                continue
            if coerce_optional_text(entry.get("device_uid")) != device_uid:
                continue
            if coerce_optional_text(entry.get("day_key")) != day_key:
                continue
            if coerce_optional_text(entry.get("timezone")) != timezone_name:
                continue
            energy_wh = coerce_optional_float(entry.get("energy_wh"))
            if energy_wh is None or energy_wh > current_energy_wh:
                continue
            candidates.append((sample_utc, energy_wh))
        for start_utc, start_energy_wh in sorted(candidates, key=lambda item: item[0]):
            last_energy_wh = start_energy_wh
            monotonic = True
            for entry in history:
                sample_utc = self._heatpump_power_history_sample_time(entry)
                if sample_utc is None or not (
                    start_utc < sample_utc < current_sample_utc
                ):
                    continue
                if coerce_optional_text(entry.get("device_uid")) != device_uid:
                    continue
                if coerce_optional_text(entry.get("day_key")) != day_key:
                    continue
                if coerce_optional_text(entry.get("timezone")) != timezone_name:
                    continue
                energy_wh = coerce_optional_float(entry.get("energy_wh"))
                if energy_wh is None:
                    continue
                if energy_wh < last_energy_wh or energy_wh > current_energy_wh:
                    monotonic = False
                    break
                last_energy_wh = energy_wh
            if not monotonic:
                continue
            window_s = (current_sample_utc - start_utc).total_seconds()
            delta_wh = current_energy_wh - start_energy_wh
            if delta_wh <= _HEATPUMP_POWER_MIN_DELTA_WH:
                continue
            value_w = (delta_wh * 3600.0) / window_s
            if max_power_w is not None and value_w > max_power_w:
                continue
            return {
                "accepted_value_w": value_w,
                "window_seconds": window_s,
                "delta_wh": delta_wh,
                "series_start_utc": start_utc,
                "validation": validation,
            }
        return None

    def _heatpump_idle_smoothed_power(
        self,
        snapshot: dict[str, object],
        *,
        current_energy_wh: float,
        current_sample_utc: datetime,
        raw_value_w: float | None,
        raw_validation: str,
    ) -> dict[str, object] | None:
        if raw_value_w is None:
            return None
        if (
            raw_value_w > _HEATPUMP_IDLE_POWER_MAX_W
            and raw_validation != _HEATPUMP_IDLE_HIGH_DELTA_PENDING
        ):
            return None
        if raw_validation not in {
            "accepted_idle_zero",
            "accepted_idle_repeated_sample",
            "accepted_idle_delta",
            _HEATPUMP_IDLE_HIGH_DELTA_PENDING,
        }:
            return None
        return self._heatpump_smoothed_power_from_history(
            snapshot,
            current_energy_wh=current_energy_wh,
            current_sample_utc=current_sample_utc,
            min_window_s=_HEATPUMP_IDLE_SMOOTHING_MIN_WINDOW_S,
            max_window_s=_HEATPUMP_IDLE_SMOOTHING_MAX_WINDOW_S,
            max_power_w=_HEATPUMP_IDLE_POWER_MAX_W,
            validation="smoothed_idle_delta",
        )

    def _heatpump_active_smoothed_power(
        self,
        snapshot: dict[str, object],
        *,
        current_energy_wh: float,
        current_sample_utc: datetime,
        raw_validation: str,
        raw_window_s: float | None,
    ) -> dict[str, object] | None:
        if raw_validation not in {"accepted_delta", "accepted_zero_delta"}:
            return None
        if raw_validation == "accepted_zero_delta":
            site_interval_s = coerce_optional_float(
                snapshot.get("site_interval_seconds")
            )
            if (
                site_interval_s is None
                or raw_window_s is None
                or raw_window_s >= site_interval_s
            ):
                return None
        return self._heatpump_smoothed_power_from_history(
            snapshot,
            current_energy_wh=current_energy_wh,
            current_sample_utc=current_sample_utc,
            min_window_s=_HEATPUMP_ACTIVE_SMOOTHING_MIN_WINDOW_S,
            max_window_s=_HEATPUMP_ACTIVE_SMOOTHING_MAX_WINDOW_S,
            validation="smoothed_active_delta",
        )

    def _heatpump_power_summary_from_daily_snapshot(
        self,
        snapshot: object,
        *,
        runtime_snapshot: object = None,
    ) -> dict[str, object] | None:
        if not isinstance(snapshot, dict):
            return None
        device_uid = coerce_optional_text(snapshot.get("split_device_uid"))
        day_key = coerce_optional_text(snapshot.get("day_key"))
        runtime_mode = self._heatpump_runtime_mode(runtime_snapshot)
        member = self._heatpump_member_for_uid(device_uid) if device_uid else None
        member_status = (
            heatpump_status_text(member)
            if isinstance(member, dict)
            else coerce_optional_text(snapshot.get("member_status"))
        )
        is_inactive_runtime = (
            runtime_mode in _HEATPUMP_INACTIVE_RUNTIME_MODES
            or _heatpump_status_is_inactive(runtime_mode)
            or _heatpump_status_is_inactive(member_status)
        )
        is_active = not is_inactive_runtime
        current_energy_wh = coerce_optional_float(snapshot.get("daily_energy_wh"))
        current_split_energy_wh = coerce_optional_float(
            snapshot.get("split_daily_energy_wh")
        )
        current_sample_utc = parse_inverter_last_report(snapshot.get("sampled_at_utc"))
        if current_sample_utc is None:
            current_sample_utc = parse_inverter_last_report(
                snapshot.get("endpoint_timestamp")
            )
        previous_snapshot = getattr(self, "_heatpump_daily_consumption_previous", None)
        previous_energy_wh = (
            coerce_optional_float(previous_snapshot.get("daily_energy_wh"))
            if isinstance(previous_snapshot, dict)
            else None
        )
        previous_split_energy_wh = (
            coerce_optional_float(previous_snapshot.get("split_daily_energy_wh"))
            if isinstance(previous_snapshot, dict)
            else None
        )
        previous_day_key = (
            coerce_optional_text(previous_snapshot.get("day_key"))
            if isinstance(previous_snapshot, dict)
            else None
        )
        previous_sample_utc = None
        if isinstance(previous_snapshot, dict):
            previous_sample_utc = parse_inverter_last_report(
                previous_snapshot.get("sampled_at_utc")
            )
            if previous_sample_utc is None:
                previous_sample_utc = parse_inverter_last_report(
                    previous_snapshot.get("endpoint_timestamp")
                )
        accepted_value = None
        rejected = False
        validation = "accepted_delta"
        window_s = None
        delta_wh = None
        series_start_utc = None
        idle_high_delta_pending = False
        power_source = "site_today_heatpump_delta"

        if current_energy_wh is None or current_sample_utc is None:
            if is_inactive_runtime:
                accepted_value = 0.0
                validation = "accepted_idle_without_delta"
            else:
                rejected = True
                validation = "rejected_missing_energy_baseline"
        elif (
            previous_energy_wh is None
            or previous_sample_utc is None
            or previous_day_key != day_key
        ):
            if is_inactive_runtime:
                accepted_value = 0.0
                validation = "accepted_idle_seeded"
            else:
                rejected = True
                validation = "seeded_waiting_for_delta"
        else:
            window_s = (current_sample_utc - previous_sample_utc).total_seconds()
            series_start_utc = previous_sample_utc
            if window_s <= 0:
                if is_inactive_runtime:
                    accepted_value = 0.0
                    validation = "accepted_idle_repeated_sample"
                else:
                    rejected = True
                    validation = "repeated_sample"
            else:
                delta_wh = current_energy_wh - previous_energy_wh
                if delta_wh < 0:
                    if is_inactive_runtime:
                        accepted_value = 0.0
                        validation = "accepted_idle_reset"
                    else:
                        rejected = True
                        validation = "rejected_energy_reset"
                elif delta_wh <= _HEATPUMP_POWER_MIN_DELTA_WH:
                    accepted_value = 0.0
                    validation = (
                        "accepted_idle_zero"
                        if is_inactive_runtime
                        else "accepted_zero_delta"
                    )
                else:
                    effective_window_s = (
                        window_s if window_s > 0 else _HEATPUMP_POWER_DEFAULT_WINDOW_S
                    )
                    candidate_value = (delta_wh * 3600.0) / effective_window_s
                    if (
                        is_inactive_runtime
                        and candidate_value > _HEATPUMP_IDLE_POWER_MAX_W
                    ):
                        accepted_value = candidate_value
                        validation = _HEATPUMP_IDLE_HIGH_DELTA_PENDING
                        idle_high_delta_pending = True
                    else:
                        accepted_value = candidate_value
                        if is_inactive_runtime:
                            validation = "accepted_idle_delta"

        raw_value = accepted_value
        raw_window_s = window_s
        raw_delta_wh = delta_wh
        raw_validation = validation
        display_window_s = window_s
        display_delta_wh = delta_wh
        display_validation = validation
        smoothed = False
        if (
            is_inactive_runtime
            and not rejected
            and current_energy_wh is not None
            and current_sample_utc is not None
        ):
            smoothed_summary = self._heatpump_idle_smoothed_power(
                snapshot,
                current_energy_wh=current_energy_wh,
                current_sample_utc=current_sample_utc,
                raw_value_w=raw_value,
                raw_validation=raw_validation,
            )
            if smoothed_summary is not None:
                accepted_value = coerce_optional_float(
                    smoothed_summary.get("accepted_value_w")
                )
                display_window_s = coerce_optional_float(
                    smoothed_summary.get("window_seconds")
                )
                display_delta_wh = coerce_optional_float(
                    smoothed_summary.get("delta_wh")
                )
                smoothed_start = smoothed_summary.get("series_start_utc")
                if isinstance(smoothed_start, datetime):
                    series_start_utc = smoothed_start
                display_validation = str(smoothed_summary["validation"])
                validation = display_validation
                smoothed = True
            elif idle_high_delta_pending:
                accepted_value = None
                rejected = True
                validation = "rejected_idle_high_delta"
                display_validation = validation
        elif (
            is_active
            and not rejected
            and current_energy_wh is not None
            and current_sample_utc is not None
        ):
            smoothed_summary = self._heatpump_active_smoothed_power(
                snapshot,
                current_energy_wh=current_energy_wh,
                current_sample_utc=current_sample_utc,
                raw_validation=raw_validation,
                raw_window_s=raw_window_s,
            )
            if smoothed_summary is not None:
                accepted_value = coerce_optional_float(
                    smoothed_summary.get("accepted_value_w")
                )
                display_window_s = coerce_optional_float(
                    smoothed_summary.get("window_seconds")
                )
                display_delta_wh = coerce_optional_float(
                    smoothed_summary.get("delta_wh")
                )
                smoothed_start = smoothed_summary.get("series_start_utc")
                if isinstance(smoothed_start, datetime):
                    series_start_utc = smoothed_start
                display_validation = str(smoothed_summary["validation"])
                validation = display_validation
                smoothed = True

        summary: dict[str, object] = {
            "requested_device_ref": (
                self._debug_truncate_identifier(device_uid) if device_uid else None
            ),
            "payload_device_ref": (
                self._debug_truncate_identifier(device_uid) if device_uid else None
            ),
            "resolved_device_ref": (
                self._debug_truncate_identifier(device_uid) if device_uid else None
            ),
            "member_device_type": snapshot.get("member_device_type"),
            "pairing_status": snapshot.get("pairing_status"),
            "device_state": snapshot.get("device_state"),
            "status": member_status,
            "recommended": self._heatpump_power_candidate_is_recommended(device_uid),
            "detail_count": 1 if current_split_energy_wh is not None else 0,
            "reported_detail_value": (
                round(current_split_energy_wh, 3)
                if current_split_energy_wh is not None
                else None
            ),
            "reported_daily_value": (
                round(current_energy_wh, 3) if current_energy_wh is not None else None
            ),
            "accepted_value_w": (
                round(accepted_value, 3) if accepted_value is not None else None
            ),
            "raw_value_w": round(raw_value, 3) if raw_value is not None else None,
            "previous_detail_value": (
                round(previous_split_energy_wh, 3)
                if previous_split_energy_wh is not None
                else None
            ),
            "previous_daily_energy_wh": (
                round(previous_energy_wh, 3) if previous_energy_wh is not None else None
            ),
            "window_seconds": round(window_s, 3) if window_s is not None else None,
            "raw_window_seconds": (
                round(raw_window_s, 3) if raw_window_s is not None else None
            ),
            "power_window_seconds": (
                round(display_window_s, 3) if display_window_s is not None else None
            ),
            "raw_delta_wh": (
                round(raw_delta_wh, 3) if raw_delta_wh is not None else None
            ),
            "power_delta_wh": (
                round(display_delta_wh, 3) if display_delta_wh is not None else None
            ),
            "daily_energy_wh": snapshot.get("daily_energy_wh"),
            "split_daily_energy_wh": snapshot.get("split_daily_energy_wh"),
            "daily_solar_wh": snapshot.get("daily_solar_wh"),
            "daily_battery_wh": snapshot.get("daily_battery_wh"),
            "daily_grid_wh": snapshot.get("daily_grid_wh"),
            "runtime_mode": runtime_mode,
            "validation": validation,
            "raw_validation": raw_validation,
            "power_validation": display_validation,
            "smoothed": smoothed,
            "rejected": rejected,
            "series_start_utc": (
                series_start_utc.isoformat() if series_start_utc is not None else None
            ),
            "endpoint_timestamp": snapshot.get("endpoint_timestamp"),
            "endpoint_type": snapshot.get("endpoint_type"),
            "sample_timestamp_source": snapshot.get("sample_timestamp_source"),
            "site_interval_seconds": snapshot.get("site_interval_seconds"),
            "split_endpoint_timestamp": snapshot.get("split_endpoint_timestamp"),
            "split_endpoint_type": snapshot.get("split_endpoint_type"),
            "source": power_source,
        }
        return summary

    async def _async_refresh_heatpump_daily_consumption(
        self, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            if self.heatpump_entities_established():
                self._heatpump_mark_daily_consumption_stale(
                    now=now,
                    error="Heat pump type temporarily missing from inventory",
                )
                self._heatpump_mark_daily_split_stale(
                    now=now,
                    error="Heat pump type temporarily missing from inventory",
                )
                self._heatpump_daily_consumption_cache_until = (
                    now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL
                )
                self._heatpump_daily_consumption_backoff_until = None
                return
            self._heatpump_daily_consumption = None
            self._heatpump_daily_consumption_cache_until = None
            self._heatpump_daily_consumption_backoff_until = None
            self._heatpump_daily_consumption_last_error = None
            self._heatpump_daily_consumption_cache_key = None
            self._heatpump_daily_consumption_last_success_mono = None
            self._heatpump_daily_consumption_last_success_utc = None
            self._heatpump_daily_consumption_using_stale = False
            self._heatpump_daily_split_last_error = None
            self._heatpump_daily_split_last_success_mono = None
            self._heatpump_daily_split_last_success_utc = None
            self._heatpump_daily_split_using_stale = False
            return

        window = self._heatpump_daily_window()
        if window is None:
            return
        start_at, end_at, tz_name, marker = window
        if (
            not force
            and self._heatpump_daily_consumption_cache_until is not None
            and self._heatpump_daily_consumption_cache_key == marker
            and now < self._heatpump_daily_consumption_cache_until
        ):
            return
        if (
            not force
            and self._heatpump_daily_consumption_backoff_until is not None
            and self._heatpump_daily_consumption_cache_key == marker
            and now < self._heatpump_daily_consumption_backoff_until
        ):
            return

        split_fetcher = getattr(self.client, "hems_energy_consumption", None)
        site_today_fetcher = getattr(self.client, "pv_system_today", None)
        if not callable(site_today_fetcher):
            return

        split_payload: object = None
        split_error: str | None = None
        skip_split_for_auth = self.coordinator._skip_hems_polling_due_to_auth_circuit(
            endpoint="hems_energy_consumption"
        )
        if skip_split_for_auth:
            split_error = "HEMS auth backoff active"
        try:
            site_today_payload = await self._async_call_refreshable_fetcher(
                site_today_fetcher,
                allow_reauth=False,
            )
        except Exception as err:  # noqa: BLE001
            error = (
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=self._heatpump_power_redaction_identifiers(
                        self._heatpump_runtime_device_uid()
                    ),
                )
                or err.__class__.__name__
            )
            self._heatpump_mark_daily_consumption_stale(now=now, error=error)
            self._heatpump_daily_consumption_backoff_until = (
                now + HEATPUMP_DAILY_CONSUMPTION_FAILURE_BACKOFF_S
            )
            self._heatpump_daily_consumption_cache_until = None
            self._heatpump_daily_consumption_cache_key = marker
            return

        if callable(split_fetcher) and not skip_split_for_auth:
            try:
                split_payload = await split_fetcher(
                    start_at=start_at,
                    end_at=end_at,
                    timezone=tz_name,
                    step="P1D",
                )
            except Exception as err:  # noqa: BLE001
                if self.coordinator._note_hems_auth_failure(
                    err,
                    endpoint="hems_energy_consumption",
                ):
                    split_error = "HEMS auth backoff active"
                else:
                    split_error = (
                        redact_text(
                            err,
                            site_ids=(self.site_id,),
                            identifiers=self._heatpump_power_redaction_identifiers(
                                self._heatpump_runtime_device_uid()
                            ),
                        )
                        or err.__class__.__name__
                    )
            else:
                self.coordinator._note_hems_auth_success(
                    endpoint="hems_energy_consumption"
                )
        elif callable(split_fetcher):
            split_error = split_error or "HEMS auth backoff active"
        else:
            split_error = "HEMS daily split endpoint unavailable"

        self._heatpump_daily_consumption_cache_until = (
            now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL
        )
        self._heatpump_daily_consumption_backoff_until = None
        self._heatpump_daily_consumption_cache_key = marker
        if not isinstance(site_today_payload, dict):
            self._heatpump_mark_daily_consumption_stale(
                now=now,
                error="No usable site today heat-pump payload",
            )
            return

        previous_snapshot = getattr(self, "_heatpump_daily_consumption", None)
        snapshot = self._build_heatpump_daily_consumption_snapshot(
            split_payload,
            site_today_payload,
        )
        if snapshot is None:
            self._heatpump_mark_daily_consumption_stale(
                now=now,
                error="No usable site today heat-pump payload",
            )
            return
        if split_error is None and not self._heatpump_daily_split_available(snapshot):
            split_error = "No usable HEMS daily split payload"
        if split_error is None:
            self._heatpump_daily_split_last_error = None
            self._heatpump_daily_split_using_stale = False
            self._heatpump_daily_split_last_success_mono = now
            self._heatpump_daily_split_last_success_utc = dt_util.utcnow()
        else:
            previous_snapshot = getattr(self, "_heatpump_daily_consumption", None)
            same_day = bool(
                isinstance(previous_snapshot, dict)
                and previous_snapshot.get("day_key") == marker[0]
                and previous_snapshot.get("timezone") == marker[1]
            )
            # Site-level totals remain useful when the optional split endpoint
            # fails.
            used_stale_split = same_day and self._heatpump_mark_daily_split_stale(
                now=now,
                error=split_error,
            )
            if used_stale_split:
                self._heatpump_copy_daily_split_fields(snapshot, previous_snapshot)
            else:
                self._heatpump_clear_daily_split_fields(snapshot)
                if not same_day:
                    self._heatpump_daily_split_using_stale = False
                    self._heatpump_daily_split_last_error = split_error
        snapshot["day_key"] = marker[0]
        snapshot["timezone"] = marker[1]
        self._heatpump_daily_consumption_previous = (
            dict(previous_snapshot) if isinstance(previous_snapshot, dict) else None
        )
        self._heatpump_daily_consumption = snapshot
        self._heatpump_mark_known_present()
        self._heatpump_daily_consumption_last_error = None
        self._heatpump_daily_consumption_using_stale = False
        self._heatpump_daily_consumption_last_success_mono = now
        self._heatpump_daily_consumption_last_success_utc = dt_util.utcnow()

    async def async_refresh_heatpump_daily_consumption(
        self, *, force: bool = False
    ) -> None:
        await self._async_refresh_heatpump_daily_consumption(force=force)

    def heatpump_daily_consumption_refresh_due(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            return self._heatpump_cleanup_due(
                "_heatpump_known_present",
                "_heatpump_daily_consumption",
                "_heatpump_daily_consumption_previous",
                "_heatpump_daily_consumption_cache_until",
                "_heatpump_daily_consumption_backoff_until",
                "_heatpump_daily_consumption_last_error",
                "_heatpump_daily_consumption_cache_key",
                "_heatpump_daily_consumption_last_success_mono",
                "_heatpump_daily_consumption_last_success_utc",
                "_heatpump_daily_consumption_using_stale",
                "_heatpump_daily_split_last_error",
                "_heatpump_daily_split_last_success_mono",
                "_heatpump_daily_split_last_success_utc",
                "_heatpump_daily_split_using_stale",
            )
        window = self._heatpump_daily_window()
        if window is None:
            return False
        _start_at, _end_at, _tz_name, marker = window
        if (
            not force
            and self._heatpump_daily_consumption_cache_until is not None
            and self._heatpump_daily_consumption_cache_key == marker
            and now < self._heatpump_daily_consumption_cache_until
        ):
            return False
        if (
            not force
            and self._heatpump_daily_consumption_backoff_until is not None
            and self._heatpump_daily_consumption_cache_key == marker
            and now < self._heatpump_daily_consumption_backoff_until
        ):
            return False
        fetcher = getattr(self.client, "pv_system_today", None)
        return callable(fetcher)

    def _heatpump_member_for_uid(self, uid: object) -> dict[str, object] | None:
        uid_text = coerce_optional_text(uid)
        if not uid_text:
            return None
        for member in self._type_bucket_members("heatpump"):
            for key in ("device_uid", "uid", "serial_number", "serial"):
                member_uid = type_member_text(member, key)
                if member_uid == uid_text:
                    return member
        return None

    @classmethod
    def _heatpump_member_aliases(cls, member: dict[str, object] | None) -> list[str]:
        if not isinstance(member, dict):
            return []
        aliases: list[str] = []
        seen: set[str] = set()
        for key in (
            "device_uid",
            "uid",
            "serial_number",
            "serial",
            "hems_device_id",
            "hems_device_facet_id",
            "device_id",
            "id",
        ):
            alias = type_member_text(member, key)
            if not alias or alias in seen:
                continue
            seen.add(alias)
            aliases.append(alias)
        return aliases

    @classmethod
    def _heatpump_member_primary_id(
        cls, member: dict[str, object] | None
    ) -> str | None:
        aliases = cls._heatpump_member_aliases(member)
        return aliases[0] if aliases else None

    @classmethod
    def _heatpump_member_parent_id(cls, member: dict[str, object] | None) -> str | None:
        if not isinstance(member, dict):
            return None
        return type_member_text(
            member,
            "parent_uid",
            "parentUid",
            "parent_device_uid",
            "parentDeviceUid",
            "parent_id",
            "parentId",
            "parent",
        )

    def _heatpump_member_alias_map(self) -> dict[str, str]:
        alias_map: dict[str, str] = {}
        for member in self._type_bucket_members("heatpump"):
            primary = self._heatpump_member_primary_id(member)
            if not primary:
                continue
            for alias in self._heatpump_member_aliases(member):
                alias_map[alias] = primary
        return alias_map

    def _heatpump_power_candidate_is_recommended(self, uid: str | None) -> bool:
        members = self._type_bucket_members("heatpump")
        if not members:
            return False
        alias_map = self._heatpump_member_alias_map()
        candidate = self._heatpump_member_for_uid(uid) if uid else None
        candidate_primary = alias_map.get(uid, uid) if uid else None
        candidate_parent = self._heatpump_member_parent_id(candidate)
        if candidate_parent:
            candidate_parent = alias_map.get(candidate_parent, candidate_parent)

        recommended_members = [
            member
            for member in members
            if isinstance(heatpump_status_text(member), str)
            and heatpump_status_text(member).casefold() == "recommended"
        ]
        if not recommended_members:
            return False

        any_parent_link = False
        for member in recommended_members:
            member_primary = self._heatpump_member_primary_id(member)
            if member_primary:
                member_primary = alias_map.get(member_primary, member_primary)
            member_parent = self._heatpump_member_parent_id(member)
            if member_parent:
                member_parent = alias_map.get(member_parent, member_parent)
            if member_parent or candidate_parent:
                any_parent_link = True
            if candidate_primary and member_primary == candidate_primary:
                return True
            if candidate_primary and member_parent == candidate_primary:
                return True
            if candidate_parent and member_primary == candidate_parent:
                return True
            if candidate_parent and member_parent == candidate_parent:
                return True

        if not any_parent_link:
            return True
        return False

    def _heatpump_power_debug_candidate_summary(
        self, uid: str | None
    ) -> dict[str, object]:
        member = self._heatpump_member_for_uid(uid) if uid else None
        return {
            "requested_device_ref": (
                self._debug_truncate_identifier(uid) if uid else None
            ),
            "member_device_ref": self._debug_truncate_identifier(
                self._heatpump_member_primary_id(member)
            ),
            "member_parent_ref": self._debug_truncate_identifier(
                self._heatpump_member_parent_id(member)
            ),
            "member_device_type": heatpump_member_device_type(member),
            "pairing_status": heatpump_pairing_status(member),
            "device_state": heatpump_device_state(member),
            "status": heatpump_status_text(member),
            "recommended": self._heatpump_power_candidate_is_recommended(uid),
        }

    async def _async_refresh_heatpump_power(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            if self.heatpump_entities_established():
                self._heatpump_mark_power_stale(
                    now=now,
                    error="Heat pump type temporarily missing from inventory",
                )
                self._heatpump_power_cache_until = (
                    now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL
                )
                self._heatpump_power_backoff_until = None
                return
            self._heatpump_power_w = None
            self._heatpump_power_sample_utc = None
            self._heatpump_power_start_utc = None
            self._heatpump_power_device_uid = None
            self._heatpump_power_source = None
            self._heatpump_power_snapshot = None
            self._heatpump_power_cache_until = None
            self._heatpump_power_backoff_until = None
            self._heatpump_power_last_error = None
            self._heatpump_power_last_success_mono = None
            self._heatpump_power_last_success_utc = None
            self._heatpump_power_raw_w = None
            self._heatpump_power_window_seconds = None
            self._heatpump_power_validation = None
            self._heatpump_power_smoothed = False
            self._heatpump_power_sample_history = []
            self._heatpump_power_using_stale = False
            return
        if not force and self._heatpump_power_cache_until is not None:
            if now < self._heatpump_power_cache_until:
                return
        if not force and self._heatpump_power_backoff_until is not None:
            if now < self._heatpump_power_backoff_until:
                return

        site_date = self._site_local_current_date()
        previous_device_uid = self._heatpump_power_device_uid
        power_snapshot: dict[str, object] = {
            "site_date": site_date,
            "force": force,
            "previous_device_ref": self._debug_truncate_identifier(previous_device_uid),
            "candidates": [
                self._heatpump_power_debug_candidate_summary(
                    self._heatpump_runtime_device_uid()
                )
            ],
            "attempts": [],
            "selected_payload": None,
            "selected_source": None,
            "selected_sample_at_utc": None,
            "last_error": None,
            "outcome": "pending",
            "using_stale": False,
            "last_success_utc": (
                self._heatpump_power_last_success_utc.isoformat()
                if isinstance(self._heatpump_power_last_success_utc, datetime)
                else None
            ),
        }
        self._heatpump_power_snapshot = power_snapshot
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Heat pump power fetch plan for site %s: site_date=%s force=%s previous_device_uid=%s candidates=%s",
                redact_site_id(self.site_id),
                site_date,
                force,
                self._debug_truncate_identifier(previous_device_uid) or "[redacted]",
                power_snapshot["candidates"],
            )

        try:
            await self._async_refresh_heatpump_runtime_state(force=force)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Heat pump power runtime-state refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(err, site_ids=(self.site_id,)),
            )

        try:
            await self._async_refresh_heatpump_daily_consumption(force=force)
        except Exception as err:  # noqa: BLE001
            error = (
                redact_text(
                    err,
                    site_ids=(self.site_id,),
                    identifiers=self._heatpump_power_redaction_identifiers(),
                )
                or err.__class__.__name__
            )
            power_snapshot["last_error"] = error
            power_snapshot["outcome"] = "fetch_error"
            attempts = power_snapshot.setdefault("attempts", [])
            if isinstance(attempts, list):
                attempts.append({"source": "site_today_heatpump", "error": error})
            self._heatpump_mark_power_stale(
                now=now,
                error=error,
                power_snapshot=power_snapshot,
            )
            self._heatpump_power_backoff_until = (
                now + HEATPUMP_DAILY_CONSUMPTION_FAILURE_BACKOFF_S
            )
            self._heatpump_power_cache_until = None
            _LOGGER.debug(
                "Heat pump power daily-consumption refresh failed for site %s: %s",
                redact_site_id(self.site_id),
                error,
            )
            return

        daily_last_error = coerce_optional_text(
            getattr(self, "_heatpump_daily_consumption_last_error", None)
        )
        power_summary = self._heatpump_power_summary_from_daily_snapshot(
            getattr(self, "_heatpump_daily_consumption", None),
            runtime_snapshot=getattr(self, "_heatpump_runtime_state", None),
        )
        if power_summary is not None:
            attempts = power_snapshot.setdefault("attempts", [])
            if isinstance(attempts, list):
                attempts.append(dict(power_summary))
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Heat pump power candidate payload for site %s: %s",
                    redact_site_id(self.site_id),
                    power_summary,
                )

        if power_summary is None:
            error = daily_last_error or "No usable site today heat-pump payload"
            power_snapshot["last_error"] = error
            power_snapshot["outcome"] = "no_usable_payload"
            attempts = power_snapshot.setdefault("attempts", [])
            if isinstance(attempts, list) and error and not attempts:
                attempts.append({"source": "site_today_heatpump", "error": error})
            self._heatpump_mark_power_stale(
                now=now,
                error=error,
                power_snapshot=power_snapshot,
            )
            self._heatpump_power_backoff_until = getattr(
                self, "_heatpump_daily_consumption_backoff_until", None
            )
            self._heatpump_power_cache_until = getattr(
                self, "_heatpump_daily_consumption_cache_until", None
            ) or (now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL)
            _LOGGER.debug(
                "Heat pump power daily-consumption payload unavailable for site %s: %s",
                redact_site_id(self.site_id),
                error,
            )
            return

        if bool(power_summary.get("rejected")):
            error = daily_last_error or str(
                power_summary.get("validation") or "rejected_bad_power_value"
            )
            power_snapshot["last_error"] = error
            power_snapshot["outcome"] = "rejected_value"
            if error in {"rejected_idle_high_delta", "seeded_waiting_for_delta"}:
                self._record_heatpump_power_history_sample(
                    getattr(self, "_heatpump_daily_consumption", None)
                )
            self._heatpump_mark_power_stale(
                now=now,
                error=error,
                power_snapshot=power_snapshot,
                stale_after_s=(
                    _HEATPUMP_POWER_SEEDED_STALE_AFTER_S
                    if error in _HEATPUMP_POWER_HOLD_STALE_ERRORS
                    else HEATPUMP_POWER_STALE_AFTER_S
                ),
            )
            self._heatpump_power_backoff_until = getattr(
                self, "_heatpump_daily_consumption_backoff_until", None
            )
            self._heatpump_power_cache_until = getattr(
                self, "_heatpump_daily_consumption_cache_until", None
            ) or (now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL)
            _LOGGER.debug(
                "Heat pump power rejected daily-consumption payload for site %s: %s",
                redact_site_id(self.site_id),
                power_summary,
            )
            return

        snapshot = getattr(self, "_heatpump_daily_consumption", None)
        device_uid = (
            coerce_optional_text(snapshot.get("split_device_uid"))
            if isinstance(snapshot, dict)
            else None
        )
        endpoint_timestamp = (
            parse_inverter_last_report(snapshot.get("sampled_at_utc"))
            if isinstance(snapshot, dict)
            else None
        )
        if endpoint_timestamp is None and isinstance(snapshot, dict):
            endpoint_timestamp = parse_inverter_last_report(
                snapshot.get("endpoint_timestamp")
            )
        self._heatpump_power_w = float(power_summary.get("accepted_value_w") or 0.0)
        self._heatpump_power_sample_utc = endpoint_timestamp
        self._heatpump_power_start_utc = parse_inverter_last_report(
            power_summary.get("series_start_utc")
        )
        self._heatpump_power_device_uid = device_uid
        self._heatpump_power_source = (
            coerce_optional_text(power_summary.get("source"))
            or "site_today_heatpump_delta"
        )
        self._heatpump_power_raw_w = coerce_optional_float(
            power_summary.get("raw_value_w")
        )
        self._heatpump_power_window_seconds = coerce_optional_float(
            power_summary.get("power_window_seconds")
        )
        self._heatpump_power_validation = coerce_optional_text(
            power_summary.get("power_validation")
        )
        self._heatpump_power_smoothed = bool(power_summary.get("smoothed"))
        self._heatpump_power_cache_until = getattr(
            self, "_heatpump_daily_consumption_cache_until", None
        ) or (now + HEATPUMP_DAILY_CONSUMPTION_CACHE_TTL)
        self._heatpump_power_backoff_until = None
        self._heatpump_power_last_error = daily_last_error
        self._heatpump_power_using_stale = bool(
            getattr(self, "_heatpump_daily_consumption_using_stale", False)
        )
        self._heatpump_power_last_success_mono = getattr(
            self, "_heatpump_daily_consumption_last_success_mono", now
        )
        self._heatpump_power_last_success_utc = getattr(
            self, "_heatpump_daily_consumption_last_success_utc", dt_util.utcnow()
        )
        self._heatpump_mark_known_present()
        self._record_heatpump_power_history_sample(snapshot)
        power_snapshot["selected_payload"] = dict(power_summary)
        power_snapshot["selected_source"] = (
            coerce_optional_text(power_summary.get("source"))
            or "site_today_heatpump_delta"
        )
        power_snapshot["selected_sample_at_utc"] = (
            endpoint_timestamp.isoformat() if endpoint_timestamp is not None else None
        )
        power_snapshot["outcome"] = "selected_sample"
        power_snapshot["using_stale"] = self._heatpump_power_using_stale
        power_snapshot["last_success_utc"] = (
            self._heatpump_power_last_success_utc.isoformat()
            if isinstance(self._heatpump_power_last_success_utc, datetime)
            else None
        )
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Heat pump power selected payload for site %s: %s",
                redact_site_id(self.site_id),
                power_summary,
            )
        return

    async def async_refresh_heatpump_power(self, *, force: bool = False) -> None:
        await self._async_refresh_heatpump_power(force=force)

    def heatpump_power_refresh_due(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not self.has_type("heatpump"):
            return self._heatpump_cleanup_due(
                "_heatpump_known_present",
                "_heatpump_power_w",
                "_heatpump_power_sample_utc",
                "_heatpump_power_start_utc",
                "_heatpump_power_device_uid",
                "_heatpump_power_source",
                "_heatpump_power_raw_w",
                "_heatpump_power_window_seconds",
                "_heatpump_power_validation",
                "_heatpump_power_smoothed",
                "_heatpump_power_sample_history",
                "_heatpump_power_snapshot",
                "_heatpump_power_cache_until",
                "_heatpump_power_backoff_until",
                "_heatpump_power_last_error",
                "_heatpump_power_last_success_mono",
                "_heatpump_power_last_success_utc",
                "_heatpump_power_using_stale",
            )
        if not force and self._heatpump_power_cache_until is not None:
            if now < self._heatpump_power_cache_until:
                return False
        if not force and self._heatpump_power_backoff_until is not None:
            if now < self._heatpump_power_backoff_until:
                return False
        fetcher = getattr(self.client, "pv_system_today", None)
        return callable(fetcher)

    @staticmethod
    def _hems_event_entries(payload: object) -> list[dict[str, object]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("events", "data", "result", "items"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return []

    def _heatpump_event_summary(self) -> dict[str, object]:
        known_event_labels = {
            "hems_sgready_mode_changed_to_2": "sg_ready_normal",
            "hems_sgready_mode_changed_to_3": "sg_ready_recommended",
            "hems_sgready_relay_offline": "sg_ready_relay_offline",
            "hems_energy_meter_offline": "energy_meter_offline",
            "hems_iqer_MQTT_offline": "iq_energy_router_mqtt_offline",
        }
        event_counts: dict[str, int] = {}
        unknown_event_keys: set[str] = set()
        for payload_entry in list(getattr(self, "_heatpump_events_payloads", []) or []):
            payload = (
                payload_entry.get("payload")
                if isinstance(payload_entry, dict)
                else None
            )
            for event in self._hems_event_entries(payload):
                event_key = coerce_optional_text(
                    event.get("event_key")
                    if event.get("event_key") is not None
                    else event.get("eventKey")
                )
                if not event_key:
                    continue
                label = known_event_labels.get(event_key)
                if label is None:
                    unknown_event_keys.add(event_key)
                    continue
                event_counts[label] = event_counts.get(label, 0) + 1
        return {
            "known_event_counts": event_counts,
            "unknown_event_keys": sorted(unknown_event_keys),
        }

    def heatpump_runtime_diagnostics(self) -> dict[str, object]:
        return {
            "runtime_state": self._copy_diagnostics_value(
                getattr(self, "_heatpump_runtime_state", None)
            ),
            "runtime_state_using_stale": bool(
                getattr(self, "_heatpump_runtime_state_using_stale", False)
            ),
            "runtime_state_last_success_utc": (
                getattr(
                    self, "_heatpump_runtime_state_last_success_utc", None
                ).isoformat()
                if isinstance(
                    getattr(self, "_heatpump_runtime_state_last_success_utc", None),
                    datetime,
                )
                else None
            ),
            "runtime_state_last_error": getattr(
                self, "_heatpump_runtime_state_last_error", None
            ),
            "heatpump_known_present": bool(
                getattr(self, "_heatpump_known_present", False)
            ),
            "daily_consumption": self._copy_diagnostics_value(
                getattr(self, "_heatpump_daily_consumption", None)
            ),
            "daily_consumption_using_stale": bool(
                getattr(self, "_heatpump_daily_consumption_using_stale", False)
            ),
            "daily_consumption_last_success_utc": (
                getattr(
                    self, "_heatpump_daily_consumption_last_success_utc", None
                ).isoformat()
                if isinstance(
                    getattr(self, "_heatpump_daily_consumption_last_success_utc", None),
                    datetime,
                )
                else None
            ),
            "daily_consumption_last_error": getattr(
                self, "_heatpump_daily_consumption_last_error", None
            ),
            "daily_split_using_stale": bool(
                getattr(self, "_heatpump_daily_split_using_stale", False)
            ),
            "daily_split_last_success_utc": (
                getattr(
                    self, "_heatpump_daily_split_last_success_utc", None
                ).isoformat()
                if isinstance(
                    getattr(self, "_heatpump_daily_split_last_success_utc", None),
                    datetime,
                )
                else None
            ),
            "daily_split_last_error": getattr(
                self, "_heatpump_daily_split_last_error", None
            ),
            "show_livestream_payload": self._copy_diagnostics_value(
                getattr(self, "_show_livestream_payload", None)
            ),
            "events_payloads": self._copy_diagnostics_value(
                list(getattr(self, "_heatpump_events_payloads", []) or [])
            ),
            "power_snapshot": self._copy_diagnostics_value(
                getattr(self, "_heatpump_power_snapshot", None)
            ),
            "power_using_stale": bool(
                getattr(self, "_heatpump_power_using_stale", False)
            ),
            "power_last_success_utc": (
                getattr(self, "_heatpump_power_last_success_utc", None).isoformat()
                if isinstance(
                    getattr(self, "_heatpump_power_last_success_utc", None), datetime
                )
                else None
            ),
            "event_summary": self._heatpump_event_summary(),
            "last_error": getattr(self, "_heatpump_runtime_diagnostics_error", None),
        }

    @property
    def heatpump_runtime_state(self) -> dict[str, object]:
        value = getattr(self, "_heatpump_runtime_state", None)
        if isinstance(value, dict):
            return dict(value)
        return {}

    @property
    def heatpump_runtime_state_last_error(self) -> str | None:
        value = getattr(self, "_heatpump_runtime_state_last_error", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_runtime_state_using_stale(self) -> bool:
        return bool(getattr(self, "_heatpump_runtime_state_using_stale", False))

    @property
    def heatpump_runtime_state_last_success_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_runtime_state_last_success_utc", None)
        return value if isinstance(value, datetime) else None

    @property
    def heatpump_daily_consumption(self) -> dict[str, object]:
        value = getattr(self, "_heatpump_daily_consumption", None)
        if isinstance(value, dict):
            return dict(value)
        return {}

    @property
    def heatpump_daily_consumption_last_error(self) -> str | None:
        value = getattr(self, "_heatpump_daily_consumption_last_error", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_daily_consumption_using_stale(self) -> bool:
        return bool(getattr(self, "_heatpump_daily_consumption_using_stale", False))

    @property
    def heatpump_daily_consumption_last_success_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_daily_consumption_last_success_utc", None)
        return value if isinstance(value, datetime) else None

    @property
    def heatpump_daily_split_last_error(self) -> str | None:
        value = getattr(self, "_heatpump_daily_split_last_error", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_daily_split_using_stale(self) -> bool:
        return bool(getattr(self, "_heatpump_daily_split_using_stale", False))

    @property
    def heatpump_daily_split_last_success_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_daily_split_last_success_utc", None)
        return value if isinstance(value, datetime) else None

    @property
    def heatpump_power_w(self) -> float | None:
        value = getattr(self, "_heatpump_power_w", None)
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @property
    def heatpump_power_sample_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_power_sample_utc", None)
        if isinstance(value, datetime):
            return value
        return None

    @property
    def heatpump_power_start_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_power_start_utc", None)
        if isinstance(value, datetime):
            return value
        return None

    @property
    def heatpump_power_device_uid(self) -> str | None:
        value = getattr(self, "_heatpump_power_device_uid", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_power_source(self) -> str | None:
        value = getattr(self, "_heatpump_power_source", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_power_raw_w(self) -> float | None:
        value = getattr(self, "_heatpump_power_raw_w", None)
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @property
    def heatpump_power_window_seconds(self) -> float | None:
        value = getattr(self, "_heatpump_power_window_seconds", None)
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @property
    def heatpump_power_validation(self) -> str | None:
        value = getattr(self, "_heatpump_power_validation", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def heatpump_power_smoothed(self) -> bool:
        return bool(getattr(self, "_heatpump_power_smoothed", False))

    @property
    def heatpump_power_using_stale(self) -> bool:
        return bool(getattr(self, "_heatpump_power_using_stale", False))

    @property
    def heatpump_power_last_success_utc(self) -> datetime | None:
        value = getattr(self, "_heatpump_power_last_success_utc", None)
        return value if isinstance(value, datetime) else None

    @property
    def heatpump_power_last_error(self) -> str | None:
        value = getattr(self, "_heatpump_power_last_error", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None


install_state_descriptors(HeatpumpRuntime)
