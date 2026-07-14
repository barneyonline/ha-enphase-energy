from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar, cast

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback as ha_callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .runtime_helpers import (
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data
from .system_events import (
    SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY,
    SystemEventHistoryEntry,
)

PARALLEL_UPDATES = 0

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])
callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)


def _site_has_battery(coord: EnphaseCoordinator, *, strict: bool = False) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    if strict:
        return has_encharge is True
    return has_encharge is not False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    backup_history_entity_added = False
    system_event_history_entity_added = False
    ent_reg = er.async_get(hass)

    def _site_calendar_unique_id(key: str) -> str:
        return f"{DOMAIN}_site_{coord.site_id}_{key}"

    @callback
    def _async_sync_site_entities() -> None:
        nonlocal backup_history_entity_added, system_event_history_entity_added
        if (
            not backup_history_entity_added
            and _site_has_battery(coord, strict=True)
            and _type_available(coord, "encharge")
        ):
            async_add_entities(
                [BackupHistoryCalendarEntity(coord)], update_before_add=False
            )
            backup_history_entity_added = True

        history_entity_id = ent_reg.async_get_entity_id(
            "calendar",
            DOMAIN,
            _site_calendar_unique_id("system_event_history"),
        )
        endpoint_health = coord._endpoint_family_state(
            SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
        )
        endpoint_suppressed = endpoint_health.support_state == "suppressed"
        if not endpoint_suppressed and (
            coord.system_events_runtime.history_available
            or history_entity_id is not None
        ):
            if not system_event_history_entity_added:
                async_add_entities(
                    [SystemEventHistoryCalendarEntity(coord)],
                    update_before_add=False,
                )
                system_event_history_entity_added = True
        elif bool(getattr(coord, "_devices_inventory_ready", False)):
            if history_entity_id is not None:
                ent_reg.async_remove(history_entity_id)
            system_event_history_entity_added = False

    unsubscribe = coord.async_add_listener(_async_sync_site_entities)
    entry.async_on_unload(unsubscribe)
    _async_sync_site_entities()


class SystemEventHistoryCalendarEntity(
    CoordinatorEntity,  # type: ignore[misc]
    CalendarEntity,  # type: ignore[misc]
):
    """Expose sanitized Enphase homeowner event history as a calendar."""

    _attr_has_entity_name = True
    _attr_translation_key = "system_event_history"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_system_event_history"

    @property
    def available(self) -> bool:
        endpoint_health = self._coord._endpoint_family_state(
            SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
        )
        return bool(
            self._coord.system_events_runtime.history_available
            and endpoint_health.support_state != "suppressed"
        )

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:cloud")},
            manufacturer="Enphase",
        )

    @property
    def event(self) -> CalendarEvent | None:
        return None

    def _to_calendar_event(self, item: SystemEventHistoryEntry) -> CalendarEvent:
        summary = (
            item.summary.strip() if item.summary and item.summary.strip() else None
        )
        if summary is None:
            try:
                name = self.name
            except Exception:  # noqa: BLE001 - platform may not be attached in tests
                name = None
            if isinstance(name, str) and name.strip():
                summary = name.strip()
            else:
                entity_id = getattr(self, "entity_id", None)
                if isinstance(entity_id, str) and entity_id.strip():
                    summary = entity_id.strip()
        return CalendarEvent(
            summary=summary or self._attr_unique_id,
            start=item.start,
            end=item.end,
            description=item.description,
        )

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        _ = hass
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        if end_date <= start_date:
            return []
        events = await self._coord.system_events_runtime.async_history_events(
            start_date,
            end_date,
        )
        return [self._to_calendar_event(item) for item in events]


class BackupHistoryCalendarEntity(
    CoordinatorEntity,  # type: ignore[misc]
    CalendarEntity,  # type: ignore[misc]
):
    _attr_has_entity_name = True
    _attr_translation_key = "backup_history"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_backup_history"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if not _type_available(self._coord, "encharge"):
            return False
        return _site_has_battery(self._coord)

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )

    def _iter_history_events(self) -> list[tuple[datetime, datetime]]:
        events: list[tuple[datetime, datetime]] = []
        for item in self._coord.battery_backup_history_events:
            if not isinstance(item, dict):
                continue
            start = item.get("start")
            end = item.get("end")
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                continue
            if start.tzinfo is None or end.tzinfo is None:
                continue
            if end <= start:
                continue
            events.append((start, end))
        return events

    def _to_calendar_event(self, start: datetime, end: datetime) -> CalendarEvent:
        summary: str | None = None
        try:
            name = self.name
        except Exception:  # noqa: BLE001 - platform may not be attached in tests
            name = None
        if isinstance(name, str) and name.strip():
            summary = name.strip()
        else:
            entity_id = getattr(self, "entity_id", None)
            if isinstance(entity_id, str) and entity_id.strip():
                summary = entity_id.strip()
        return CalendarEvent(
            summary=summary or self._attr_unique_id,
            start=start,
            end=end,
        )

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        next_upcoming: tuple[datetime, datetime] | None = None
        for start, end in self._iter_history_events():
            if start <= now < end:
                return self._to_calendar_event(start, end)
            if start > now:
                next_upcoming = (start, end)
                break
        if next_upcoming is None:
            return None
        return self._to_calendar_event(next_upcoming[0], next_upcoming[1])

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        _ = hass
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        out: list[CalendarEvent] = []
        for event_start, event_end in self._iter_history_events():
            if event_end <= start_date or event_start >= end_date:
                continue
            out.append(self._to_calendar_event(event_start, event_end))
        return out
