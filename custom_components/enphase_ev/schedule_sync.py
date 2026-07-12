"""Synchronize Enphase EVSE schedules into Home Assistant schedule helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable
from datetime import datetime, time as dt_time, timedelta
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Protocol, TypeVar, cast

from homeassistant.components import websocket_api
from homeassistant.components.schedule.const import (
    CONF_ALL_DAYS,
    CONF_FROM,
    CONF_TO,
    DOMAIN as SCHEDULE_DOMAIN,
)
from homeassistant.core import HomeAssistant, callback as ha_callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_interval,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .api import EnphaseLoginWallUnauthorized, SchedulerUnavailable
from .const import (
    DEFAULT_SCHEDULE_SYNC_ENABLED,
    DOMAIN,
    OPT_SCHEDULE_SYNC_ENABLED,
)
from .log_redaction import redact_identifier, redact_text
from .request_metrics import request_metrics_scope
from .schedule import normalize_slot_payload

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator
    from .runtime_data import EnphaseConfigEntry

_LOGGER = logging.getLogger(__name__)

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])


def callback(func: _CallbackT) -> _CallbackT:
    """Apply Home Assistant's callback marker with its identity type preserved."""

    return cast(_CallbackT, ha_callback(func))


class ScheduleStorageCollection(Protocol):
    """Storage operations used by the schedule sync cleanup path."""

    data: dict[str, Any]

    async def async_delete_item(self, item_id: str) -> None: ...


SYNC_INTERVAL = timedelta(minutes=5)
SYNC_REFRESH_CONCURRENCY = 3
# Scheduler writes often return before reads reflect the new slot state.
PATCH_REFRESH_DELAY_S = 1.0
SCHEDULE_SYNC_STOP_TIMEOUT_S = 10.0
SYNC_CAPTURE_ERRORS = (RuntimeError, TypeError, ValueError, AttributeError)


class ScheduleSync:
    """Mirror Enphase scheduler slots into Home Assistant helper entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EnphaseCoordinator,
        config_entry: EnphaseConfigEntry | None = None,
    ) -> None:
        self.hass = hass
        self._coordinator = coordinator
        self._config_entry = config_entry
        self._slot_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._meta_cache: dict[str, str | None] = {}
        self._config_cache: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._storage_collection: ScheduleStorageCollection | None = None
        self._unsub_interval: Callable[[], None] | None = None
        self._unsub_coordinator: Callable[[], None] | None = None
        self._listeners: list[Callable[[], None]] = []
        self._disabled_cleanup_done = False
        self._storage_sanitize_done = False
        self._last_sync: datetime | None = None
        self._last_error: str | None = None
        self._last_status: str | None = None
        self._pending_patch_refresh: set[str] = set()
        self._pending_patch_refresh_cancels: dict[str, Callable[[], None]] = {}
        self._pending_patch_refresh_tasks: dict[str, asyncio.Future[None]] = {}
        self._refresh_tasks: set[asyncio.Future[None]] = set()
        self._stopping = False

    async def async_start(self) -> None:
        self._disabled_cleanup_done = False
        if not self._sync_enabled():
            await self._disable_support()
            return
        await self._remove_all_helpers()
        if self._stopping:
            return
        self._unsub_interval = async_track_time_interval(
            self.hass, self._handle_interval, SYNC_INTERVAL
        )
        try:
            self._unsub_coordinator = self._coordinator.async_add_listener(
                self._handle_coordinator_update
            )
        except Exception:
            self._unsub_coordinator = None
        await self.async_refresh(reason="startup")

    async def async_stop(self) -> None:
        self._stopping = True
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        if self._unsub_coordinator is not None:
            self._unsub_coordinator()
            self._unsub_coordinator = None
        for cancel in self._pending_patch_refresh_cancels.values():
            cancel()
        self._pending_patch_refresh_cancels.clear()
        tasks = tuple(
            {
                *self._pending_patch_refresh_tasks.values(),
                *self._refresh_tasks,
            }
        )
        for task in tasks:
            task.cancel()
        self._pending_patch_refresh_tasks.clear()
        self._refresh_tasks.clear()
        self._pending_patch_refresh.clear()
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=SCHEDULE_SYNC_STOP_TIMEOUT_S,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            if pending:
                _LOGGER.warning(
                    "Timed out waiting for %s Enphase schedule task(s) to stop",
                    len(pending),
                )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": self._sync_enabled(),
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
            "last_status": self._last_status,
            "last_error": self._last_error,
            "cached_serials": sorted(self._slot_cache.keys()),
        }

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def _unsub() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsub

    @callback
    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - keep other listeners alive
                _LOGGER.exception("Schedule sync listener error")

    def get_slot(self, sn: str, slot_id: str) -> dict[str, Any] | None:
        return self._slot_cache.get(sn, {}).get(slot_id)

    def iter_slots(self) -> Iterable[tuple[str, str, dict[str, Any]]]:
        for serial, slots in self._slot_cache.items():
            for slot_id, slot in slots.items():
                yield serial, slot_id, slot

    def is_off_peak_eligible(self, sn: str) -> bool:
        config = self._config_cache.get(sn)
        if not isinstance(config, dict):
            return True
        eligible = config.get("isOffPeakEligible")
        if eligible is None:
            return True
        return bool(eligible)

    @callback
    def _schedule_post_patch_refresh(self, sn: str) -> None:
        if sn in self._pending_patch_refresh:
            return
        self._pending_patch_refresh.add(sn)

        @callback
        def _run(_now: datetime) -> None:
            self._pending_patch_refresh_cancels.pop(sn, None)
            coro = self.async_refresh(reason="patch", serials=[sn])
            try:
                task = self.hass.async_create_task(
                    coro,
                    name=f"{DOMAIN}_schedule_patch_refresh_{redact_identifier(sn)}",
                )
            except TypeError:
                coro.close()
                task = self.hass.async_create_task(
                    self.async_refresh(reason="patch", serials=[sn])
                )
            if task is None:
                self._pending_patch_refresh.discard(sn)
                return
            self._pending_patch_refresh_tasks[sn] = task

            def _clear(completed: asyncio.Future[None]) -> None:
                if self._pending_patch_refresh_tasks.get(sn) is completed:
                    self._pending_patch_refresh_tasks.pop(sn, None)
                    self._pending_patch_refresh.discard(sn)

            task.add_done_callback(_clear)

        self._pending_patch_refresh_cancels[sn] = async_call_later(
            self.hass, PATCH_REFRESH_DELAY_S, _run
        )

    def _scheduler_backoff_active(self) -> bool:
        backoff_active = getattr(self._coordinator, "scheduler_backoff_active", None)
        if not callable(backoff_active):
            return False
        try:
            return bool(backoff_active())
        except SYNC_CAPTURE_ERRORS:
            return False

    def _mark_scheduler_available(self) -> None:
        mark_available = getattr(self._coordinator, "mark_scheduler_available", None)
        if callable(mark_available):
            mark_available()

    def _note_scheduler_unavailable(self, err: Exception) -> None:
        note_unavailable = getattr(
            self._coordinator, "note_scheduler_unavailable", None
        )
        if callable(note_unavailable):
            note_unavailable(err)

    def _note_login_wall_unauthorized(self, err: EnphaseLoginWallUnauthorized) -> None:
        """Let the coordinator activate its auth-block path from scheduler calls."""

        self._last_error = redact_text(
            err,
            site_ids=(getattr(self._coordinator, "site_id", None),),
        )
        self._last_status = "auth_failed"
        activate = getattr(
            self._coordinator, "_activate_auth_block_from_login_wall", None
        )
        if callable(activate) and activate(err):
            self._last_status = "auth_blocked"

    async def _disable_support(self) -> None:
        if self._disabled_cleanup_done:
            return
        self._disabled_cleanup_done = True
        await self.async_stop()
        await self._remove_all_helpers()
        self._slot_cache.clear()
        self._meta_cache.clear()
        self._config_cache.clear()
        self._last_status = "disabled"
        self._notify_listeners()

    async def _remove_all_helpers(self) -> None:
        collection = await self._ensure_storage_collection()
        ent_reg = er.async_get(self.hass)
        # Remove both entity-registry and storage-collection records because
        # schedule helpers persist independently of the integration entities.
        slot_keys: set[tuple[str, str]] = {
            (serial, slot_id)
            for serial, slots in self._slot_cache.items()
            for slot_id in slots
            if serial and slot_id
        }

        for entry in list(ent_reg.entities.values()):
            entry_domain = getattr(entry, "domain", None)
            if entry_domain is None:
                entry_domain = entry.entity_id.partition(".")[0]
            if entry_domain != SCHEDULE_DOMAIN:
                continue
            unique_id = getattr(entry, "unique_id", "") or ""
            if not unique_id.startswith(f"{DOMAIN}:"):
                continue
            entry_config_id = getattr(entry, "config_entry_id", None)
            if (
                self._config_entry is not None
                and entry_config_id is not None
                and entry_config_id != self._config_entry.entry_id
            ):
                continue
            serial, slot_id = self._parse_slot_id(unique_id)
            if serial and slot_id:
                slot_keys.add((serial, slot_id))
            ent_reg.async_remove(entry.entity_id)

        if collection is not None:
            for item_id in list(collection.data):
                if not isinstance(item_id, str):
                    continue
                if not item_id.startswith(f"{DOMAIN}:"):
                    continue
                serial, slot_id = self._parse_slot_id(item_id)
                if serial and slot_id:
                    slot_keys.add((serial, slot_id))
                await collection.async_delete_item(item_id)
            await self.hass.async_block_till_done()

        for serial, slot_id in slot_keys:
            switch_unique_id = f"{DOMAIN}:{serial}:schedule:{slot_id}:enabled"
            switch_entity_id = ent_reg.async_get_entity_id(
                "switch", DOMAIN, switch_unique_id
            )
            if switch_entity_id:
                ent_reg.async_remove(switch_entity_id)

        known_serials: set[str] = set()
        serial_provider = getattr(self._coordinator, "iter_serials", None)
        if callable(serial_provider):
            try:
                known_serials = {str(sn) for sn in serial_provider() if sn}
            except Exception:
                known_serials = set()
        if not known_serials:
            serials = getattr(self._coordinator, "serials", None)
            if isinstance(serials, (list, set, tuple)):
                known_serials = {str(sn) for sn in serials if sn}

        for entry in list(ent_reg.entities.values()):
            entry_domain = getattr(entry, "domain", None)
            if entry_domain is None:
                entry_domain = entry.entity_id.partition(".")[0]
            if entry_domain != "switch":
                continue
            entry_platform = getattr(entry, "platform", None)
            if entry_platform is not None and entry_platform != DOMAIN:
                continue
            unique_id = entry.unique_id or ""
            if (
                not unique_id.startswith(f"{DOMAIN}:")
                or ":schedule:" not in unique_id
                or not unique_id.endswith(":enabled")
            ):
                continue
            entry_config_id = getattr(entry, "config_entry_id", None)
            if (
                self._config_entry is not None
                and entry_config_id is not None
                and entry_config_id != self._config_entry.entry_id
            ):
                continue
            base_unique_id = unique_id[: -len(":enabled")]
            serial, slot_id = self._parse_slot_id(base_unique_id)
            if serial is None or slot_id is None:
                continue
            if known_serials and serial not in known_serials:
                continue
            ent_reg.async_remove(entry.entity_id)

    @staticmethod
    def _parse_slot_id(unique_id: str) -> tuple[str | None, str | None]:
        prefix = f"{DOMAIN}:"
        if not unique_id.startswith(prefix):
            return None, None
        rest = unique_id[len(prefix) :]
        serial, sep, slot_id = rest.partition(":schedule:")
        if not sep or not serial or not slot_id:
            return None, None
        return serial, slot_id

    async def async_set_slot_enabled(
        self, sn: str, slot_id: str, enabled: bool
    ) -> None:
        if not self._sync_enabled():
            return
        if self._scheduler_backoff_active():
            self._last_status = "scheduler_unavailable"
            self._last_error = getattr(self._coordinator, "scheduler_last_error", None)
            return
        slot = self._slot_cache.get(sn, {}).get(slot_id)
        if not slot:
            return
        schedule_type = str(slot.get("scheduleType") or "")
        if schedule_type == "OFF_PEAK" and not self.is_off_peak_eligible(sn):
            _LOGGER.debug(
                "Skipping OFF_PEAK toggle for %s: not eligible for off-peak schedules",
                redact_identifier(sn),
            )
            return
        slot_states: dict[str, bool] = {}
        for cached_id, cached_slot in self._slot_cache.get(sn, {}).items():
            desired = bool(cached_slot.get("enabled", True))
            if cached_id == slot_id:
                desired = bool(enabled)
            slot_states[str(cached_id)] = desired
        if not slot_states:
            return
        try:
            response = await self._coordinator.client.patch_schedule_states(
                sn, slot_states=slot_states
            )
            self._mark_scheduler_available()
        except EnphaseLoginWallUnauthorized as err:
            self._note_login_wall_unauthorized(err)
            return
        except SchedulerUnavailable as err:
            self._last_error = redact_text(
                err,
                site_ids=(getattr(self._coordinator, "site_id", None),),
                identifiers=(sn,),
            )
            self._last_status = "scheduler_unavailable"
            self._note_scheduler_unavailable(err)
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Schedule state PATCH failed for %s: %s",
                redact_identifier(sn),
                redact_text(
                    err,
                    site_ids=(getattr(self._coordinator, "site_id", None),),
                    identifiers=(sn,),
                ),
            )
            return
        for cached_id, desired in slot_states.items():
            updated_slot = self._slot_cache.get(sn, {}).get(cached_id)
            if updated_slot is not None:
                updated_slot["enabled"] = bool(desired)
        needs_refresh = True
        if isinstance(response, dict):
            meta = response.get("meta")
            if isinstance(meta, dict) and meta.get("serverTimeStamp"):
                self._meta_cache[sn] = meta.get("serverTimeStamp")
            data = response.get("data")
            if isinstance(data, dict):
                config = data.get("config")
                if isinstance(config, dict):
                    self._config_cache[sn] = config
                slots = data.get("slots")
                if isinstance(slots, list):
                    slot_map: dict[str, dict[str, Any]] = {}
                    for slot_item in slots:
                        if not isinstance(slot_item, dict):
                            continue
                        cached_slot_id = str(slot_item.get("id") or "")
                        if not cached_slot_id:
                            continue
                        slot_map[cached_slot_id] = slot_item
                    if slot_map:
                        self._slot_cache[sn] = slot_map
                        needs_refresh = False
            elif isinstance(data, list):
                slot_map = {}
                for slot_item in data:
                    if not isinstance(slot_item, dict):
                        continue
                    cached_slot_id = str(slot_item.get("id") or "")
                    if not cached_slot_id:
                        continue
                    slot_map[cached_slot_id] = slot_item
                if slot_map:
                    self._slot_cache[sn] = slot_map
                    needs_refresh = False
        if needs_refresh:
            self._schedule_post_patch_refresh(sn)
        self._notify_listeners()

    @callback
    def _handle_interval(self, *_args: object) -> None:
        self._schedule_refresh_task(
            self.async_refresh(reason="interval"),
            f"{DOMAIN}_schedule_interval_refresh",
            fallback=lambda: self.async_refresh(reason="interval"),
        )

    def _schedule_refresh_task(
        self,
        coro: Coroutine[Any, Any, None],
        name: str,
        *,
        fallback: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Schedule and retain a refresh for deterministic shutdown."""

        if self._stopping:
            coro.close()
            return
        try:
            task = self.hass.async_create_task(coro, name=name)
        except TypeError:
            coro.close()
            task = self.hass.async_create_task(fallback())
        if not isinstance(task, asyncio.Future):
            return
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._last_sync is not None:
            age = dt_util.utcnow() - self._last_sync
            if age < SYNC_INTERVAL:
                return
        self._schedule_refresh_task(
            self._refresh_if_stale(),
            f"{DOMAIN}_schedule_coordinator_refresh",
            fallback=self._refresh_if_stale,
        )

    async def _refresh_if_stale(self) -> None:
        if not self._last_sync:
            await self.async_refresh(reason="coordinator")
            return
        age = dt_util.utcnow() - self._last_sync
        if age >= SYNC_INTERVAL:
            await self.async_refresh(reason="coordinator")

    async def async_refresh(
        self, *, reason: str = "manual", serials: Iterable[str] | None = None
    ) -> None:
        with request_metrics_scope("schedule_sync"):
            await self._async_refresh_impl(reason=reason, serials=serials)

    async def _async_refresh_impl(
        self, *, reason: str, serials: Iterable[str] | None
    ) -> None:
        if not self._sync_enabled():
            self._last_status = "disabled"
            await self._disable_support()
            return
        if self._scheduler_backoff_active():
            self._last_status = "scheduler_unavailable"
            self._last_error = getattr(self._coordinator, "scheduler_last_error", None)
            return
        if not self._has_scheduler_bearer():
            self._last_status = "missing_bearer"
            return
        if self._lock.locked():
            # Coordinator updates can arrive while an interval refresh is still
            # running; the next scheduled tick will catch up.
            return
        async with self._lock:
            serial_list = (
                list(serials)
                if serials is not None
                else self._coordinator.iter_serials()
            )
            unique_serials = [sn for sn in dict.fromkeys(serial_list) if sn]
            success = True
            if unique_serials:
                semaphore = asyncio.Semaphore(SYNC_REFRESH_CONCURRENCY)

                async def _fetch_one(
                    sn: str,
                ) -> tuple[str, dict[str, Any] | None, Exception | None]:
                    async with semaphore:
                        return (sn, *await self._async_fetch_serial_sync(sn))

                tasks = []
                async with asyncio.TaskGroup() as group:
                    for sn in unique_serials:
                        tasks.append(
                            group.create_task(
                                _fetch_one(sn),
                                name=f"{DOMAIN}_schedule_fetch_{redact_identifier(sn)}",
                            )
                        )
                results = [task.result() for task in tasks]
                auth_result = next(
                    (
                        result
                        for result in results
                        if isinstance(result[2], EnphaseLoginWallUnauthorized)
                    ),
                    None,
                )
                success = auth_result is None and all(
                    result[2] is None for result in results
                )
                if auth_result is not None:
                    self._apply_sync_serial_result(*auth_result)
                else:
                    for sn, response, err in results:
                        self._apply_sync_serial_result(sn, response, err)
            self._last_sync = dt_util.utcnow()
            if success:
                self._last_status = f"ok:{reason}"

    async def _patch_slot(
        self, sn: str, slot_id: str, slot_patch: dict[str, Any]
    ) -> None:
        if slot_id not in self._slot_cache.get(sn, {}):
            return
        if self._scheduler_backoff_active():
            self._last_status = "scheduler_unavailable"
            self._last_error = getattr(self._coordinator, "scheduler_last_error", None)
            return
        try:
            slot_patch = normalize_slot_payload(slot_patch)
            slot_patch["id"] = str(slot_id)
            response = await self._coordinator.client.patch_schedule(
                sn, slot_id, slot_patch
            )
            self._mark_scheduler_available()
        except EnphaseLoginWallUnauthorized as err:
            self._note_login_wall_unauthorized(err)
            return
        except SchedulerUnavailable as err:
            self._last_error = redact_text(
                err,
                site_ids=(getattr(self._coordinator, "site_id", None),),
                identifiers=(sn,),
            )
            self._last_status = "scheduler_unavailable"
            self._note_scheduler_unavailable(err)
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Schedule PATCH failed for %s: %s",
                redact_identifier(sn),
                redact_text(
                    err,
                    site_ids=(getattr(self._coordinator, "site_id", None),),
                    identifiers=(sn,),
                ),
            )
            return
        new_timestamp = None
        if isinstance(response, dict):
            meta = response.get("meta")
            if isinstance(meta, dict):
                new_timestamp = meta.get("serverTimeStamp")
            data = response.get("data")
            if isinstance(data, dict):
                inner_meta = data.get("meta")
                if isinstance(inner_meta, dict) and not new_timestamp:
                    new_timestamp = inner_meta.get("serverTimeStamp")
        if new_timestamp:
            self._meta_cache[sn] = new_timestamp
        self._slot_cache.setdefault(sn, {})[slot_id] = slot_patch
        self._schedule_post_patch_refresh(sn)
        self._notify_listeners()

    async def _create_slot(self, sn: str, slot: dict[str, Any]) -> bool:
        if self._scheduler_backoff_active():
            self._last_status = "scheduler_unavailable"
            self._last_error = getattr(self._coordinator, "scheduler_last_error", None)
            return False
        slot_payload = normalize_slot_payload(slot)
        try:
            response = await self._coordinator.client.create_schedule(sn, slot_payload)
            self._mark_scheduler_available()
        except SchedulerUnavailable as err:
            self._last_error = redact_text(
                err,
                site_ids=(getattr(self._coordinator, "site_id", None),),
                identifiers=(sn,),
            )
            self._last_status = "scheduler_unavailable"
            self._note_scheduler_unavailable(err)
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Schedule create failed for %s: %s",
                redact_identifier(sn),
                redact_text(
                    err,
                    site_ids=(getattr(self._coordinator, "site_id", None),),
                    identifiers=(sn,),
                ),
            )
            self._last_error = redact_text(
                err,
                site_ids=(getattr(self._coordinator, "site_id", None),),
                identifiers=(sn,),
            )
            self._last_status = "create_failed"
            return False

        new_timestamp = None
        created_slot_id = str(slot_payload.get("id") or "")
        if isinstance(response, dict):
            meta = response.get("meta")
            if isinstance(meta, dict):
                new_timestamp = meta.get("serverTimeStamp")
            data = response.get("data")
            if isinstance(data, str) and data.strip():
                created_slot_id = data.strip()
            elif isinstance(data, dict):
                inner_meta = data.get("meta")
                if isinstance(inner_meta, dict) and not new_timestamp:
                    new_timestamp = inner_meta.get("serverTimeStamp")
                candidate_id = data.get("id")
                if candidate_id is not None and str(candidate_id).strip():
                    created_slot_id = str(candidate_id).strip()
        if new_timestamp:
            self._meta_cache[sn] = new_timestamp
        if created_slot_id:
            slot_payload["id"] = created_slot_id
            self._slot_cache.setdefault(sn, {})[created_slot_id] = slot_payload
        self._schedule_post_patch_refresh(sn)
        self._notify_listeners()
        return True

    async def _sync_serial(self, sn: str) -> None:
        response, err = await self._async_fetch_serial_sync(sn)
        self._apply_sync_serial_result(sn, response, err)

    async def _async_fetch_serial_sync(
        self, sn: str
    ) -> tuple[dict[str, Any] | None, Exception | None]:
        try:
            return await self._coordinator.client.get_schedules(sn), None
        except Exception as err:  # noqa: BLE001
            return None, err

    def _apply_sync_serial_result(
        self,
        sn: str,
        response: dict[str, Any] | None,
        err: Exception | None,
    ) -> None:
        if err is None:
            self._mark_scheduler_available()
        elif isinstance(err, EnphaseLoginWallUnauthorized):
            self._note_login_wall_unauthorized(err)
            return
        elif isinstance(err, SchedulerUnavailable):
            self._last_error = redact_text(
                err,
                site_ids=(getattr(self._coordinator, "site_id", None),),
                identifiers=(sn,),
            )
            self._last_status = "scheduler_unavailable"
            self._note_scheduler_unavailable(err)
            return
        else:
            self._last_error = redact_text(
                err,
                site_ids=(getattr(self._coordinator, "site_id", None),),
                identifiers=(sn,),
            )
            _LOGGER.warning(
                "Failed to fetch schedules for %s: %s",
                redact_identifier(sn),
                redact_text(
                    err,
                    site_ids=(getattr(self._coordinator, "site_id", None),),
                    identifiers=(sn,),
                ),
            )
            return
        self._last_error = None

        meta = response.get("meta") if isinstance(response, dict) else None
        if isinstance(meta, dict):
            self._meta_cache[sn] = meta.get("serverTimeStamp")
        config = response.get("config") if isinstance(response, dict) else None
        if isinstance(config, dict):
            self._config_cache[sn] = config
        else:
            self._config_cache.pop(sn, None)
        slots = response.get("slots") if isinstance(response, dict) else None
        if not isinstance(slots, list):
            slots = []
        slot_map: dict[str, dict[str, Any]] = {}
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            slot_id = str(slot.get("id") or "")
            if not slot_id:
                continue
            slot_map[slot_id] = slot
        self._slot_cache[sn] = slot_map
        self._notify_listeners()

    def _default_server_timestamp(self) -> str:
        return cast(str, dt_util.utcnow().isoformat(timespec="milliseconds"))

    async def async_replace_slots(self, sn: str, slots: list[dict[str, Any]]) -> None:
        if not self._sync_enabled():
            return
        if self._scheduler_backoff_active():
            self._last_status = "scheduler_unavailable"
            self._last_error = getattr(self._coordinator, "scheduler_last_error", None)
            return
        server_timestamp = self._meta_cache.get(sn)
        if not server_timestamp:
            # Collection writes need the server timestamp as optimistic
            # concurrency metadata.
            await self.async_refresh(reason="replace_prepare", serials=[sn])
            server_timestamp = self._meta_cache.get(sn)
        payload_slots: list[dict[str, Any]] = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            payload_slots.append(normalize_slot_payload(slot))
        try:
            response = await self._coordinator.client.patch_schedules(
                sn,
                server_timestamp=server_timestamp or self._default_server_timestamp(),
                slots=payload_slots,
            )
            self._mark_scheduler_available()
        except SchedulerUnavailable as err:
            self._last_error = redact_text(
                err,
                site_ids=(getattr(self._coordinator, "site_id", None),),
                identifiers=(sn,),
            )
            self._last_status = "scheduler_unavailable"
            self._note_scheduler_unavailable(err)
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Schedule collection PATCH failed for %s: %s",
                redact_identifier(sn),
                redact_text(
                    err,
                    site_ids=(getattr(self._coordinator, "site_id", None),),
                    identifiers=(sn,),
                ),
            )
            return

        new_timestamp = None
        response_slots: list[dict[str, Any]] | None = None
        if isinstance(response, dict):
            meta = response.get("meta")
            if isinstance(meta, dict):
                new_timestamp = meta.get("serverTimeStamp")
            data = response.get("data")
            if isinstance(data, dict):
                inner_meta = data.get("meta")
                if isinstance(inner_meta, dict) and not new_timestamp:
                    new_timestamp = inner_meta.get("serverTimeStamp")
                config = data.get("config")
                if isinstance(config, dict):
                    self._config_cache[sn] = config
                slots_data = data.get("slots")
                if isinstance(slots_data, list):
                    response_slots = [
                        slot for slot in slots_data if isinstance(slot, dict)
                    ]
            elif isinstance(data, list):
                response_slots = [slot for slot in data if isinstance(slot, dict)]
        if new_timestamp:
            self._meta_cache[sn] = new_timestamp

        final_slots = response_slots if response_slots is not None else payload_slots
        self._slot_cache[sn] = {
            str(slot.get("id")): slot
            for slot in final_slots
            if isinstance(slot, dict) and slot.get("id")
        }
        self._schedule_post_patch_refresh(sn)
        self._notify_listeners()

    async def async_upsert_slot(self, sn: str, slot: dict[str, Any]) -> bool:
        slot_id = str(slot.get("id") or "")
        if slot_id and slot_id in self._slot_cache.get(sn, {}):
            await self._patch_slot(sn, slot_id, slot)
            return True
        return await self._create_slot(sn, slot)

    async def async_delete_slot(self, sn: str, slot_id: str) -> None:
        if slot_id not in self._slot_cache.get(sn, {}):
            return
        if self._scheduler_backoff_active():
            self._last_status = "scheduler_unavailable"
            self._last_error = getattr(self._coordinator, "scheduler_last_error", None)
            return
        try:
            response = await self._coordinator.client.delete_schedule(sn, slot_id)
            self._mark_scheduler_available()
        except SchedulerUnavailable as err:
            self._last_error = redact_text(
                err,
                site_ids=(getattr(self._coordinator, "site_id", None),),
                identifiers=(sn,),
            )
            self._last_status = "scheduler_unavailable"
            self._note_scheduler_unavailable(err)
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Schedule delete failed for %s: %s",
                redact_identifier(sn),
                redact_text(
                    err,
                    site_ids=(getattr(self._coordinator, "site_id", None),),
                    identifiers=(sn,),
                ),
            )
            return

        new_timestamp = None
        if isinstance(response, dict):
            meta = response.get("meta")
            if isinstance(meta, dict):
                new_timestamp = meta.get("serverTimeStamp")
            data = response.get("data")
            if isinstance(data, dict):
                inner_meta = data.get("meta")
                if isinstance(inner_meta, dict) and not new_timestamp:
                    new_timestamp = inner_meta.get("serverTimeStamp")
        if new_timestamp:
            self._meta_cache[sn] = new_timestamp
        self._slot_cache.get(sn, {}).pop(slot_id, None)
        self._schedule_post_patch_refresh(sn)
        self._notify_listeners()

    async def _ensure_storage_collection(self) -> ScheduleStorageCollection | None:
        if self._storage_collection is not None:
            return self._storage_collection
        if not self._storage_sanitize_done:
            self._storage_sanitize_done = True
            await self._sanitize_schedule_storage()
        await async_setup_component(self.hass, SCHEDULE_DOMAIN, {})
        handlers = self.hass.data.get(websocket_api.DOMAIN) or {}
        handler_entry = handlers.get(f"{SCHEDULE_DOMAIN}/create")
        if not handler_entry:
            return None
        handler = handler_entry[0]
        target = inspect.unwrap(handler)
        self._storage_collection = cast(
            ScheduleStorageCollection | None,
            getattr(getattr(target, "__self__", None), "storage_collection", None),
        )
        return self._storage_collection

    async def _sanitize_schedule_storage(self) -> bool:
        path = self.hass.config.path(".storage", SCHEDULE_DOMAIN)

        def _load() -> object:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return cast(object, json.load(handle))
            except FileNotFoundError:
                return None
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to load schedule storage: %s",
                    redact_text(err),
                )
                return None

        raw = await self.hass.async_add_executor_job(_load)
        if not isinstance(raw, dict):
            return False
        data = raw.get("data")
        if not isinstance(data, dict):
            return False
        items = data.get("items")
        if not isinstance(items, list):
            return False
        changed = False

        for item in items:
            if not isinstance(item, dict):
                continue
            for day in CONF_ALL_DAYS:
                entries = item.get(day)
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    for key in (CONF_FROM, CONF_TO):
                        value = entry.get(key)
                        if not isinstance(value, str) or "." not in value:
                            continue
                        trimmed = value.split(".", 1)[0]
                        try:
                            dt_time.fromisoformat(trimmed)
                        except ValueError:
                            continue
                        entry[key] = trimmed
                        changed = True

        if not changed:
            return False

        def _save() -> bool:
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(raw, handle, ensure_ascii=False, indent=2)
                return True
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to write schedule storage: %s",
                    redact_text(err),
                )
                return False

        saved = await self.hass.async_add_executor_job(_save)
        if saved:
            _LOGGER.warning("Sanitized schedule storage times with microseconds")
        return bool(saved)

    def _sync_enabled(self) -> bool:
        if not self._config_entry:
            return True
        return bool(
            self._config_entry.options.get(
                OPT_SCHEDULE_SYNC_ENABLED,
                DEFAULT_SCHEDULE_SYNC_ENABLED,
            )
        )

    def _has_scheduler_bearer(self) -> bool:
        client = getattr(self._coordinator, "client", None)
        if not client:
            return False
        control_headers = None
        control_fn = getattr(client, "control_headers", None)
        if callable(control_fn):
            if inspect.iscoroutinefunction(control_fn):
                return False
            try:
                control_headers = control_fn()
            except SYNC_CAPTURE_ERRORS:
                control_headers = None
        if isinstance(control_headers, dict) and control_headers.get("Authorization"):
            return True
        has_bearer = getattr(client, "has_scheduler_bearer", None)
        if callable(has_bearer):
            try:
                return bool(has_bearer())
            except SYNC_CAPTURE_ERRORS:
                return False
        bearer = getattr(client, "scheduler_bearer", None)
        if bearer is None:
            return False
        if inspect.iscoroutinefunction(bearer):
            return False
        try:
            token = bearer()
        except SYNC_CAPTURE_ERRORS:
            return False
        if inspect.isawaitable(token):
            if hasattr(token, "close"):
                token.close()
            return False
        return bool(token)
