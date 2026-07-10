from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar, cast

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback as ha_callback
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
    site_entity_added = False

    @callback
    def _async_sync_site_entities() -> None:
        nonlocal site_entity_added
        if site_entity_added:
            return
        if not _site_has_battery(coord, strict=True) or not _type_available(
            coord, "encharge"
        ):
            return
        async_add_entities(
            [BackupHistoryCalendarEntity(coord)], update_before_add=False
        )
        site_entity_added = True

    unsubscribe = coord.async_add_listener(_async_sync_site_entities)
    entry.async_on_unload(unsubscribe)
    _async_sync_site_entities()


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
