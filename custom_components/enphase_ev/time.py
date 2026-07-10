from __future__ import annotations

from collections.abc import Callable
from datetime import time as dt_time
from typing import TypeVar, cast

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant, callback as ha_callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .battery_schedule_editor import (
    BatteryScheduleEditorEntity,
    battery_scheduler_enabled,
)
from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity_cleanup import prune_managed_entities
from .evse_schedule_editor import (
    EvseScheduleEditorEntity,
    evse_schedule_editor_active,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data

PARALLEL_UPDATES = 0

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])
callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    return has_encharge is not False


def _type_available(coord: EnphaseCoordinator, type_key: str) -> bool:
    return bool(coord.inventory_view.has_type_for_entities(type_key))


def _type_device_info(coord: EnphaseCoordinator, type_key: str) -> DeviceInfo | None:
    return coord.inventory_view.type_device_info(type_key)


def _battery_schedule_editor_active(
    coord: EnphaseCoordinator, entry: EnphaseConfigEntry | None
) -> bool:
    client = getattr(coord, "client", None)
    return bool(
        battery_scheduler_enabled(entry)
        and callable(getattr(client, "battery_schedules", None))
        and all(
            callable(getattr(client, method, None))
            for method in (
                "create_battery_schedule",
                "update_battery_schedule",
                "delete_battery_schedule",
            )
        )
    )


def _retained_site_time_unique_ids(
    coord: EnphaseCoordinator, entry: EnphaseConfigEntry | None = None
) -> set[str]:
    unique_ids: set[str] = set()
    if not _type_available(coord, "encharge"):
        return unique_ids
    if not battery_scheduler_enabled(entry):
        return unique_ids
    editor_active = _battery_schedule_editor_active(coord, entry)
    if editor_active:
        unique_ids.update(
            {
                f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_start_time",
                f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_end_time",
            }
        )
    return unique_ids


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    added_site_time_unique_ids: set[str] = set()
    known_serials: set[str] = set()
    active_site_time_unique_ids: set[str] = set()
    ent_reg = er.async_get(hass)

    def _managed_site_time_unique_ids() -> set[str]:
        return {
            f"{DOMAIN}_site_{coord.site_id}_charge_from_grid_start_time",
            f"{DOMAIN}_site_{coord.site_id}_charge_from_grid_end_time",
            f"{DOMAIN}_site_{coord.site_id}_discharge_to_grid_start_time",
            f"{DOMAIN}_site_{coord.site_id}_discharge_to_grid_end_time",
            f"{DOMAIN}_site_{coord.site_id}_restrict_battery_discharge_start_time",
            f"{DOMAIN}_site_{coord.site_id}_restrict_battery_discharge_end_time",
            f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_start_time",
            f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_end_time",
            f"{DOMAIN}_site_{coord.site_id}_battery_new_schedule_start_time",
            f"{DOMAIN}_site_{coord.site_id}_battery_new_schedule_end_time",
        }

    def _charger_schedule_time_unique_ids(sn: str) -> set[str]:
        return {
            f"{DOMAIN}_{sn}_schedule_edit_start_time",
            f"{DOMAIN}_{sn}_schedule_edit_end_time",
        }

    def _site_time_entities_by_unique_id(
        retained_site_time_unique_ids: set[str],
    ) -> dict[str, TimeEntity]:
        entity_factories: dict[str, Callable[[], TimeEntity]] = {}
        if battery_scheduler_enabled(entry):
            entity_factories[
                f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_start_time"
            ] = lambda: BatteryScheduleEditStartTimeEntity(coord, entry)
            entity_factories[
                f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_end_time"
            ] = lambda: BatteryScheduleEditEndTimeEntity(coord, entry)

        active_site_time_unique_ids = retained_site_time_unique_ids

        return {
            unique_id: factory()
            for unique_id, factory in entity_factories.items()
            if unique_id in active_site_time_unique_ids
        }

    @callback
    def _async_sync_site_entities() -> None:
        nonlocal active_site_time_unique_ids
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        retained_site_time_unique_ids = _retained_site_time_unique_ids(coord, entry)
        active_site_time_unique_ids = set()
        if _site_has_battery(coord) and _type_available(coord, "encharge"):
            active_site_time_unique_ids = retained_site_time_unique_ids
            current_site_entities = _site_time_entities_by_unique_id(
                retained_site_time_unique_ids
            )
            site_entities = [
                entity
                for unique_id, entity in current_site_entities.items()
                if unique_id not in added_site_time_unique_ids
            ]
            if site_entities:
                async_add_entities(site_entities, update_before_add=False)
                added_site_time_unique_ids.update(
                    entity.unique_id
                    for entity in site_entities
                    if isinstance(entity.unique_id, str)
                )
        added_site_time_unique_ids.intersection_update(active_site_time_unique_ids)
        if not inventory_ready:
            return
        prune_managed_entities(
            ent_reg,
            entry.entry_id,
            domain="time",
            active_unique_ids=active_site_time_unique_ids,
            is_managed=lambda unique_id: unique_id in _managed_site_time_unique_ids(),
        )

    @callback
    def _async_sync_charger_entities() -> None:
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        current_serials = {sn for sn in coord.iter_serials() if sn}
        serials = [sn for sn in current_serials if sn not in known_serials]
        entities: list[TimeEntity] = []
        if evse_schedule_editor_active(coord, entry):
            for sn in serials:
                entities.append(EvseScheduleEditStartTimeEntity(coord, entry, sn))
                entities.append(EvseScheduleEditEndTimeEntity(coord, entry, sn))
        if entities:
            async_add_entities(entities, update_before_add=False)
        known_serials.intersection_update(current_serials)
        known_serials.update(serials)
        if not inventory_ready:
            return
        active_unique_ids: set[str] = set()
        if evse_schedule_editor_active(coord, entry):
            for sn in current_serials:
                active_unique_ids.update(_charger_schedule_time_unique_ids(sn))
        prune_managed_entities(
            ent_reg,
            entry.entry_id,
            domain="time",
            active_unique_ids=active_unique_ids | active_site_time_unique_ids,
            is_managed=lambda unique_id: (
                unique_id in _managed_site_time_unique_ids()
                or unique_id.endswith(
                    ("_schedule_edit_start_time", "_schedule_edit_end_time")
                )
            ),
        )

    add_topology_listener = getattr(coord, "async_add_topology_listener", None)
    if callable(add_topology_listener):
        entry.async_on_unload(add_topology_listener(_async_sync_site_entities))
        entry.async_on_unload(add_topology_listener(_async_sync_charger_entities))
    add_listener = getattr(coord, "async_add_listener", None)
    if callable(add_listener):
        entry.async_on_unload(add_listener(_async_sync_site_entities))
        entry.async_on_unload(add_listener(_async_sync_charger_entities))
    _async_sync_site_entities()
    _async_sync_charger_entities()


def _parse_editor_time(value: str | None) -> dt_time | None:
    if not value:
        return None
    try:
        return dt_time.fromisoformat(value)
    except ValueError:
        return None


class _BatteryScheduleEditorTimeEntity(
    BatteryScheduleEditorEntity,  # type: ignore[misc, unused-ignore]
    TimeEntity,  # type: ignore[misc, unused-ignore]
):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coord: EnphaseCoordinator,
        entry: EnphaseConfigEntry,
        *,
        unique_suffix: str,
        translation_key: str,
        key: str,
    ) -> None:
        super().__init__(coord, entry)
        self._attr_translation_key = translation_key
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{unique_suffix}"

    @property
    def available(self) -> bool:
        client = getattr(self._coord, "client", None)
        return (
            super().available
            and battery_scheduler_enabled(self._entry)
            and _type_available(self._coord, "encharge")
            and getattr(self._coord, "battery_write_access_confirmed", False)
            and callable(getattr(client, "battery_schedules", None))
            and callable(getattr(client, "create_battery_schedule", None))
            and callable(getattr(client, "update_battery_schedule", None))
            and callable(getattr(client, "delete_battery_schedule", None))
            and self._editor is not None
        )

    @property
    def native_value(self) -> dt_time | None:
        if self._editor is None:
            return None
        return _parse_editor_time(getattr(self._editor.edit, self._key))

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class BatteryScheduleEditStartTimeEntity(_BatteryScheduleEditorTimeEntity):
    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(
            coord,
            entry,
            unique_suffix="battery_schedule_edit_start_time",
            translation_key="battery_schedule_edit_start_time",
            key="start_time",
        )

    async def async_set_value(self, value: dt_time) -> None:
        if self._editor is not None:
            self._editor.set_edit_time("start_time", value)


class BatteryScheduleEditEndTimeEntity(_BatteryScheduleEditorTimeEntity):
    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(
            coord,
            entry,
            unique_suffix="battery_schedule_edit_end_time",
            translation_key="battery_schedule_edit_end_time",
            key="end_time",
        )

    async def async_set_value(self, value: dt_time) -> None:
        if self._editor is not None:
            self._editor.set_edit_time("end_time", value)


class _EvseScheduleEditorTimeEntity(
    EvseScheduleEditorEntity,  # type: ignore[misc, unused-ignore]
    TimeEntity,  # type: ignore[misc, unused-ignore]
):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coord: EnphaseCoordinator,
        entry: EnphaseConfigEntry,
        sn: str,
        *,
        unique_suffix: str,
        translation_key: str,
        key: str,
    ) -> None:
        super().__init__(coord, entry, sn)
        self._attr_translation_key = translation_key
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{sn}_{unique_suffix}"

    @property
    def available(self) -> bool:
        return (
            super().available
            and evse_schedule_editor_active(self._coord, self._entry)
            and self._editor is not None
        )

    @property
    def native_value(self) -> dt_time | None:
        if self._editor is None:
            return None
        value = getattr(self._editor.form_state(self._sn), self._key)
        return _parse_editor_time(value)


class EvseScheduleEditStartTimeEntity(_EvseScheduleEditorTimeEntity):
    def __init__(
        self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry, sn: str
    ) -> None:
        super().__init__(
            coord,
            entry,
            sn,
            unique_suffix="schedule_edit_start_time",
            translation_key="evse_schedule_edit_start_time",
            key="start_time",
        )

    async def async_set_value(self, value: dt_time) -> None:
        if self._editor is not None:
            self._editor.set_edit_time(self._sn, "start_time", value)


class EvseScheduleEditEndTimeEntity(_EvseScheduleEditorTimeEntity):
    def __init__(
        self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry, sn: str
    ) -> None:
        super().__init__(
            coord,
            entry,
            sn,
            unique_suffix="schedule_edit_end_time",
            translation_key="evse_schedule_edit_end_time",
            key="end_time",
        )

    async def async_set_value(self, value: dt_time) -> None:
        if self._editor is not None:
            self._editor.set_edit_time(self._sn, "end_time", value)
