"""Switch entities for Enphase charger, battery, and schedule controls."""

from __future__ import annotations

import logging
import re
import time

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ac_battery_support import (
    ac_battery_control_available,
    ac_battery_device_info,
    ac_battery_entities_available,
)
from .api import AuthSettingsUnavailable
from .battery_schedule_editor import (
    BatteryScheduleEditorEntity,
    DAY_ORDER,
    battery_scheduler_enabled,
)
from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity, battery_schedule_extra_state_attributes
from .entity_cleanup import prune_managed_entities
from .evse_schedule_editor import (
    EvseScheduleEditorEntity,
    DAY_ORDER as EVSE_DAY_ORDER,
    evse_schedule_editor_active,
)
from .evse_runtime import FAST_TOGGLE_POLL_HOLD_S
from .log_redaction import redact_identifier
from .runtime_helpers import (
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data
from .service_validation import raise_translated_service_validation

PARALLEL_UPDATES = 0
_LOGGER = logging.getLogger(__name__)
_AUTO_SUFFIX_RE = re.compile(r"^\d+$")


def _write_state_if_available(entity: SwitchEntity) -> None:
    """Push the local entity state when coordinator fields changed synchronously."""

    if getattr(entity, "hass", None) is None or not getattr(entity, "entity_id", None):
        return
    entity.async_write_ha_state()


def _effective_evse_toggle_state(
    coord: EnphaseCoordinator,
    attr_name: str,
    serial: str,
    current_value: object,
) -> bool | None:
    """Return a short-lived effective EVSE toggle state while writes settle."""

    effective = current_value if isinstance(current_value, bool) else None
    pending = getattr(coord, attr_name, None)
    if not isinstance(pending, dict):
        return effective
    serial_key = str(serial)
    pending_entry = pending.get(serial_key)
    if not pending_entry:
        return effective
    try:
        pending_value, expires_at = pending_entry
    except (TypeError, ValueError):
        pending.pop(serial_key, None)
        return effective
    if effective is not None and effective == bool(pending_value):
        pending.pop(serial_key, None)
        return effective
    try:
        if time.monotonic() >= float(expires_at):
            pending.pop(serial_key, None)
            return effective
    except Exception:  # noqa: BLE001
        pending.pop(serial_key, None)
        return effective
    return bool(pending_value)


def _pending_charging_state(coord: EnphaseCoordinator, serial: str) -> bool | None:
    """Return an in-flight EVSE charging target while start/stop settles."""

    pending = getattr(coord, "_pending_charging", {}).get(str(serial))
    if not pending:
        return None
    try:
        target_state, expires_at = pending
    except (TypeError, ValueError):
        return None
    try:
        if time.monotonic() > float(expires_at):
            getattr(coord, "_pending_charging", {}).pop(str(serial), None)
            return None
    except Exception:  # noqa: BLE001
        return None
    return bool(target_state)


def _effective_storm_guard_state(coord: EnphaseCoordinator) -> str | None:
    """Return the effective Storm Guard state, including pending writes."""

    if getattr(coord, "storm_guard_update_pending", False):
        pending_state = getattr(coord, "_storm_guard_pending_state", None)
        if isinstance(pending_state, str) and pending_state:
            return pending_state
    return getattr(coord, "storm_guard_state", None)


def _is_disabled_by_integration(disabled_by: object) -> bool:
    if disabled_by is None:
        return False
    return getattr(disabled_by, "value", disabled_by) == "integration"


def _reenable_integration_disabled_entity(
    ent_reg: er.EntityRegistry, *, domain: str, unique_id: str
) -> None:
    entity_id = ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
    if entity_id is None:
        return
    reg_entry = ent_reg.async_get(entity_id)
    if reg_entry is None:
        return
    if not _is_disabled_by_integration(getattr(reg_entry, "disabled_by", None)):
        return
    ent_reg.async_update_entity(entity_id, disabled_by=None)


def _switch_entity_id_migrations(coord: EnphaseCoordinator) -> dict[str, str]:
    return {
        f"{DOMAIN}_site_{coord.site_id}_charge_from_grid_schedule": (
            "switch.charge_from_grid_schedule"
        )
    }


def _migrate_storm_guard_evse_entity_id(current_entity_id: str) -> str | None:
    """Return canonical EVSE storm guard entity_id preserving numeric suffixes."""

    # Home Assistant may append numeric suffixes after entity collisions; keep
    # those user-visible IDs stable during the spelling migration.
    base_entity_id = current_entity_id
    numeric_suffix = ""
    base, _, suffix = current_entity_id.rpartition("_")
    if base and _AUTO_SUFFIX_RE.fullmatch(suffix):
        base_entity_id = base
        numeric_suffix = f"_{suffix}"
    if not base_entity_id.endswith("_storm_guard_ev_charge"):
        return None
    return (
        f"{base_entity_id[: -len('_storm_guard_ev_charge')]}"
        f"_storm_guard_evse_charge{numeric_suffix}"
    )


def _migrated_switch_entity_id(
    current_entity_id: str, target_entity_id: str
) -> str | None:
    """Return canonical ID target for migration, preserving numeric suffixes."""
    if current_entity_id == target_entity_id:
        return None

    target_prefix = f"{target_entity_id}_"
    if current_entity_id.startswith(target_prefix):
        suffix = current_entity_id[len(target_prefix) :]
        if _AUTO_SUFFIX_RE.fullmatch(suffix):
            return None

    base, _, suffix = current_entity_id.rpartition("_")
    if base and _AUTO_SUFFIX_RE.fullmatch(suffix):
        return f"{target_entity_id}_{suffix}"
    return target_entity_id


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
    return False


def _battery_write_access_explicitly_denied(coord: EnphaseCoordinator) -> bool:
    if getattr(coord, "battery_write_access_confirmed", None) is True:
        return False
    return (
        getattr(coord, "battery_user_is_owner", None) is False
        and getattr(coord, "battery_user_is_installer", None) is False
    )


def _storm_guard_visible(coord: EnphaseCoordinator) -> bool:
    show_storm_guard = getattr(coord, "battery_show_storm_guard", None)
    return show_storm_guard is not False


def _charge_from_grid_retained(coord: EnphaseCoordinator) -> bool:
    if getattr(coord, "battery_system_task", None) is True:
        return False
    cfg_show = getattr(coord, "battery_cfg_control_show", None)
    if cfg_show is False:
        return False
    if (
        cfg_show is None
        and getattr(coord, "_battery_hide_charge_from_grid", None) is True
    ):
        return False
    if getattr(coord, "battery_cfg_control_locked", None) is True:
        return False
    return True


def _charge_from_grid_schedule_retained(coord: EnphaseCoordinator) -> bool:
    if not _charge_from_grid_retained(coord):
        return False
    show_day_schedule = getattr(coord, "battery_cfg_control_show_day_schedule", None)
    if show_day_schedule is False:
        return False
    force_supported = getattr(
        coord,
        "battery_cfg_control_force_schedule_supported",
        None,
    )
    return force_supported is not False


def _retained_site_switch_keys(
    coord: EnphaseCoordinator, entry: EnphaseConfigEntry | None = None
) -> set[str]:
    retained: set[str] = set()
    client = getattr(coord, "client", None)
    if (
        _site_has_battery(coord)
        and _type_available(coord, "envoy")
        and not _battery_write_access_explicitly_denied(coord)
        and _storm_guard_visible(coord)
    ):
        retained.add("storm_guard")
    if _type_available(
        coord, "encharge"
    ) and not _battery_write_access_explicitly_denied(coord):
        if getattr(coord, "savings_use_battery_switch_available", None) is not False:
            retained.add("savings_use_battery_after_peak")
        if _charge_from_grid_retained(coord):
            retained.add("charge_from_grid")
        if _charge_from_grid_schedule_retained(coord):
            retained.add("charge_from_grid_schedule")
        if getattr(coord, "discharge_to_grid_schedule_available", None) is not False:
            retained.add("discharge_to_grid_schedule")
        if (
            getattr(coord, "restrict_battery_discharge_schedule_supported", None)
            is not False
        ):
            retained.add("restrict_battery_discharge_schedule")
    if ac_battery_control_available(coord):
        retained.add("ac_battery_sleep_mode")
    if (
        battery_scheduler_enabled(entry)
        and _site_has_battery(coord)
        and _type_available(coord, "encharge")
        and callable(getattr(client, "battery_schedules", None))
        and callable(getattr(client, "create_battery_schedule", None))
        and callable(getattr(client, "update_battery_schedule", None))
        and callable(getattr(client, "delete_battery_schedule", None))
    ):
        for day_key, _ in DAY_ORDER:
            retained.add(f"battery_schedule_edit_{day_key}")
    return retained


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    ent_reg = er.async_get(hass)
    rename_by_unique = _switch_entity_id_migrations(coord)
    for registry_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        target_entity_id = rename_by_unique.get(registry_entry.unique_id)
        if target_entity_id is not None:
            migrated_entity_id = _migrated_switch_entity_id(
                registry_entry.entity_id, target_entity_id
            )
        elif getattr(registry_entry, "unique_id", "").endswith(
            "_storm_guard_evse_charge"
        ):
            migrated_entity_id = _migrate_storm_guard_evse_entity_id(
                registry_entry.entity_id
            )
        else:
            continue
        if migrated_entity_id is None:
            continue
        try:
            ent_reg.async_update_entity(
                registry_entry.entity_id,
                new_entity_id=migrated_entity_id,
            )
        except ValueError:
            _LOGGER.debug(
                "Could not rename switch during migration (%s -> %s)",
                redact_identifier(registry_entry.entity_id),
                redact_identifier(migrated_entity_id),
            )

    site_entity_keys: set[str] = set()
    known_serials: set[str] = set()
    known_green_battery: set[str] = set()
    known_app_auth: set[str] = set()
    known_evse_schedule_days: set[tuple[str, str]] = set()

    @callback
    def _async_sync_site_entities() -> None:
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        site_entities: list[SwitchEntity] = []
        retain_site_entity_keys = _retained_site_switch_keys(coord, entry)
        if (
            "storm_guard" in retain_site_entity_keys
            and "storm_guard" not in site_entity_keys
            and _site_has_battery(coord)
            and _type_available(coord, "envoy")
        ):
            site_entities.append(StormGuardSwitch(coord))
            site_entity_keys.add("storm_guard")
        if _site_has_battery(coord) and _type_available(coord, "encharge"):
            if (
                "savings_use_battery_after_peak" in retain_site_entity_keys
                and "savings_use_battery_after_peak" not in site_entity_keys
            ):
                site_entities.append(SavingsUseBatteryAfterPeakSwitch(coord))
                site_entity_keys.add("savings_use_battery_after_peak")
            if (
                "charge_from_grid" in retain_site_entity_keys
                and "charge_from_grid" not in site_entity_keys
            ):
                site_entities.append(ChargeFromGridSwitch(coord))
                site_entity_keys.add("charge_from_grid")
            if (
                "charge_from_grid_schedule" in retain_site_entity_keys
                and "charge_from_grid_schedule" not in site_entity_keys
            ):
                site_entities.append(ChargeFromGridScheduleSwitch(coord))
                site_entity_keys.add("charge_from_grid_schedule")
            if (
                "discharge_to_grid_schedule" in retain_site_entity_keys
                and "discharge_to_grid_schedule" not in site_entity_keys
            ):
                site_entities.append(DischargeToGridScheduleSwitch(coord))
                site_entity_keys.add("discharge_to_grid_schedule")
            if (
                "restrict_battery_discharge_schedule" in retain_site_entity_keys
                and "restrict_battery_discharge_schedule" not in site_entity_keys
            ):
                site_entities.append(RestrictBatteryDischargeScheduleSwitch(coord))
                site_entity_keys.add("restrict_battery_discharge_schedule")
        if (
            "ac_battery_sleep_mode" in retain_site_entity_keys
            and "ac_battery_sleep_mode" not in site_entity_keys
            and ac_battery_entities_available(coord)
        ):
            site_entities.append(AcBatterySleepModeSwitch(coord))
            site_entity_keys.add("ac_battery_sleep_mode")
        if _site_has_battery(coord) and _type_available(coord, "encharge"):
            for day_key, _ in DAY_ORDER:
                edit_key = f"battery_schedule_edit_{day_key}"
                if (
                    edit_key in retain_site_entity_keys
                    and edit_key not in site_entity_keys
                ):
                    _reenable_integration_disabled_entity(
                        ent_reg,
                        domain="switch",
                        unique_id=f"{DOMAIN}_site_{coord.site_id}_{edit_key}",
                    )
                    site_entities.append(
                        BatteryScheduleEditorDaySwitch(coord, entry, day_key=day_key)
                    )
                    site_entity_keys.add(edit_key)
        if site_entities:
            async_add_entities(site_entities, update_before_add=False)
        if not _site_has_battery(coord):
            site_entity_keys.difference_update(
                {
                    "storm_guard",
                    "savings_use_battery_after_peak",
                    "charge_from_grid",
                    "charge_from_grid_schedule",
                    "discharge_to_grid_schedule",
                    "restrict_battery_discharge_schedule",
                    *(f"battery_schedule_edit_{day_key}" for day_key, _ in DAY_ORDER),
                }
            )
        elif not _type_available(coord, "encharge"):
            site_entity_keys.difference_update(
                {
                    "savings_use_battery_after_peak",
                    "charge_from_grid",
                    "charge_from_grid_schedule",
                    "discharge_to_grid_schedule",
                    "restrict_battery_discharge_schedule",
                    *(f"battery_schedule_edit_{day_key}" for day_key, _ in DAY_ORDER),
                }
            )
        if not ac_battery_entities_available(coord):
            site_entity_keys.discard("ac_battery_sleep_mode")
        if not _type_available(coord, "envoy"):
            site_entity_keys.discard("storm_guard")
        if not inventory_ready:
            return
        prune_managed_entities(
            ent_reg,
            entry.entry_id,
            domain="switch",
            active_unique_ids={
                f"{DOMAIN}_site_{coord.site_id}_{key}" for key in site_entity_keys
            },
            is_managed=lambda unique_id: unique_id
            in {
                f"{DOMAIN}_site_{coord.site_id}_storm_guard",
                f"{DOMAIN}_site_{coord.site_id}_savings_use_battery_after_peak",
                f"{DOMAIN}_site_{coord.site_id}_charge_from_grid",
                f"{DOMAIN}_site_{coord.site_id}_charge_from_grid_schedule",
                f"{DOMAIN}_site_{coord.site_id}_discharge_to_grid_schedule",
                f"{DOMAIN}_site_{coord.site_id}_restrict_battery_discharge_schedule",
                f"{DOMAIN}_site_{coord.site_id}_ac_battery_sleep_mode",
                *(
                    f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_{day_key}"
                    for day_key, _ in DAY_ORDER
                ),
                *(
                    f"{DOMAIN}_site_{coord.site_id}_battery_new_schedule_{day_key}"
                    for day_key, _ in DAY_ORDER
                ),
            },
        )

    @callback
    def _async_sync_chargers() -> None:
        _async_sync_site_entities()
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        site_has_battery = _site_has_battery(coord)
        current_serials = {sn for sn in coord.iter_serials() if sn}
        serials = [sn for sn in current_serials if sn not in known_serials]
        entities: list[SwitchEntity] = []
        if serials:
            entities.extend(ChargingSwitch(coord, sn) for sn in serials)
            if (
                site_has_battery
                and _battery_write_access_confirmed(coord)
                and _storm_guard_visible(coord)
            ):
                entities.extend(StormGuardEvseSwitch(coord, sn) for sn in serials)
        data_source = coord.data or {}
        retain_green_battery: set[str] = set()
        retain_app_auth: set[str] = set()
        if isinstance(data_source, dict):
            if site_has_battery:
                for sn in coord.iter_serials():
                    if not sn:
                        continue
                    data = data_source.get(sn) or {}
                    if data.get("green_battery_supported") is True:
                        retain_green_battery.add(sn)
                    if sn in known_green_battery:
                        continue
                    if data.get("green_battery_supported") is True:
                        entities.append(GreenBatterySwitch(coord, sn))
                        known_green_battery.add(sn)
            for sn in coord.iter_serials():
                if not sn:
                    continue
                data = data_source.get(sn) or {}
                if data.get("app_auth_supported") is True:
                    retain_app_auth.add(sn)
                if sn in known_app_auth:
                    continue
                if data.get("app_auth_supported") is True:
                    entities.append(AppAuthenticationSwitch(coord, sn))
                    known_app_auth.add(sn)
        if evse_schedule_editor_active(coord, entry):
            for sn in current_serials:
                for day_key, _day in EVSE_DAY_ORDER:
                    key = (sn, day_key)
                    if key in known_evse_schedule_days:
                        continue
                    _reenable_integration_disabled_entity(
                        ent_reg,
                        domain="switch",
                        unique_id=f"{DOMAIN}_{sn}_schedule_edit_{day_key}",
                    )
                    entities.append(
                        EvseScheduleEditorDaySwitch(coord, entry, sn, day_key)
                    )
                    known_evse_schedule_days.add(key)
        if entities:
            async_add_entities(entities, update_before_add=False)
        known_serials.intersection_update(current_serials)
        known_serials.update(serials)
        known_green_battery.intersection_update(retain_green_battery)
        known_app_auth.intersection_update(retain_app_auth)
        known_evse_schedule_days.intersection_update(
            {
                (sn, day_key)
                for sn in current_serials
                for day_key, _day in EVSE_DAY_ORDER
            }
        )
        if not inventory_ready:
            return
        active_unique_ids = {f"{DOMAIN}_{sn}_charging_switch" for sn in current_serials}
        if site_has_battery and _storm_guard_visible(coord):
            active_unique_ids.update(
                f"{DOMAIN}_{sn}_storm_guard_evse_charge" for sn in current_serials
            )
        active_unique_ids.update(
            f"{DOMAIN}_{sn}_green_battery" for sn in retain_green_battery
        )
        active_unique_ids.update(
            f"{DOMAIN}_{sn}_app_authentication" for sn in retain_app_auth
        )
        if evse_schedule_editor_active(coord, entry):
            active_unique_ids.update(
                f"{DOMAIN}_{sn}_schedule_edit_{day_key}"
                for sn in current_serials
                for day_key, _day in EVSE_DAY_ORDER
            )
        prune_managed_entities(
            ent_reg,
            entry.entry_id,
            domain="switch",
            active_unique_ids=active_unique_ids,
            is_managed=lambda unique_id: (
                unique_id.endswith(
                    (
                        "_charging_switch",
                        "_storm_guard_evse_charge",
                        "_green_battery",
                        "_app_authentication",
                    )
                )
                or (
                    unique_id.endswith(
                        (
                            "_schedule_edit_mon",
                            "_schedule_edit_tue",
                            "_schedule_edit_wed",
                            "_schedule_edit_thu",
                            "_schedule_edit_fri",
                            "_schedule_edit_sat",
                            "_schedule_edit_sun",
                        )
                    )
                    and not unique_id.startswith(f"{DOMAIN}_site_")
                )
            ),
        )

    add_topology_listener = getattr(coord, "async_add_topology_listener", None)
    if callable(add_topology_listener):
        entry.async_on_unload(add_topology_listener(_async_sync_chargers))
    add_listener = getattr(coord, "async_add_listener", None)
    if callable(add_listener):
        entry.async_on_unload(add_listener(_async_sync_chargers))
    _async_sync_chargers()


class StormGuardSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "storm_guard"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_storm_guard"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if _battery_write_access_explicitly_denied(self._coord):
            return False
        if not _storm_guard_visible(self._coord):
            return False
        if not _type_available(self._coord, "envoy"):
            return False
        return self._coord.storm_guard_state is not None

    @property
    def is_on(self) -> bool:
        return _effective_storm_guard_state(self._coord) == "enabled"

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_storm_guard_enabled(True)
        _write_state_if_available(self)
        self._coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self._coord.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_storm_guard_enabled(False)
        _write_state_if_available(self)
        self._coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self._coord.async_request_refresh()

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "envoy")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class SavingsUseBatteryAfterPeakSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "savings_use_battery_after_peak"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_savings_use_battery_after_peak"
        )

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and not _battery_write_access_explicitly_denied(self._coord)
            and self._coord.savings_use_battery_switch_available
        )

    @property
    def is_on(self) -> bool:
        return bool(self._coord.savings_use_battery_after_peak)

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_savings_use_battery_after_peak(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_savings_use_battery_after_peak(False)

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class ChargeFromGridSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charge_from_grid"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_charge_from_grid"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and not _battery_write_access_explicitly_denied(self._coord)
            and self._coord.charge_from_grid_control_available
        )

    @property
    def is_on(self) -> bool:
        return bool(self._coord.battery_charge_from_grid_enabled)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return battery_schedule_extra_state_attributes(self._coord)

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_charge_from_grid(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_charge_from_grid(False)

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class ChargeFromGridScheduleSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charge_from_grid_schedule"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_charge_from_grid_schedule"
        )

    @property
    def suggested_object_id(self) -> str | None:
        return "charge_from_grid_schedule"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and not _battery_write_access_explicitly_denied(self._coord)
            and self._coord.charge_from_grid_force_schedule_available
        )

    @property
    def is_on(self) -> bool:
        return bool(self._coord.battery_charge_from_grid_schedule_enabled)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return battery_schedule_extra_state_attributes(
            self._coord,
            start_time=self._coord.battery_charge_from_grid_start_time,
            end_time=self._coord.battery_charge_from_grid_end_time,
            schedule_status=self._coord.battery_cfg_schedule_status,
            schedule_pending=self._coord.battery_cfg_schedule_pending,
            schedule_enabled=self._coord.battery_charge_from_grid_schedule_enabled,
            schedule_limit=self._coord.battery_cfg_schedule_limit,
        )

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_charge_from_grid_schedule_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_charge_from_grid_schedule_enabled(False)

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class _BaseBatteryScheduleSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coord: EnphaseCoordinator,
        *,
        unique_suffix: str,
        availability_attr: str,
        enabled_attr: str,
        setter_name: str,
        suggested_object_id: str,
    ) -> None:
        super().__init__(coord)
        self._coord = coord
        self._availability_attr = availability_attr
        self._enabled_attr = enabled_attr
        self._setter_name = setter_name
        self._suggested_object_id = suggested_object_id
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{unique_suffix}"

    @property
    def suggested_object_id(self) -> str | None:
        return self._suggested_object_id

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        return (
            _type_available(self._coord, "encharge")
            and not _battery_write_access_explicitly_denied(self._coord)
            and bool(getattr(self._coord, self._availability_attr, False))
        )

    @property
    def is_on(self) -> bool:
        return bool(getattr(self._coord, self._enabled_attr, None))

    def _extra_schedule_state_attributes(self) -> dict[str, object]:
        return {}

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return battery_schedule_extra_state_attributes(
            self._coord, **self._extra_schedule_state_attributes()
        )

    async def async_turn_on(self, **kwargs) -> None:
        await getattr(self._coord, self._setter_name)(True)

    async def async_turn_off(self, **kwargs) -> None:
        await getattr(self._coord, self._setter_name)(False)

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class DischargeToGridScheduleSwitch(_BaseBatteryScheduleSwitch):
    _attr_translation_key = "discharge_to_grid_schedule"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            unique_suffix="discharge_to_grid_schedule",
            availability_attr="discharge_to_grid_schedule_available",
            enabled_attr="battery_discharge_to_grid_schedule_enabled",
            setter_name="async_set_discharge_to_grid_schedule_enabled",
            suggested_object_id="discharge_to_grid_schedule",
        )

    def _extra_schedule_state_attributes(self) -> dict[str, object]:
        return {
            "start_time": self._coord.battery_discharge_to_grid_start_time,
            "end_time": self._coord.battery_discharge_to_grid_end_time,
            "schedule_status": self._coord.battery_dtg_schedule_status,
            "schedule_pending": self._coord.battery_dtg_schedule_pending,
            "schedule_enabled": self._coord.battery_discharge_to_grid_schedule_enabled,
            "schedule_limit": self._coord.battery_dtg_schedule_limit,
        }


class RestrictBatteryDischargeScheduleSwitch(_BaseBatteryScheduleSwitch):
    _attr_translation_key = "restrict_battery_discharge_schedule"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            unique_suffix="restrict_battery_discharge_schedule",
            availability_attr="restrict_battery_discharge_schedule_supported",
            enabled_attr="battery_restrict_battery_discharge_schedule_enabled",
            setter_name="async_set_restrict_battery_discharge_schedule_enabled",
            suggested_object_id="restrict_battery_discharge_schedule",
        )

    def _extra_schedule_state_attributes(self) -> dict[str, object]:
        return {
            "start_time": self._coord.battery_restrict_battery_discharge_start_time,
            "end_time": self._coord.battery_restrict_battery_discharge_end_time,
            "schedule_status": self._coord.battery_rbd_schedule_status,
            "schedule_pending": self._coord.battery_rbd_schedule_pending,
            "schedule_enabled": self._coord.battery_restrict_battery_discharge_schedule_enabled,
            "schedule_limit": self._coord.battery_rbd_schedule_limit,
        }


class AcBatterySleepModeSwitch(CoordinatorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "ac_battery_sleep_mode"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_ac_battery_sleep_mode"

    @property
    def suggested_object_id(self) -> str | None:
        return "ac_battery_sleep_mode"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if not ac_battery_control_available(self._coord):
            return False
        return self._coord.ac_battery_sleep_state is not None

    @property
    def is_on(self) -> bool:
        return self._coord.ac_battery_sleep_state in {"on", "pending", "mixed"}

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "sleep_state": self._coord.ac_battery_sleep_state,
            "pending": self._coord.ac_battery_control_pending,
            "last_command": getattr(self._coord, "_ac_battery_last_command", None),
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_ac_battery_sleep_mode(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_ac_battery_sleep_mode(False)

    @property
    def device_info(self) -> DeviceInfo:
        return ac_battery_device_info(self._coord)


class ChargingSwitch(EnphaseBaseEntity, RestoreEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charging"
    # Main feature of the device; let entity name equal device name
    _attr_name = None

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charging_switch"
        self._restored_state: bool | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            desired = last_state.state == STATE_ON
            self._restored_state = desired
            self._coord.set_desired_charging(self._sn, desired)
            if desired and not self.is_on:
                self._coord.kick_fast(60)
                try:
                    await self._coord.async_request_refresh()
                except Exception:  # noqa: BLE001
                    return
            self.async_write_ha_state()
        else:
            if self.available:
                self._coord.set_desired_charging(self._sn, self.is_on)
                self._restored_state = self.is_on

    @property
    def is_on(self) -> bool:
        if not self.available and self._restored_state is not None:
            return self._restored_state
        pending_state = _pending_charging_state(self._coord, self._sn)
        if pending_state is not None:
            return pending_state
        return bool(self.data.get("charging"))

    async def async_turn_on(self, **kwargs) -> None:
        try:
            result = await self._coord.async_start_charging(self._sn)
        except ServiceValidationError:
            self._schedule_failure_refresh()
            self._force_write_state()
            raise
        if isinstance(result, dict) and result.get("status") == "not_ready":
            self._schedule_failure_refresh()
            self._force_write_state()
            return
        _write_state_if_available(self)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_stop_charging(self._sn)
        _write_state_if_available(self)

    def _schedule_failure_refresh(self) -> None:
        self._coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        if self.hass is None:
            return
        self.hass.async_create_task(
            self._coord.async_request_refresh(), name=f"{DOMAIN}_switch_failure_refresh"
        )

    def _force_write_state(self) -> None:
        if self.hass is None or not self.entity_id:
            return
        prev_force = getattr(self, "_attr_force_update", False)
        self._attr_force_update = True
        try:
            self.async_write_ha_state()
        finally:
            self._attr_force_update = prev_force

    @callback
    def _handle_coordinator_update(self) -> None:
        self._restored_state = None
        super()._handle_coordinator_update()


class GreenBatterySwitch(EnphaseBaseEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "green_battery"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_green_battery"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if not self._coord.scheduler_available:
            return False
        if self.data.get("green_battery_supported") is not True:
            return False
        return self.data.get("green_battery_enabled") is not None

    @property
    def is_on(self) -> bool:
        effective = _effective_evse_toggle_state(
            self._coord,
            "_green_battery_pending",
            self._sn,
            self.data.get("green_battery_enabled"),
        )
        return bool(effective)

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.evse_runtime.async_set_green_battery_setting(
            self._sn, enabled=True
        )
        _write_state_if_available(self)

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.evse_runtime.async_set_green_battery_setting(
            self._sn, enabled=False
        )
        _write_state_if_available(self)


class AppAuthenticationSwitch(EnphaseBaseEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "app_authentication"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_app_authentication"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if not self._coord.auth_settings_available:
            return False
        return self.data.get("app_auth_supported") is True

    @property
    def is_on(self) -> bool:
        effective = _effective_evse_toggle_state(
            self._coord,
            "_app_auth_pending",
            self._sn,
            self.data.get("app_auth_enabled"),
        )
        return bool(effective)

    async def async_turn_on(self, **kwargs) -> None:
        try:
            await self._coord.evse_runtime.async_set_app_authentication(
                self._sn, enabled=True
            )
        except AuthSettingsUnavailable:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="auth_settings_service_unavailable",
                message=(
                    "Authentication settings are unavailable while the Enphase "
                    "service is down."
                ),
            )
        _write_state_if_available(self)

    async def async_turn_off(self, **kwargs) -> None:
        try:
            await self._coord.evse_runtime.async_set_app_authentication(
                self._sn, enabled=False
            )
        except AuthSettingsUnavailable:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="auth_settings_service_unavailable",
                message=(
                    "Authentication settings are unavailable while the Enphase "
                    "service is down."
                ),
            )
        _write_state_if_available(self)


class StormGuardEvseSwitch(EnphaseBaseEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "storm_guard_evse_charge"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_storm_guard_evse_charge"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if not _battery_write_access_confirmed(self._coord):
            return False
        if not _storm_guard_visible(self._coord):
            return False
        if self.data.get("storm_guard_state") is None:
            return False
        return self.data.get("storm_evse_enabled") is not None

    @property
    def is_on(self) -> bool:
        value = self._coord.storm_evse_enabled
        if value is None:
            value = self.data.get("storm_evse_enabled")
        return bool(value)

    async def async_turn_on(self, **kwargs) -> None:
        await self._coord.async_set_storm_evse_enabled(True)
        _write_state_if_available(self)
        self._coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self._coord.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self._coord.async_set_storm_evse_enabled(False)
        _write_state_if_available(self)
        self._coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await self._coord.async_request_refresh()


class BatteryScheduleEditorDaySwitch(BatteryScheduleEditorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coord: EnphaseCoordinator,
        entry: EnphaseConfigEntry,
        *,
        day_key: str,
    ) -> None:
        super().__init__(coord, entry)
        self._day_key = day_key
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_{day_key}"
        )
        self._attr_translation_key = f"battery_schedule_edit_{day_key}"

    @property
    def available(self) -> bool:  # type: ignore[override]
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
    def is_on(self) -> bool:
        if self._editor is None:
            return False
        return bool(self._editor.edit.days.get(self._day_key))

    async def async_turn_on(self, **kwargs) -> None:
        if self._editor is None:
            return
        self._editor.set_edit_day(self._day_key, True)

    async def async_turn_off(self, **kwargs) -> None:
        if self._editor is None:
            return
        self._editor.set_edit_day(self._day_key, False)

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class EvseScheduleEditorDaySwitch(EvseScheduleEditorEntity, SwitchEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coord: EnphaseCoordinator,
        entry: EnphaseConfigEntry,
        sn: str,
        day_key: str,
    ) -> None:
        super().__init__(coord, entry, sn)
        self._day_key = day_key
        self._attr_translation_key = f"evse_schedule_edit_{day_key}"
        self._attr_unique_id = f"{DOMAIN}_{sn}_schedule_edit_{day_key}"

    @property
    def available(self) -> bool:  # type: ignore[override]
        return (
            super().available
            and evse_schedule_editor_active(self._coord, self._entry)
            and self._editor is not None
        )

    @property
    def is_on(self) -> bool:
        if self._editor is None:
            return False
        return bool(self._editor.form_state(self._sn).days.get(self._day_key))

    async def async_turn_on(self, **kwargs) -> None:
        if self._editor is not None:
            self._editor.set_edit_day(self._sn, self._day_key, True)

    async def async_turn_off(self, **kwargs) -> None:
        if self._editor is not None:
            self._editor.set_edit_day(self._sn, self._day_key, False)
