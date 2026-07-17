"""Button entities for Enphase charger and battery service actions."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from homeassistant.components.button import ButtonEntity
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback as ha_callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .battery_schedule_editor import (
    BatteryScheduleRecord,
    BatteryScheduleEditorEntity,
    battery_schedule_option_label,
    battery_scheduler_enabled,
    days_list_from_editor,
)
from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity
from .entity_cleanup import prune_managed_entities
from .evse_schedule_editor import (
    EvseScheduleEditorEntity,
    NEW_SCHEDULE_OPTION as EVSE_NEW_SCHEDULE_OPTION,
    days_list_from_editor as evse_days_list_from_editor,
    evse_schedule_editor_active,
)
from .runtime_helpers import (
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
)
from .labels import battery_schedule_button_label
from .runtime_data import EnphaseConfigEntry, get_runtime_data

PARALLEL_UPDATES = 0

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])
callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    has_enpower = getattr(coord, "battery_has_enpower", None)
    if has_encharge is True or has_enpower is True:
        return True
    if has_encharge is False and has_enpower is False:
        return False
    return _type_available(coord, "encharge")


def _storm_guard_visible(coord: EnphaseCoordinator) -> bool:
    show_storm_guard = getattr(coord, "battery_show_storm_guard", None)
    return show_storm_guard is not False


def _retain_cancel_pending_profile_change(coord: EnphaseCoordinator) -> bool:
    return _type_available(coord, "envoy")


def _retain_storm_alert_opt_out(coord: EnphaseCoordinator) -> bool:
    return (
        _site_has_battery(coord)
        and _type_available(coord, "envoy")
        and _storm_guard_visible(coord)
    )


def _retain_battery_schedule_refresh_button(coord: EnphaseCoordinator) -> bool:
    client = getattr(coord, "client", None)
    return (
        _site_has_battery(coord)
        and _type_available(coord, "encharge")
        and callable(getattr(client, "battery_schedules", None))
    )


def _retain_battery_schedule_editor_buttons(coord: EnphaseCoordinator) -> bool:
    client = getattr(coord, "client", None)
    return (
        _retain_battery_schedule_refresh_button(coord)
        and callable(getattr(client, "create_battery_schedule", None))
        and callable(getattr(client, "update_battery_schedule", None))
        and callable(getattr(client, "delete_battery_schedule", None))
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    ent_reg = er.async_get(hass)
    known_serials: set[str] = set()
    site_entity_keys: set[str] = set()

    def _site_button_unique_id(key: str) -> str:
        return f"{DOMAIN}_site_{coord.site_id}_{key}"

    def _charger_button_unique_id(sn: str, action: str) -> str:
        return f"{DOMAIN}_{sn}_{action}"

    @callback
    def _async_sync_chargers() -> None:
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        site_entities: list[ButtonEntity] = []
        retain_site_entity_keys: set[str] = set()
        current_serials = {sn for sn in coord.iter_serials() if sn}
        if _retain_cancel_pending_profile_change(coord):
            retain_site_entity_keys.add("cancel_pending_profile_change")
        if "cancel_pending_profile_change" not in site_entity_keys and _type_available(
            coord, "envoy"
        ):
            site_entities.append(CancelPendingProfileChangeButton(coord))
            site_entity_keys.add("cancel_pending_profile_change")
        if _retain_storm_alert_opt_out(coord):
            retain_site_entity_keys.add("storm_alert_opt_out")
        if (
            "storm_alert_opt_out" not in site_entity_keys
            and _site_has_battery(coord)
            and _type_available(coord, "envoy")
            and _storm_guard_visible(coord)
        ):
            site_entities.append(StormAlertOptOutButton(coord))
            site_entity_keys.add("storm_alert_opt_out")
        scheduler_enabled = battery_scheduler_enabled(entry)
        if scheduler_enabled and _retain_battery_schedule_refresh_button(coord):
            retain_site_entity_keys.add("battery_force_refresh")
            if "battery_force_refresh" not in site_entity_keys:
                site_entities.append(BatteryForceRefreshButton(coord, entry))
                site_entity_keys.add("battery_force_refresh")
        if scheduler_enabled and _retain_battery_schedule_editor_buttons(coord):
            for key, entity in (
                ("battery_schedule_save", BatteryScheduleSaveButton(coord, entry)),
                (
                    "battery_schedule_delete",
                    BatteryScheduleDeleteButton(coord, entry),
                ),
            ):
                retain_site_entity_keys.add(key)
                if key not in site_entity_keys:
                    site_entities.append(entity)
                    site_entity_keys.add(key)
        if site_entities:
            async_add_entities(site_entities, update_before_add=False)
        serials = [sn for sn in current_serials if sn not in known_serials]
        if not serials:
            serial_entities: list[ButtonEntity] = []
        else:
            serial_entities = []
            for sn in serials:
                serial_entities.append(StartChargeButton(coord, sn))
                serial_entities.append(StopChargeButton(coord, sn))
                if evse_schedule_editor_active(coord, entry):
                    serial_entities.append(EvseScheduleRefreshButton(coord, entry, sn))
                    serial_entities.append(EvseScheduleSaveButton(coord, entry, sn))
                    serial_entities.append(EvseScheduleDeleteButton(coord, entry, sn))
        if serial_entities:
            async_add_entities(serial_entities, update_before_add=False)
        known_serials.intersection_update(current_serials)
        known_serials.update(serials)
        site_entity_keys.intersection_update(retain_site_entity_keys)

        if not inventory_ready:
            return

        # Prune only after inventory is ready so a transient discovery miss does
        # not remove user-customized entity registry entries.
        active_charger_unique_ids = set()
        for sn in current_serials:
            active_charger_unique_ids.update(
                {
                    _charger_button_unique_id(sn, "start_charging"),
                    _charger_button_unique_id(sn, "stop_charging"),
                }
            )
            if evse_schedule_editor_active(coord, entry):
                active_charger_unique_ids.update(
                    {
                        _charger_button_unique_id(sn, "schedule_refresh"),
                        _charger_button_unique_id(sn, "schedule_save"),
                        _charger_button_unique_id(sn, "schedule_delete"),
                    }
                )
        prune_managed_entities(
            ent_reg,
            entry.entry_id,
            domain="button",
            active_unique_ids={
                *(_site_button_unique_id(key) for key in retain_site_entity_keys),
                *active_charger_unique_ids,
            },
            is_managed=lambda unique_id: (
                unique_id
                in {
                    _site_button_unique_id("cancel_pending_profile_change"),
                    _site_button_unique_id("storm_alert_opt_out"),
                    _site_button_unique_id("battery_force_refresh"),
                    _site_button_unique_id("battery_schedule_save"),
                    _site_button_unique_id("battery_schedule_delete"),
                    _site_button_unique_id("battery_schedule_add"),
                }
                or unique_id.endswith(
                    (
                        "_start_charging",
                        "_stop_charging",
                        "_schedule_refresh",
                        "_schedule_save",
                        "_schedule_delete",
                    )
                )
            ),
        )

    unsubscribe = coord.async_add_listener(_async_sync_chargers)
    entry.async_on_unload(unsubscribe)
    _async_sync_chargers()


class CancelPendingProfileChangeButton(CoordinatorEntity, ButtonEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "cancel_pending_profile_change"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_cancel_pending_profile_change"
        )

    @property
    def available(self) -> bool:
        return cast(
            bool,
            (
                super().available
                and _type_available(self._coord, "envoy")
                and self._coord.battery_profile_pending
            ),
        )

    async def async_press(self) -> None:
        await self._coord.async_cancel_pending_profile_change()

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "envoy")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class StormAlertOptOutButton(CoordinatorEntity, ButtonEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "storm_alert_opt_out"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_storm_alert_opt_out"

    @property
    def available(self) -> bool:
        return (
            super().available
            and _site_has_battery(self._coord)
            and _type_available(self._coord, "envoy")
            and _storm_guard_visible(self._coord)
        )

    async def async_press(self) -> None:
        await self._coord.async_opt_out_all_storm_alerts()

    @property
    def device_info(self) -> DeviceInfo:
        info = _type_device_info(self._coord, "envoy")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class _BatteryScheduleButton(BatteryScheduleEditorEntity, ButtonEntity):  # type: ignore[misc]
    _attr_has_entity_name = True

    def __init__(
        self,
        coord: EnphaseCoordinator,
        entry: EnphaseConfigEntry,
        *,
        unique_suffix: str,
        translation_key: str,
        icon: str,
    ) -> None:
        super().__init__(coord, entry)
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{unique_suffix}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon

    @property
    def available(self) -> bool:
        return (
            super().available
            and battery_scheduler_enabled(self._entry)
            and _retain_battery_schedule_refresh_button(self._coord)
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

    def _notification_id(self) -> str:
        return f"{DOMAIN}_battery_schedule_result_{self._coord.site_id}"

    def _button_label(self, action: str) -> str:
        hass = getattr(self, "hass", None) or self._coord.hass
        return battery_schedule_button_label(action, hass=hass)

    def _schedule_result_label(self) -> str | None:
        if self._editor is None:
            return None
        hass = getattr(self, "hass", None) or self._coord.hass
        if not self._editor.is_creating and self._editor.edit.selected_schedule_id:
            schedule = self._editor.get_schedule(self._editor.edit.selected_schedule_id)
            if schedule is not None:
                return cast(
                    str | None, battery_schedule_option_label(schedule, hass=hass)
                )
        preview = BatteryScheduleRecord(
            schedule_id="preview",
            schedule_type=self._editor.edit.schedule_type,
            start_time=self._editor.edit.start_time,
            end_time=self._editor.edit.end_time,
            limit=int(self._editor.edit.limit),
            days=days_list_from_editor(self._editor.edit.days),
            timezone=(
                str(getattr(self._coord, "battery_timezone", None) or "").strip()
                or None
            ),
            enabled=True,
            schedule_status=None,
        )
        return cast(str | None, battery_schedule_option_label(preview, hass=hass))

    def _show_success_notification(self, *, action: str, body: str | None) -> None:
        message = str(body).strip() if isinstance(body, str) else ""
        if not message:
            return
        persistent_notification.async_create(
            self.hass,
            message,
            title=self._button_label(action),
            notification_id=self._notification_id(),
        )


class BatteryForceRefreshButton(_BatteryScheduleButton):
    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(
            coord,
            entry,
            unique_suffix="battery_force_refresh",
            translation_key="battery_schedule_refresh",
            icon="mdi:refresh",
        )

    async def async_press(self) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "force_refresh",
            {"config_entry_id": self._entry.entry_id},
            blocking=True,
        )


class BatteryScheduleSaveButton(_BatteryScheduleButton):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(
            coord,
            entry,
            unique_suffix="battery_schedule_save",
            translation_key="battery_schedule_save",
            icon="mdi:content-save",
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._editor is not None
            and _retain_battery_schedule_editor_buttons(self._coord)
            and bool(getattr(self._coord, "battery_write_access_confirmed", False))
            and (
                self._editor.is_creating
                or self._editor.edit.selected_schedule_id is not None
            )
        )

    async def async_press(self) -> None:
        if self._editor is None:
            return
        success_label = self._schedule_result_label()
        service = "add_schedule" if self._editor.is_creating else "update_schedule"
        selected_schedule = self._editor.get_schedule(
            self._editor.edit.selected_schedule_id
        )
        service_data: dict[str, object] = {
            "config_entry_id": self._entry.entry_id,
            "schedule_type": (
                selected_schedule.schedule_type
                if selected_schedule is not None
                else self._editor.edit.schedule_type
            ),
            "start_time": self._editor.edit.start_time,
            "end_time": self._editor.edit.end_time,
            "limit": int(self._editor.edit.limit),
            "days": days_list_from_editor(self._editor.edit.days),
        }
        if self._editor.is_creating:
            await self.hass.services.async_call(
                DOMAIN, service, service_data, blocking=True
            )
            self._show_success_notification(action="save", body=success_label)
            return
        if self._editor.edit.selected_schedule_id is None:
            return
        service_data["schedule_id"] = self._editor.edit.selected_schedule_id
        service_data["confirm"] = True
        await self.hass.services.async_call(
            DOMAIN, service, service_data, blocking=True
        )
        self._show_success_notification(action="save", body=success_label)


class BatteryScheduleDeleteButton(_BatteryScheduleButton):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry) -> None:
        super().__init__(
            coord,
            entry,
            unique_suffix="battery_schedule_delete",
            translation_key="battery_schedule_delete",
            icon="mdi:calendar-remove",
        )

    @property
    def available(self) -> bool:
        return super().available and bool(
            self._editor
            and _retain_battery_schedule_editor_buttons(self._coord)
            and bool(getattr(self._coord, "battery_write_access_confirmed", False))
            and not self._editor.is_creating
            and self._editor.edit.selected_schedule_id
        )

    async def async_press(self) -> None:
        if self._editor is None or self._editor.edit.selected_schedule_id is None:
            return
        selected_schedule = self._editor.get_schedule(
            self._editor.edit.selected_schedule_id
        )
        success_label = self._schedule_result_label()
        service_data: dict[str, object] = {
            "config_entry_id": self._entry.entry_id,
            "schedule_id": self._editor.edit.selected_schedule_id,
            "confirm": True,
        }
        if selected_schedule is not None:
            service_data["schedule_type"] = selected_schedule.schedule_type
        await self.hass.services.async_call(
            DOMAIN,
            "delete_schedule",
            service_data,
            blocking=True,
        )
        self._show_success_notification(action="delete", body=success_label)


class _BaseButton(EnphaseBaseEntity, ButtonEntity):  # type: ignore[misc]
    def __init__(self, coord: EnphaseCoordinator, sn: str, name_suffix: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_{name_suffix.replace(' ', '_').lower()}"


class StartChargeButton(_BaseButton):
    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn, "Start Charging")
        self._attr_translation_key = "start_charging"

    async def async_press(self) -> None:
        await self._coord.async_start_charging(self._sn)


class StopChargeButton(_BaseButton):
    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn, "Stop Charging")
        self._attr_translation_key = "stop_charging"

    async def async_press(self) -> None:
        await self._coord.async_stop_charging(self._sn)


class _EvseScheduleButton(EvseScheduleEditorEntity, ButtonEntity):  # type: ignore[misc]
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
        icon: str,
    ) -> None:
        super().__init__(coord, entry, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_{unique_suffix}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon

    @property
    def available(self) -> bool:
        return (
            super().available
            and evse_schedule_editor_active(self._coord, self._entry)
            and self._editor is not None
        )


class EvseScheduleRefreshButton(_EvseScheduleButton):
    def __init__(
        self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry, sn: str
    ) -> None:
        super().__init__(
            coord,
            entry,
            sn,
            unique_suffix="schedule_refresh",
            translation_key="evse_schedule_refresh",
            icon="mdi:refresh",
        )

    async def async_press(self) -> None:
        schedule_sync = getattr(self._coord, "schedule_sync", None)
        if schedule_sync is not None:
            await schedule_sync.async_refresh(
                reason="editor_refresh", serials=[self._sn]
            )


class EvseScheduleSaveButton(_EvseScheduleButton):
    def __init__(
        self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry, sn: str
    ) -> None:
        super().__init__(
            coord,
            entry,
            sn,
            unique_suffix="schedule_save",
            translation_key="evse_schedule_save",
            icon="mdi:content-save",
        )

    async def async_press(self) -> None:
        if self._editor is None:
            return
        form = self._editor.form_state(self._sn)
        if not any(form.days.values()):
            raise ServiceValidationError(
                "Select at least one weekday for the schedule.",
                translation_domain=DOMAIN,
                translation_key="evse_schedule_day_required",
            )
        if form.start_time == form.end_time:
            raise ServiceValidationError(
                "Schedule start and end times must be different.",
                translation_domain=DOMAIN,
                translation_key="evse_schedule_times_different",
            )
        schedule_sync = getattr(self._coord, "schedule_sync", None)
        if schedule_sync is None:
            return
        slot = self._editor.build_slot_payload(self._sn)
        slot["days"] = evse_days_list_from_editor(form.days)
        if not await schedule_sync.async_upsert_slot(self._sn, slot):
            raise ServiceValidationError(
                "Enphase rejected the schedule change.",
                translation_domain=DOMAIN,
                translation_key="evse_schedule_change_rejected",
            )


class EvseScheduleDeleteButton(_EvseScheduleButton):
    def __init__(
        self, coord: EnphaseCoordinator, entry: EnphaseConfigEntry, sn: str
    ) -> None:
        super().__init__(
            coord,
            entry,
            sn,
            unique_suffix="schedule_delete",
            translation_key="evse_schedule_delete",
            icon="mdi:calendar-remove",
        )

    @property
    def available(self) -> bool:
        return super().available and bool(
            self._editor
            and not self._editor.is_creating(self._sn)
            and self._editor.current_selection(self._sn)
        )

    async def async_press(self) -> None:
        if self._editor is None:
            return
        slot_id = self._editor.current_selection(self._sn)
        if slot_id in {None, EVSE_NEW_SCHEDULE_OPTION}:
            return
        schedule_sync = getattr(self._coord, "schedule_sync", None)
        if schedule_sync is None:
            return
        await schedule_sync.async_delete_slot(self._sn, slot_id)
