"""Select entities for Enphase charge modes and schedule editors."""

from __future__ import annotations

import asyncio
import json

import aiohttp
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SchedulerUnavailable
from .battery_schedule_editor import (
    BatteryScheduleEditorEntity,
    NEW_SCHEDULE_OPTION,
    battery_schedule_type_label,
    battery_schedule_type_options,
    battery_scheduler_enabled,
)
from .ac_battery_support import (
    AC_BATTERY_SOC_OPTIONS,
    ac_battery_control_available,
    ac_battery_device_info,
    ac_battery_entities_available,
    ac_battery_soc_option_label,
)
from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity
from .entity_cleanup import prune_managed_entities
from .evse_schedule_editor import (
    EvseScheduleEditorEntity,
    NEW_SCHEDULE_OPTION as EVSE_NEW_SCHEDULE_OPTION,
    evse_schedule_create_label,
    evse_schedule_editor_active,
)
from .labels import (
    CHARGE_MODE_LABELS,
    battery_profile_label,
    battery_schedule_create_label,
    charge_mode_label,
)
from .runtime_helpers import (
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data
from .service_validation import raise_translated_service_validation

PARALLEL_UPDATES = 0


def _smart_charging_context(coord: EnphaseCoordinator, sn: str | None = None) -> bool:
    if sn is None:
        return False
    try:
        data = (coord.data or {}).get(sn, {})
    except Exception:
        data = {}
    for key in ("charge_mode_pref", "charge_mode"):
        try:
            value = str(data.get(key) or "").strip().upper()
        except Exception:
            value = ""
        if value == "SMART_CHARGING":
            return True
    cache_entry = getattr(coord, "_charge_mode_cache", {}).get(sn)
    if cache_entry:
        try:
            return str(cache_entry[0]).strip().upper() == "SMART_CHARGING"
        except Exception:
            return False
    battery_profile_pref = getattr(
        coord, "_battery_profile_charge_mode_preference", None
    )
    if callable(battery_profile_pref):
        try:
            return battery_profile_pref(sn) == "SMART_CHARGING"
        except Exception:
            return False
    return False


def _explicit_charge_mode(coord: EnphaseCoordinator, sn: str) -> str | None:
    try:
        data = (coord.data or {}).get(sn, {})
    except Exception:
        data = {}
    for key in ("charge_mode_pref", "charge_mode"):
        normalizer = getattr(coord, "_normalize_charge_mode_preference", None)
        raw = data.get(key) if isinstance(data, dict) else None
        value = normalizer(raw) if callable(normalizer) else raw
        if value is not None:
            return str(value)
    cached = getattr(coord, "_cached_charge_mode_preference", None)
    value = cached(sn) if callable(cached) else None
    return str(value) if value is not None else None


def _solar_mode(coord: EnphaseCoordinator, sn: str | None = None) -> tuple[str, str]:
    if _smart_charging_context(coord, sn):
        # The Enphase UI can present the same solar option as Smart Charging
        # when battery profile preferences are active.
        return "SMART_CHARGING", charge_mode_label("SMART_CHARGING", hass=coord.hass)
    return "GREEN_CHARGING", charge_mode_label("GREEN_CHARGING", hass=coord.hass)


def _english_charge_mode_label(mode: str) -> str | None:
    key = str(mode).strip().lower()
    aliases = {
        "manual": "manual_charging",
        "manual_charging": "manual_charging",
        "scheduled": "scheduled_charging",
        "scheduled_charging": "scheduled_charging",
        "green": "green_charging",
        "green_charging": "green_charging",
        "smart": "smart_charging",
        "smart_charging": "smart_charging",
    }
    normalized = aliases.get(key)
    if normalized is None:
        return None
    return CHARGE_MODE_LABELS.get(normalized)


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    return has_encharge is not False


def _battery_write_access_confirmed(coord: EnphaseCoordinator) -> bool:
    confirmed = getattr(coord, "battery_write_access_confirmed", None)
    if confirmed is not None:
        return bool(confirmed)
    owner = getattr(coord, "battery_user_is_owner", None)
    installer = getattr(coord, "battery_user_is_installer", None)
    return owner is True or installer is True


def _battery_write_access_explicitly_denied(coord: EnphaseCoordinator) -> bool:
    if getattr(coord, "battery_write_access_confirmed", None) is True:
        return False
    return (
        getattr(coord, "battery_user_is_owner", None) is False
        and getattr(coord, "battery_user_is_installer", None) is False
    )


def _retain_system_profile(coord: EnphaseCoordinator) -> bool:
    if not _site_has_battery(coord):
        return False
    if not _type_available(coord, "envoy"):
        return False
    available = getattr(coord, "battery_profile_selection_available", None)
    if available is not None:
        return bool(available and not _battery_write_access_explicitly_denied(coord))
    if not getattr(coord, "battery_controls_available", False):
        return False
    if _battery_write_access_explicitly_denied(coord):
        return False
    return True


def _retain_ac_battery_target_soc(coord: EnphaseCoordinator) -> bool:
    return ac_battery_control_available(coord)


def _retain_battery_schedule_editor(
    coord: EnphaseCoordinator, entry: EnphaseConfigEntry
) -> bool:
    client = getattr(coord, "client", None)
    return (
        battery_scheduler_enabled(entry)
        and _site_has_battery(coord)
        and _type_available(coord, "encharge")
        and callable(getattr(client, "battery_schedules", None))
        and callable(getattr(client, "create_battery_schedule", None))
        and callable(getattr(client, "update_battery_schedule", None))
        and callable(getattr(client, "delete_battery_schedule", None))
    )


def _parse_scheduler_error(message: str) -> tuple[str | None, str | None]:
    if not message:
        return None, None
    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("errorMessageCode")
    display = error.get("displayMessage") or error.get("additionalInfo")
    return (str(code) if code else None, str(display) if display else None)


def _scheduler_error_context(
    err: aiohttp.ClientResponseError,
) -> tuple[str | None, str | None]:
    context = getattr(err, "enphase_scheduler_error", None)
    if isinstance(context, dict):
        code = context.get("code")
        display = context.get("display")
        return (str(code) if code else None, str(display) if display else None)
    return _parse_scheduler_error(err.message)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    ent_reg = er.async_get(hass)
    known_serials: set[str] = set()
    site_entity_added = False
    ac_battery_select_added = False
    battery_schedule_editor_added = False

    def _system_profile_unique_id() -> str:
        return f"{DOMAIN}_site_{coord.site_id}_system_profile"

    def _battery_schedule_select_unique_id() -> str:
        return f"{DOMAIN}_site_{coord.site_id}_battery_schedule_selected"

    def _battery_new_schedule_type_unique_id() -> str:
        return f"{DOMAIN}_site_{coord.site_id}_battery_new_schedule_type"

    def _charge_mode_unique_id(sn: str) -> str:
        return f"{DOMAIN}_{sn}_charge_mode_select"

    def _evse_schedule_select_unique_id(sn: str) -> str:
        return f"{DOMAIN}_{sn}_schedule_selected"

    @callback
    def _async_sync_site_entities() -> None:
        nonlocal site_entity_added
        nonlocal ac_battery_select_added
        nonlocal battery_schedule_editor_added
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        retain_system_profile = _retain_system_profile(coord)
        retain_ac_battery_target_soc = _retain_ac_battery_target_soc(coord)
        retain_battery_schedule_editor = _retain_battery_schedule_editor(coord, entry)
        if not site_entity_added and retain_system_profile:
            async_add_entities([SystemProfileSelect(coord)], update_before_add=False)
            site_entity_added = True
        if not ac_battery_select_added and retain_ac_battery_target_soc:
            async_add_entities(
                [AcBatteryTargetStateOfChargeSelect(coord)], update_before_add=False
            )
            ac_battery_select_added = True
        if not battery_schedule_editor_added and retain_battery_schedule_editor:
            async_add_entities(
                [
                    BatteryScheduleSelect(coord, entry),
                    BatteryNewScheduleTypeSelect(coord, entry),
                ],
                update_before_add=False,
            )
            battery_schedule_editor_added = True
        if not inventory_ready:
            return
        # Site-level selects are dynamic because BatteryConfig permissions and
        # AC Battery support are learned after setup.
        system_profile_loaded = (
            site_entity_added
            and _site_has_battery(coord)
            and _type_available(coord, "envoy")
        )
        ac_battery_target_soc_loaded = (
            ac_battery_select_added and ac_battery_entities_available(coord)
        )
        battery_schedule_editor_loaded = (
            battery_schedule_editor_added
            and _site_has_battery(coord)
            and _type_available(coord, "encharge")
        )
        prune_managed_entities(
            ent_reg,
            entry.entry_id,
            domain="select",
            active_unique_ids={
                unique_id
                for unique_id, retained in (
                    (
                        _system_profile_unique_id(),
                        retain_system_profile or system_profile_loaded,
                    ),
                    (
                        f"{DOMAIN}_site_{coord.site_id}_ac_battery_target_state_of_charge",
                        retain_ac_battery_target_soc or ac_battery_target_soc_loaded,
                    ),
                    (
                        _battery_schedule_select_unique_id(),
                        retain_battery_schedule_editor
                        or battery_schedule_editor_loaded,
                    ),
                    (
                        _battery_new_schedule_type_unique_id(),
                        retain_battery_schedule_editor
                        or battery_schedule_editor_loaded,
                    ),
                )
                if retained
            },
            is_managed=lambda unique_id: unique_id
            in {
                _system_profile_unique_id(),
                f"{DOMAIN}_site_{coord.site_id}_ac_battery_target_state_of_charge",
                _battery_schedule_select_unique_id(),
                _battery_new_schedule_type_unique_id(),
            },
        )

    @callback
    def _async_sync_chargers() -> None:
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        current_serials = {sn for sn in coord.iter_serials() if sn}
        serials = [sn for sn in current_serials if sn not in known_serials]
        if not serials:
            entities: list[SelectEntity] = []
        else:
            entities = []
            for sn in serials:
                entities.append(ChargeModeSelect(coord, sn))
                if evse_schedule_editor_active(coord, entry):
                    entities.append(EvseScheduleSelect(coord, entry, sn))
        if entities:
            async_add_entities(entities, update_before_add=False)
        known_serials.intersection_update(current_serials)
        known_serials.update(serials)
        if not inventory_ready:
            return
        active_unique_ids = {_charge_mode_unique_id(sn) for sn in current_serials}
        if evse_schedule_editor_active(coord, entry):
            active_unique_ids.update(
                _evse_schedule_select_unique_id(sn) for sn in current_serials
            )
        prune_managed_entities(
            ent_reg,
            entry.entry_id,
            domain="select",
            active_unique_ids=active_unique_ids,
            is_managed=lambda unique_id: (
                unique_id.endswith("_charge_mode_select")
                or (
                    unique_id.endswith("_schedule_selected")
                    and not unique_id.startswith(f"{DOMAIN}_site_")
                )
            ),
        )

    topology_listener = getattr(coord, "async_add_topology_listener", None)
    generic_listener = getattr(coord, "async_add_listener", None)
    if callable(generic_listener):
        entry.async_on_unload(generic_listener(_async_sync_site_entities))
    if callable(topology_listener):
        entry.async_on_unload(topology_listener(_async_sync_chargers))
    elif callable(generic_listener):
        entry.async_on_unload(generic_listener(_async_sync_chargers))
    _async_sync_site_entities()
    _async_sync_chargers()


class SystemProfileSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "system_profile"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_system_profile"

    @property
    def options(self) -> list[str]:
        labels = self._coord.battery_profile_option_labels
        return [
            labels[key]
            for key in self._coord.battery_profile_option_keys
            if key in labels
        ]

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if not _type_available(self._coord, "envoy"):
            return False
        available = getattr(self._coord, "battery_profile_selection_available", None)
        if available is None:
            if not getattr(self._coord, "battery_controls_available", False):
                return False
            owner = getattr(self._coord, "battery_user_is_owner", None)
            installer = getattr(self._coord, "battery_user_is_installer", None)
            if owner is False and installer is False:
                return False
        elif not available:
            return False
        if _battery_write_access_explicitly_denied(self._coord):
            return False
        return bool(self.options)

    @property
    def current_option(self) -> str | None:
        selected = self._coord.battery_selected_profile
        if not selected:
            return None
        return battery_profile_label(
            selected,
            hass=getattr(self, "hass", None) or self._coord.hass,
        )

    async def async_select_option(self, option: str) -> None:
        labels = self._coord.battery_profile_option_labels
        selected_key = None
        for key, label in labels.items():
            if label == option:
                selected_key = key
                break
        if selected_key is None:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="exceptions.selected_system_profile_unavailable",
                message="Selected system profile is not available.",
            )
        try:
            await self._coord.battery_runtime.async_set_system_profile(selected_key)
        except ServiceValidationError:
            raise
        except aiohttp.ClientResponseError as err:
            if err.status == 403:
                raise_translated_service_validation(
                    translation_domain=DOMAIN,
                    translation_key="exceptions.system_profile_update_forbidden",
                    message=(
                        "System profile update was rejected by Enphase "
                        "(HTTP 403 Forbidden)."
                    ),
                )
            if err.status == 401:
                raise_translated_service_validation(
                    translation_domain=DOMAIN,
                    translation_key="exceptions.system_profile_update_unauthorized",
                    message=(
                        "System profile update could not be authenticated. "
                        "Reauthenticate and try again."
                    ),
                )
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="exceptions.system_profile_update_failed",
                message="System profile update failed.",
            )
        except aiohttp.ClientError:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="exceptions.system_profile_update_network",
                message="System profile update failed due to a network error. Try again.",
            )
        except asyncio.TimeoutError:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="exceptions.system_profile_update_timeout",
                message="System profile update timed out. Try again.",
            )

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "envoy")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class _BatteryScheduleEditorSelect(BatteryScheduleEditorEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(coord, entry)
        self._attr_has_entity_name = True

    @property
    def available(self) -> bool:  # type: ignore[override]
        return (
            super().available
            and battery_scheduler_enabled(self._entry)
            and _retain_battery_schedule_editor(self._coord, self._entry)
            and self._editor is not None
        )

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
        )


class BatteryScheduleSelect(_BatteryScheduleEditorSelect):
    _attr_translation_key = "battery_schedule_selected"
    _attr_icon = "mdi:calendar-edit"

    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_battery_schedule_selected"
        )

    @property
    def options(self) -> list[str]:
        if self._editor is None:
            return []
        hass = getattr(self, "hass", None) or self._coord.hass
        return [
            *self._editor.option_label_by_schedule_id(hass=hass).values(),
            battery_schedule_create_label(hass=hass),
        ]

    @property
    def current_option(self) -> str | None:
        if self._editor is None:
            return None
        hass = getattr(self, "hass", None) or self._coord.hass
        if self._editor.current_selection == NEW_SCHEDULE_OPTION:
            return battery_schedule_create_label(hass=hass)
        selected_schedule = self._editor.get_schedule(self._editor.current_selection)
        if selected_schedule is None:
            return None
        return self._editor.option_label_by_schedule_id(hass=hass).get(
            selected_schedule.schedule_id
        )

    async def async_select_option(self, option: str) -> None:
        if self._editor is not None:
            hass = getattr(self, "hass", None) or self._coord.hass
            selected = (
                NEW_SCHEDULE_OPTION
                if option == battery_schedule_create_label(hass=hass)
                else self._editor.schedule_id_for_option_label(option, hass=hass)
                or option
            )
            self._editor.select_schedule(selected)


class BatteryNewScheduleTypeSelect(_BatteryScheduleEditorSelect):
    _attr_translation_key = "battery_new_schedule_type"
    _attr_icon = "mdi:calendar-plus"

    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_battery_new_schedule_type"
        )

    @property
    def options(self) -> list[str]:
        if self._editor is None:
            return []
        hass = getattr(self, "hass", None) or self._coord.hass
        if not self._editor.is_creating:
            current = battery_schedule_type_label(
                self._editor.edit.schedule_type,
                hass=hass,
            )
            return [current] if current else []
        return [label for _key, label in battery_schedule_type_options(hass=hass)]

    @property
    def available(self) -> bool:  # type: ignore[override]
        return super().available and self._editor is not None

    @property
    def current_option(self) -> str | None:
        if self._editor is None:
            return None
        hass = getattr(self, "hass", None) or self._coord.hass
        return battery_schedule_type_label(self._editor.edit.schedule_type, hass=hass)

    async def async_select_option(self, option: str) -> None:
        if self._editor is None or not self._editor.is_creating:
            return
        hass = getattr(self, "hass", None) or self._coord.hass
        for schedule_type, label in battery_schedule_type_options(hass=hass):
            if label == option:
                self._editor.set_new_schedule_type(schedule_type)
                return


class EvseScheduleSelect(EvseScheduleEditorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "evse_schedule_selected"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-edit"

    def __init__(
        self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry, sn: str
    ) -> None:
        super().__init__(coord, entry, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_schedule_selected"

    @property
    def available(self) -> bool:  # type: ignore[override]
        return (
            super().available
            and evse_schedule_editor_active(self._coord, self._entry)
            and self._editor is not None
        )

    @property
    def options(self) -> list[str]:
        if self._editor is None:
            return []
        hass = getattr(self, "hass", None) or self._coord.hass
        return [
            *self._editor.option_label_by_slot_id(self._sn).values(),
            evse_schedule_create_label(hass=hass),
        ]

    @property
    def current_option(self) -> str | None:
        if self._editor is None:
            return None
        hass = getattr(self, "hass", None) or self._coord.hass
        if self._editor.current_selection(self._sn) == EVSE_NEW_SCHEDULE_OPTION:
            return evse_schedule_create_label(hass=hass)
        selected_schedule = self._editor.get_schedule(
            self._sn, self._editor.current_selection(self._sn)
        )
        if selected_schedule is None:
            return None
        return self._editor.option_label_by_slot_id(self._sn).get(
            selected_schedule.slot_id
        )

    async def async_select_option(self, option: str) -> None:
        if self._editor is None:
            return
        hass = getattr(self, "hass", None) or self._coord.hass
        selected = (
            EVSE_NEW_SCHEDULE_OPTION
            if option == evse_schedule_create_label(hass=hass)
            else self._editor.slot_id_for_option_label(self._sn, option) or option
        )
        self._editor.select_schedule(self._sn, selected)


class ChargeModeSelect(EnphaseBaseEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charge_mode"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charge_mode_select"

    @property
    def options(self) -> list[str]:
        _solar_mode_key, solar_label = _solar_mode(self._coord, self._sn)
        hass = getattr(self, "hass", None) or self._coord.hass
        return [
            charge_mode_label("MANUAL_CHARGING", hass=hass),
            charge_mode_label("SCHEDULED_CHARGING", hass=hass),
            solar_label,
        ]

    @property
    def available(self) -> bool:  # type: ignore[override]
        return super().available and self._coord.scheduler_available

    @property
    def current_option(self) -> str | None:
        resolve_pref = getattr(self._coord, "_resolve_charge_mode_pref", None)
        val = resolve_pref(self._sn) if callable(resolve_pref) else None
        if not val:
            return None
        if val == "MANUAL_CHARGING":
            return charge_mode_label(
                val, hass=getattr(self, "hass", None) or self._coord.hass
            )
        if val == "SCHEDULED_CHARGING":
            return charge_mode_label(
                val, hass=getattr(self, "hass", None) or self._coord.hass
            )
        if val in {"GREEN_CHARGING", "SMART_CHARGING"}:
            return _solar_mode(self._coord, self._sn)[1]
        return None

    async def async_select_option(self, option: str) -> None:
        if not self._coord.scheduler_available:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="exceptions.scheduler_service_unavailable",
                message=(
                    "Charging mode selection is unavailable while the Enphase "
                    "scheduler service is down."
                ),
            )
        hass = getattr(self, "hass", None) or self._coord.hass
        solar_mode_key, solar_label = _solar_mode(self._coord, self._sn)
        manual_label = charge_mode_label("MANUAL_CHARGING", hass=hass)
        scheduled_label = charge_mode_label("SCHEDULED_CHARGING", hass=hass)
        option_map: dict[str, str] = {}
        for label, mode in (
            (manual_label, "MANUAL_CHARGING"),
            (scheduled_label, "SCHEDULED_CHARGING"),
            (solar_label, solar_mode_key),
            (charge_mode_label("GREEN_CHARGING", hass=hass), solar_mode_key),
            (charge_mode_label("SMART_CHARGING", hass=hass), solar_mode_key),
            (_english_charge_mode_label("GREEN_CHARGING"), solar_mode_key),
            (_english_charge_mode_label("SMART_CHARGING"), solar_mode_key),
        ):
            if label:
                option_map[label] = mode
        mode = option_map.get(option)
        if mode is None:
            raise ServiceValidationError(
                "Selected charging mode is not available.",
                translation_domain=DOMAIN,
                translation_key="charge_mode_invalid_option",
            )
        previous_mode = _explicit_charge_mode(self._coord, self._sn)
        if previous_mode == mode:
            return
        try:
            await self._coord.evse_runtime.async_set_charge_mode(
                self._sn, mode, previous_mode=previous_mode
            )
        except SchedulerUnavailable:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="exceptions.scheduler_service_unavailable",
                message=(
                    "Charging mode selection is unavailable while the Enphase "
                    "scheduler service is down."
                ),
            )
        except aiohttp.ClientResponseError as err:
            code, display = _scheduler_error_context(err)
            if err.status == 400 and (
                code == "iqevc_sch_10031"
                or (display and "No Schedules enabled" in display)
            ):
                # Scheduler returns this opaque code when Scheduled Charging is
                # selected before any schedule is enabled.
                raise ServiceValidationError(
                    "Enable at least one schedule before selecting Scheduled charging.",
                    translation_domain=DOMAIN,
                    translation_key="exceptions.schedule_required",
                )
            raise


class AcBatteryTargetStateOfChargeSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "ac_battery_target_state_of_charge"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_ac_battery_target_state_of_charge"
        )

    @property
    def suggested_object_id(self) -> str | None:
        return "ac_battery_target_state_of_charge"

    @property
    def options(self) -> list[str]:
        return [label for _value, label in AC_BATTERY_SOC_OPTIONS]

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if getattr(self._coord, "battery_has_acb", None) is not True:
            return False
        return ac_battery_control_available(self._coord)

    @property
    def current_option(self) -> str | None:
        return ac_battery_soc_option_label(
            self._coord.ac_battery_selected_sleep_min_soc
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "selected_sleep_min_soc": self._coord.ac_battery_selected_sleep_min_soc,
        }

    async def async_select_option(self, option: str) -> None:
        selected_value = None
        for value, label in AC_BATTERY_SOC_OPTIONS:
            if label == option:
                selected_value = value
                break
        if selected_value is None:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key=(
                    "exceptions.selected_ac_battery_target_state_of_charge_unavailable"
                ),
                message="Selected AC Battery target state of charge is not available.",
            )
        await self._coord.async_set_ac_battery_target_soc(selected_value)

    @property
    def device_info(self) -> DeviceInfo:
        return ac_battery_device_info(self._coord)
