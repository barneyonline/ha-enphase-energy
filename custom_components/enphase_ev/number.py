"""Number entities for Enphase current limits and battery schedule settings."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, cast

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback as ha_callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .battery_schedule_editor import (
    BatteryScheduleEditorEntity,
    battery_scheduler_enabled,
)
from .const import DOMAIN, SAFE_LIMIT_AMPS
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity, evse_amp_control_applicable
from .entity_cleanup import prune_managed_entities
from .runtime_helpers import (
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data
from .tariff import tariff_rate_sensor_specs

PARALLEL_UPDATES = 0

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])
callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    return has_encharge is not False


def _battery_write_access_confirmed(coord: EnphaseCoordinator) -> bool:
    confirmed = getattr(coord, "battery_write_access_confirmed", None)
    owner = getattr(coord, "battery_user_is_owner", None)
    installer = getattr(coord, "battery_user_is_installer", None)
    if owner is True or installer is True:
        return True
    if confirmed is not None:
        return bool(confirmed)
    # Battery write access starts unknown until BatteryConfig permissions load.
    return False


def _battery_write_access_explicitly_denied(coord: EnphaseCoordinator) -> bool:
    if getattr(coord, "battery_write_access_confirmed", None) is True:
        return False
    return (
        getattr(coord, "battery_user_is_owner", None) is False
        and getattr(coord, "battery_user_is_installer", None) is False
    )


def _battery_schedule_editor_active(
    coord: EnphaseCoordinator, entry: EnphaseConfigEntry | None
) -> bool:
    client = getattr(coord, "client", None)
    return bool(
        battery_scheduler_enabled(entry)
        and callable(getattr(client, "battery_schedules", None))
        and callable(getattr(client, "create_battery_schedule", None))
        and callable(getattr(client, "update_battery_schedule", None))
        and callable(getattr(client, "delete_battery_schedule", None))
    )


def _retained_site_number_unique_ids(
    coord: EnphaseCoordinator, entry: EnphaseConfigEntry | None = None
) -> set[str]:
    unique_ids: set[str] = set()
    if not _type_available(coord, "encharge"):
        return unique_ids
    write_access_denied = _battery_write_access_explicitly_denied(coord)
    core_unique_ids = {
        f"{DOMAIN}_site_{coord.site_id}_battery_reserve",
        f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level",
    }
    if not battery_scheduler_enabled(entry):
        return set() if write_access_denied else core_unique_ids
    editor_active = _battery_schedule_editor_active(coord, entry)
    if not write_access_denied:
        unique_ids |= core_unique_ids
    if editor_active:
        unique_ids.add(f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_limit")
    return unique_ids


def _tariff_rate_number_unique_id(
    coord: EnphaseCoordinator, spec: dict, *, is_import: bool  # type: ignore[type-arg]
) -> str:
    prefix = "tariff_import_rate" if is_import else "tariff_export_rate"
    return f"{DOMAIN}_site_{coord.site_id}_{prefix}_{spec['key']}_number"


def _tariff_rate_number_entities(coord: EnphaseCoordinator) -> dict[str, NumberEntity]:
    if not bool(getattr(coord, "pricing_edits_enabled", True)):
        return {}
    entities: dict[str, NumberEntity] = {}
    for is_import, attr in (
        (True, "tariff_import_rate"),
        (False, "tariff_export_rate"),
    ):
        for spec in tariff_rate_sensor_specs(getattr(coord, attr, None)):
            attributes = spec.get("attributes")
            locator = (
                attributes.get("tariff_locator")
                if isinstance(attributes, dict)
                else None
            )
            if not isinstance(locator, dict):
                continue
            unique_id = _tariff_rate_number_unique_id(coord, spec, is_import=is_import)
            entities[unique_id] = EnphaseTariffRateNumber(
                coord, spec, is_import=is_import
            )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    ent_reg = er.async_get(hass)
    known_serials: set[str] = set()
    added_default_charge_level_unique_ids: set[str] = set()
    added_site_number_unique_ids: set[str] = set()
    live_tariff_entities: dict[str, EnphaseTariffRateNumber] = {}

    def _managed_site_number_unique_ids() -> set[str]:
        return {
            f"{DOMAIN}_site_{coord.site_id}_battery_reserve",
            f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level",
            f"{DOMAIN}_site_{coord.site_id}_battery_cfg_schedule_limit",
            f"{DOMAIN}_site_{coord.site_id}_battery_dtg_schedule_limit",
            f"{DOMAIN}_site_{coord.site_id}_battery_rbd_schedule_limit",
            f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_limit",
            f"{DOMAIN}_site_{coord.site_id}_battery_new_schedule_limit",
        }

    def _tariff_number_managed(unique_id: str) -> bool:
        return unique_id.startswith(
            f"{DOMAIN}_site_{coord.site_id}_tariff_import_rate_"
        ) or unique_id.startswith(f"{DOMAIN}_site_{coord.site_id}_tariff_export_rate_")

    def _core_site_number_unique_ids() -> set[str]:
        return {
            f"{DOMAIN}_site_{coord.site_id}_battery_reserve",
            f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level",
        }

    def _charger_number_unique_id(sn: str) -> str:
        return f"{DOMAIN}_{sn}_amps_number"

    def _default_charge_level_number_unique_id(sn: str) -> str:
        return f"{DOMAIN}_{sn}_default_charge_level_number"

    def _site_number_entities_by_unique_id(
        retained_site_number_unique_ids: set[str],
    ) -> dict[str, NumberEntity]:
        site_entities: dict[str, NumberEntity] = {}

        entity_factories: dict[str, Callable[[], NumberEntity]] = {
            f"{DOMAIN}_site_{coord.site_id}_battery_reserve": lambda: BatteryReserveNumber(
                coord
            ),
            f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level": lambda: BatteryShutdownLevelNumber(
                coord
            ),
        }

        if battery_scheduler_enabled(entry):
            entity_factories[
                f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_limit"
            ] = lambda: BatteryScheduleEditLimitNumber(coord, entry)

        active_site_number_unique_ids = (
            retained_site_number_unique_ids & _core_site_number_unique_ids()
        )
        if battery_scheduler_enabled(entry):
            active_site_number_unique_ids |= retained_site_number_unique_ids & {
                f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_limit"
            }

        for unique_id, factory in entity_factories.items():
            if unique_id in active_site_number_unique_ids:
                site_entities[unique_id] = factory()

        return site_entities

    @callback
    def _async_sync_chargers() -> None:
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        current_serials = {sn for sn in coord.iter_serials() if sn}
        retained_site_number_unique_ids = _retained_site_number_unique_ids(coord, entry)
        active_site_number_unique_ids: set[str] = set()
        site_entities: list[NumberEntity] = []
        tariff_entities = _tariff_rate_number_entities(coord)
        active_site_number_unique_ids |= set(tariff_entities)
        new_tariff_entities = {
            unique_id: entity
            for unique_id, entity in tariff_entities.items()
            if unique_id not in added_site_number_unique_ids
        }
        site_entities.extend(new_tariff_entities.values())
        if _site_has_battery(coord) and _type_available(coord, "encharge"):
            active_site_number_unique_ids |= (
                retained_site_number_unique_ids & _core_site_number_unique_ids()
            )
            if battery_scheduler_enabled(entry):
                active_site_number_unique_ids |= retained_site_number_unique_ids & {
                    f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_limit"
                }
            current_site_entities = _site_number_entities_by_unique_id(
                retained_site_number_unique_ids
            )
            site_entities.extend(
                entity
                for unique_id, entity in current_site_entities.items()
                if unique_id not in added_site_number_unique_ids
            )
        if site_entities:
            async_add_entities(site_entities, update_before_add=False)
            added_site_number_unique_ids.update(
                entity.unique_id
                for entity in site_entities
                if isinstance(entity.unique_id, str)
            )
            live_tariff_entities.update(new_tariff_entities)

        stale_tariff_unique_ids = set(live_tariff_entities) - set(tariff_entities)
        for unique_id in stale_tariff_unique_ids:
            entity = live_tariff_entities.pop(unique_id)
            added_site_number_unique_ids.discard(unique_id)
            task = hass.async_create_task(
                entity.async_remove(force_remove=True),
                name=f"{DOMAIN}_remove_stale_tariff_number",
            )
            track_task = getattr(coord, "track_entry_background_task", None)
            if callable(track_task):
                track_task(task)
        serials = [sn for sn in current_serials if sn not in known_serials]
        if not serials:
            entities: list[NumberEntity] = []
        else:
            entities = []
            for sn in serials:
                entities.append(ChargingAmpsNumber(coord, sn))
        for sn in current_serials:
            data = coord.data.get(sn, {}) if isinstance(coord.data, dict) else {}
            unique_id = _default_charge_level_number_unique_id(sn)
            if (
                isinstance(data, dict)
                and data.get("default_charge_level_supported") is True
                and unique_id not in added_default_charge_level_unique_ids
            ):
                entities.append(DefaultChargeLevelNumber(coord, sn))
        if entities:
            async_add_entities(entities, update_before_add=False)
            added_default_charge_level_unique_ids.update(
                entity.unique_id
                for entity in entities
                if isinstance(entity, DefaultChargeLevelNumber)
                and isinstance(entity.unique_id, str)
            )
        known_serials.intersection_update(current_serials)
        known_serials.update(serials)

        if not bool(getattr(coord, "pricing_edits_enabled", True)):
            prune_managed_entities(
                ent_reg,
                entry.entry_id,
                domain="number",
                active_unique_ids=set(),
                is_managed=_tariff_number_managed,
            )

        if not inventory_ready:
            return

        # Registry cleanup waits for inventory so numbers are not removed while
        # optional BatteryConfig endpoints are still warming up.
        if _site_has_battery(coord) and _type_available(coord, "encharge"):
            loaded_site_number_unique_ids = {
                unique_id
                for unique_id in added_site_number_unique_ids
                if unique_id in _managed_site_number_unique_ids()
            }
        else:
            loaded_site_number_unique_ids = set()
        active_charger_unique_ids = {
            _charger_number_unique_id(sn) for sn in current_serials
        }
        for sn in current_serials:
            data = coord.data.get(sn, {}) if isinstance(coord.data, dict) else {}
            unique_id = _default_charge_level_number_unique_id(sn)
            if isinstance(data, dict) and (
                data.get("default_charge_level_supported") is True
                or (
                    unique_id in added_default_charge_level_unique_ids
                    and data.get("default_charge_level_supported") is not False
                )
            ):
                active_charger_unique_ids.add(unique_id)
        prune_managed_entities(
            ent_reg,
            entry.entry_id,
            domain="number",
            active_unique_ids={
                *active_site_number_unique_ids,
                *loaded_site_number_unique_ids,
                *active_charger_unique_ids,
            },
            is_managed=lambda unique_id: (
                unique_id in _managed_site_number_unique_ids()
                or _tariff_number_managed(unique_id)
                or unique_id.endswith(
                    (
                        "_amps_number",
                        "_default_charge_level_number",
                        "_schedule_edit_limit",
                    )
                )
            ),
        )

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


class BatteryReserveNumber(CoordinatorEntity, NumberEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "battery_reserve"
    _attr_native_min_value = 5.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_reserve"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and not _battery_write_access_explicitly_denied(self._coord)
            and self._coord.battery_reserve_editable
        )

    @property
    def native_value(self) -> float | None:
        value = self._coord.battery_selected_backup_percentage
        if value is None:
            return None
        return float(value)

    @property
    def native_min_value(self) -> float:
        return float(self._coord.battery_reserve_min)

    @property
    def native_max_value(self) -> float:
        return float(self._coord.battery_reserve_max)

    async def async_set_native_value(self, value: float) -> None:
        reserve = int(value)
        if self._coord.battery_selected_backup_percentage == reserve:
            return
        await self._coord.async_set_battery_reserve(reserve)

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class ChargingAmpsNumber(EnphaseBaseEntity, NumberEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "charging_amps"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_amps_number"

    @staticmethod
    def _safe_limit_active(value: object) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return bool(value)
        try:
            return int(str(value).strip()) != 0
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _charging_active(value: object) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes", "y", "on"):
                return True
            if normalized in ("false", "0", "no", "n", "off"):
                return False
            return False
        return False

    @staticmethod
    def _coerce_amp(value: object) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(float(str(value).strip()))
        except Exception:  # noqa: BLE001
            return None

    @classmethod
    def _safe_limit_amps(cls, data: dict[str, Any]) -> int:
        min_amp = cls._coerce_amp(data.get("min_amp"))
        if min_amp is not None and min_amp > 0:
            return min_amp
        return SAFE_LIMIT_AMPS

    @property
    def native_value(self) -> float | None:
        data = self.data
        if not evse_amp_control_applicable(self._coord, self._sn):
            return float(self._coord.pick_start_amps(self._sn))
        if self._safe_limit_active(
            data.get("safe_limit_state")
        ) and self._charging_active(data.get("charging")):
            return float(self._safe_limit_amps(data))
        lvl = data.get("charging_level")
        if lvl is None:
            # Let coordinator choose a safe default within charger limits
            return float(self._coord.pick_start_amps(self._sn))
        try:
            return float(int(lvl))
        except Exception:
            return float(self._coord.pick_start_amps(self._sn))

    @property
    def native_min_value(self) -> float:
        v = self._coerce_amp(self.data.get("min_amp"))
        return float(v) if v is not None else 6.0

    @property
    def native_max_value(self) -> float:
        v = self._coerce_amp(self.data.get("max_amp"))
        return float(v) if v is not None else 40.0

    @property
    def native_step(self) -> float:
        return 1.0

    async def async_set_native_value(self, value: float) -> None:
        amps = int(value)
        # Store desired setpoint locally; do not start charging here.
        # Start actions (switch/button/service) will use this setpoint.
        self._coord.set_last_set_amps(self._sn, amps)
        await self._coord.async_request_refresh()
        if bool(self.data.get("charging")) and evse_amp_control_applicable(
            self._coord, self._sn
        ):
            # Restart the active session so the updated amps take effect
            self._coord.schedule_amp_restart(self._sn)


class DefaultChargeLevelNumber(EnphaseBaseEntity, NumberEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "default_charge_level"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_default_charge_level_number"

    @staticmethod
    def _coerce_amp(value: object) -> int | None:
        return ChargingAmpsNumber._coerce_amp(value)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.data.get("default_charge_level_supported") is True
        )

    @property
    def native_value(self) -> float | None:
        value = self._coerce_amp(self.data.get("default_charge_level"))
        return float(value) if value is not None else None

    @property
    def native_min_value(self) -> float:
        v = self._coerce_amp(self.data.get("min_amp"))
        return float(v) if v is not None else 6.0

    @property
    def native_max_value(self) -> float:
        v = self._coerce_amp(self.data.get("max_amp"))
        return float(v) if v is not None else 40.0

    @property
    def native_step(self) -> float:
        v = self._coerce_amp(self.data.get("amp_granularity"))
        return float(v) if v is not None and v > 0 else 1.0

    @property
    def extra_state_attributes(self) -> dict[str, object | None]:
        return {
            "min_amp": self._coerce_amp(self.data.get("min_amp")),
            "max_amp": self._coerce_amp(self.data.get("max_amp")),
            "max_current": self._coerce_amp(self.data.get("max_current")),
            "amp_granularity": self._coerce_amp(self.data.get("amp_granularity")),
            "charging_amps": self._coerce_amp(self.data.get("charging_level")),
            "default_charge_level_supported_source": self.data.get(
                "default_charge_level_supported_source"
            ),
        }

    async def async_set_native_value(self, value: float) -> None:
        await self._coord.evse_runtime.async_set_default_charge_level(
            self._sn,
            int(value),
        )


class BatteryShutdownLevelNumber(CoordinatorEntity, NumberEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "battery_shutdown_level"
    _attr_native_min_value = 5.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_shutdown_level"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and not _battery_write_access_explicitly_denied(self._coord)
            and self._coord.battery_shutdown_level_available
        )

    @property
    def native_value(self) -> float | None:
        value = self._coord.battery_shutdown_level
        if value is None:
            return None
        return float(value)

    @property
    def native_min_value(self) -> float:
        return float(self._coord.battery_shutdown_level_min)

    @property
    def native_max_value(self) -> float:
        return float(self._coord.battery_shutdown_level_max)

    async def async_set_native_value(self, value: float) -> None:
        level = int(value)
        if self._coord.battery_shutdown_level == level:
            return
        await self._coord.async_set_battery_shutdown_level(level)

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class _BatteryScheduleEditorLimitNumber(BatteryScheduleEditorEntity, NumberEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 5.0
    _attr_native_max_value = 100.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coord: EnphaseCoordinator,
        entry: EnphaseConfigEntry,
        *,
        unique_suffix: str,
        translation_key: str,
    ) -> None:
        super().__init__(coord, entry)
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{unique_suffix}"

    @property
    def available(self) -> bool:
        client = getattr(self._coord, "client", None)
        return (
            super().available
            and battery_scheduler_enabled(self._entry)
            and _type_available(self._coord, "encharge")
            and _battery_write_access_confirmed(self._coord)
            and callable(getattr(client, "battery_schedules", None))
            and callable(getattr(client, "create_battery_schedule", None))
            and callable(getattr(client, "update_battery_schedule", None))
            and callable(getattr(client, "delete_battery_schedule", None))
            and self._editor is not None
        )

    @property
    def native_value(self) -> float | None:
        if self._editor is None:
            return None
        return float(self._editor.edit.limit)

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class BatteryScheduleEditLimitNumber(_BatteryScheduleEditorLimitNumber):
    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(
            coord,
            entry,
            unique_suffix="battery_schedule_edit_limit",
            translation_key="battery_schedule_edit_limit",
        )

    async def async_set_native_value(self, value: float) -> None:
        if self._editor is not None:
            self._editor.set_edit_limit(int(value))


class EnphaseTariffRateNumber(CoordinatorEntity, NumberEntity):  # type: ignore[misc]
    """Editable tariff rate value."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0.0
    _attr_native_step = 0.0001
    _attr_suggested_display_precision = 4

    def __init__(self, coord: EnphaseCoordinator, spec: dict, *, is_import: bool) -> None:  # type: ignore[type-arg]
        super().__init__(coord)
        self._coord = coord
        self._is_import = is_import
        self._rate_attr = "tariff_import_rate" if is_import else "tariff_export_rate"
        self._rate_prefix = "tariff_import_rate" if is_import else "tariff_export_rate"
        self._detail_key = str(spec.get("key") or "rate")
        detail_name = str(
            spec.get("name") or self._detail_key.replace("_", " ").title()
        )
        self._attr_translation_key = f"{self._rate_prefix}_value"
        self._attr_translation_placeholders = {"detail": detail_name}
        self._attr_unique_id = _tariff_rate_number_unique_id(
            coord, spec, is_import=is_import
        )
        self._attr_icon = "mdi:cash-minus" if is_import else "mdi:cash-plus"

    def _spec(self) -> dict | None:  # type: ignore[type-arg]
        for spec in tariff_rate_sensor_specs(
            getattr(self._coord, self._rate_attr, None)
        ):
            if spec.get("key") == self._detail_key:
                return cast(dict[Any, Any], spec)
        return None

    @property
    def available(self) -> bool:
        spec = self._spec()
        client = getattr(self._coord, "client", None)
        return (
            super().available
            and bool(getattr(self._coord, "pricing_edits_enabled", True))
            and spec is not None
            and isinstance((spec.get("attributes") or {}).get("tariff_locator"), dict)
            and callable(getattr(client, "site_tariff", None))
            and callable(getattr(client, "site_tariff_update", None))
        )

    @property
    def native_value(self) -> float | None:
        spec = self._spec()
        if spec is None:
            return None
        value = spec.get("state")
        return float(value) if value is not None else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        hass = getattr(self, "hass", None)
        currency = getattr(getattr(hass, "config", None), "currency", None)
        if isinstance(currency, str) and currency.strip():
            return f"{currency.strip()}/{UnitOfEnergy.KILO_WATT_HOUR}"
        spec = self._spec()
        if spec is None:
            return None
        return spec.get("unit")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        spec = self._spec()
        if spec is None:
            return {}
        return dict(spec.get("attributes") or {})

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "envoy")
        if info is not None:
            return info
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"site:{self._coord.site_id}")},
            manufacturer="Enphase",
        )

    async def async_set_native_value(self, value: float) -> None:
        spec = self._spec()
        locator = (spec.get("attributes") or {}).get("tariff_locator") if spec else None
        await self._coord.tariff_runtime.async_set_tariff_rate(locator, value)
