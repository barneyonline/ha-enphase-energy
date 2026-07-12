from __future__ import annotations

from types import MappingProxyType
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_ON
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.enphase_ev.battery_schedule_editor import (
    BatteryScheduleEditorManager,
)
from custom_components.enphase_ev import switch as switch_mod
from custom_components.enphase_ev.api import AuthSettingsUnavailable
from custom_components.enphase_ev.const import (
    OPT_BATTERY_SCHEDULES_ENABLED,
    OPT_SCHEDULE_SYNC_ENABLED,
)
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.entity import EnphaseBaseEntity
from custom_components.enphase_ev.evse_schedule_editor import EvseScheduleEditorManager
from custom_components.enphase_ev.evse_runtime import FAST_TOGGLE_POLL_HOLD_S
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.switch import (
    AcBatterySleepModeSwitch,
    _is_disabled_by_integration,
    _migrated_switch_entity_id,
    _migrate_storm_guard_evse_entity_id,
    _reenable_integration_disabled_entity,
    AppAuthenticationSwitch,
    BatteryScheduleEditorDaySwitch,
    ChargeFromGridScheduleSwitch,
    ChargeFromGridSwitch,
    ChargingSwitch,
    DischargeToGridScheduleSwitch,
    EvseScheduleEditorDaySwitch,
    GreenBatterySwitch,
    RestrictBatteryDischargeScheduleSwitch,
    SavingsUseBatteryAfterPeakSwitch,
    StormGuardEvseSwitch,
    StormGuardSwitch,
    async_setup_entry,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _attach_evse_editor_runtime(config_entry, coord) -> EvseScheduleEditorManager:
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.get_schedules = AsyncMock()
    coord.client.patch_schedule = AsyncMock()
    coord.client.create_schedule = AsyncMock()
    coord.client.delete_schedule = AsyncMock()
    slot_id = f"site:{RANDOM_SERIAL}:slot-editor"
    coord.schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            slot_id: {
                "id": slot_id,
                "startTime": "08:00",
                "endTime": "09:00",
                "scheduleType": "CUSTOM",
                "days": [1, 3],
                "enabled": True,
                "chargingLevelAmp": 32,
            }
        }
    }
    coord.schedule_sync.get_slot = lambda sn, slot: coord.schedule_sync._slot_cache.get(
        sn, {}
    ).get(slot)
    config_entry.__dict__["options"] = MappingProxyType(
        {**config_entry.options, OPT_SCHEDULE_SYNC_ENABLED: True}
    )
    editor = EvseScheduleEditorManager(coord)
    editor.sync_from_coordinator()
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        evse_schedule_editor=editor,
    )
    return editor


def _attach_battery_editor_runtime(config_entry, coord) -> BatteryScheduleEditorManager:
    payload = {
        "cfg": {
            "scheduleStatus": "active",
            "details": [
                {
                    "scheduleId": "cfg-1",
                    "startTime": "02:00:00",
                    "endTime": "05:00:00",
                    "limit": 80,
                    "days": [1, 3],
                    "timezone": "Australia/Melbourne",
                    "isEnabled": True,
                }
            ],
        }
    }
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_write_access_confirmed = True  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.battery_schedules = AsyncMock(return_value=payload)
    coord.client.create_battery_schedule = AsyncMock(return_value={"status": "ok"})
    coord.client.update_battery_schedule = AsyncMock(return_value={"status": "ok"})
    coord.client.delete_battery_schedule = AsyncMock(return_value={"status": "ok"})
    coord._battery_schedules_payload = payload  # noqa: SLF001
    coord.parse_battery_schedules_payload(payload)
    config_entry.__dict__["options"] = MappingProxyType(
        {**config_entry.options, OPT_BATTERY_SCHEDULES_ENABLED: True}
    )
    editor = BatteryScheduleEditorManager(coord)
    editor.sync_from_coordinator()
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        battery_schedule_editor=editor,
    )
    return editor


def test_evse_schedule_editor_day_switch_enabled_by_default(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _attach_evse_editor_runtime(config_entry, coord)

    sw = EvseScheduleEditorDaySwitch(coord, config_entry, RANDOM_SERIAL, "mon")
    sw.hass = hass

    assert sw.entity_registry_enabled_default is True


def test_switch_is_disabled_by_integration_handles_none() -> None:
    assert _is_disabled_by_integration(None) is False


def test_reenable_integration_disabled_entity_skips_missing_registry_entry() -> None:
    ent_reg = MagicMock()
    ent_reg.async_get_entity_id.return_value = "switch.test_entity"
    ent_reg.async_get.return_value = None

    _reenable_integration_disabled_entity(
        ent_reg, domain="switch", unique_id="test-unique-id"
    )

    ent_reg.async_update_entity.assert_not_called()


def test_reenable_integration_disabled_entity_skips_non_integration_disable() -> None:
    ent_reg = MagicMock()
    ent_reg.async_get_entity_id.return_value = "switch.test_entity"
    ent_reg.async_get.return_value = SimpleNamespace(disabled_by="user")

    _reenable_integration_disabled_entity(
        ent_reg, domain="switch", unique_id="test-unique-id"
    )

    ent_reg.async_update_entity.assert_not_called()


def test_migrated_switch_entity_id_handles_canonical_and_suffixes() -> None:
    assert (
        _migrated_switch_entity_id(
            "switch.charge_from_grid_schedule", "switch.charge_from_grid_schedule"
        )
        is None
    )
    assert (
        _migrated_switch_entity_id(
            "switch.charge_from_grid_schedule_2", "switch.charge_from_grid_schedule"
        )
        is None
    )
    assert (
        _migrated_switch_entity_id(
            "switch.custom_schedule_2", "switch.charge_from_grid_schedule"
        )
        == "switch.charge_from_grid_schedule_2"
    )


def test_migrate_storm_guard_evse_entity_id_handles_legacy_suffixes() -> None:
    assert (
        _migrate_storm_guard_evse_entity_id(
            "switch.iq_ev_charger_1707_storm_guard_ev_charge"
        )
        == "switch.iq_ev_charger_1707_storm_guard_evse_charge"
    )
    assert (
        _migrate_storm_guard_evse_entity_id(
            "switch.iq_ev_charger_1707_storm_guard_ev_charge_2"
        )
        == "switch.iq_ev_charger_1707_storm_guard_evse_charge_2"
    )
    assert (
        _migrate_storm_guard_evse_entity_id(
            "switch.iq_ev_charger_1707_storm_guard_evse_charge"
        )
        is None
    )


def test_switch_helper_fallbacks_and_retained_site_keys() -> None:
    from custom_components.enphase_ev import switch as switch_mod

    coord = SimpleNamespace(
        battery_has_encharge=True,
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key
            in {"envoy", "encharge", "ac_battery"}
        ),
        battery_write_access_confirmed=None,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_has_acb=True,
        battery_show_storm_guard=True,
        storm_guard_state="enabled",
        storm_evse_enabled=True,
        savings_use_battery_switch_available=True,
        charge_from_grid_control_available=True,
        charge_from_grid_force_schedule_available=True,
        discharge_to_grid_schedule_available=True,
        restrict_battery_discharge_schedule_supported=True,
    )

    assert switch_mod._type_available(coord, "envoy") is True
    assert switch_mod._battery_write_access_confirmed(coord) is True
    assert switch_mod._retained_site_switch_keys(coord) == {
        "storm_guard",
        "savings_use_battery_after_peak",
        "charge_from_grid",
        "charge_from_grid_schedule",
        "discharge_to_grid_schedule",
        "restrict_battery_discharge_schedule",
        "ac_battery_sleep_mode",
    }

    coord.battery_write_access_confirmed = False
    assert switch_mod._battery_write_access_confirmed(coord) is True
    assert switch_mod._retained_site_switch_keys(coord) == {
        "storm_guard",
        "savings_use_battery_after_peak",
        "charge_from_grid",
        "charge_from_grid_schedule",
        "discharge_to_grid_schedule",
        "restrict_battery_discharge_schedule",
        "ac_battery_sleep_mode",
    }

    coord.battery_user_is_owner = False
    coord.battery_user_is_installer = False
    assert switch_mod._battery_write_access_confirmed(coord) is False
    assert switch_mod._retained_site_switch_keys(coord) == set()


def test_switch_battery_write_access_confirmed_falls_back_to_false() -> None:
    from custom_components.enphase_ev import switch as switch_mod

    coord = SimpleNamespace(
        battery_write_access_confirmed=None,
        battery_user_is_owner=None,
        battery_user_is_installer=None,
    )

    assert switch_mod._battery_write_access_confirmed(coord) is False


def test_charge_from_grid_retention_honors_explicit_negative_flags() -> None:
    coord = SimpleNamespace()

    coord.battery_system_task = True
    assert switch_mod._charge_from_grid_retained(coord) is False

    coord.battery_system_task = False
    coord.battery_cfg_control_show = False
    assert switch_mod._charge_from_grid_retained(coord) is False

    coord.battery_cfg_control_show = None
    coord._battery_hide_charge_from_grid = True  # noqa: SLF001
    assert switch_mod._charge_from_grid_retained(coord) is False

    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord.battery_cfg_control_locked = True
    assert switch_mod._charge_from_grid_retained(coord) is False

    coord.battery_cfg_control_locked = False
    assert switch_mod._charge_from_grid_retained(coord) is True

    coord.battery_system_task = True
    assert switch_mod._charge_from_grid_schedule_retained(coord) is False

    coord.battery_system_task = False
    coord.battery_cfg_control_show_day_schedule = False
    assert switch_mod._charge_from_grid_schedule_retained(coord) is False


def test_switch_pending_helper_edge_paths() -> None:
    entity = SimpleNamespace(
        hass=object(),
        entity_id="switch.test",
        async_write_ha_state=MagicMock(),
    )
    switch_mod._write_state_if_available(entity)
    entity.async_write_ha_state.assert_called_once()

    coord = SimpleNamespace(_green_battery_pending={})

    assert (
        switch_mod._effective_evse_toggle_state(
            SimpleNamespace(_green_battery_pending=True),  # type: ignore[arg-type]
            "_green_battery_pending",
            RANDOM_SERIAL,
            False,
        )
        is False
    )

    coord._green_battery_pending[RANDOM_SERIAL] = ("bad",)  # noqa: SLF001
    assert (
        switch_mod._effective_evse_toggle_state(
            coord, "_green_battery_pending", RANDOM_SERIAL, False
        )
        is False
    )
    assert RANDOM_SERIAL not in coord._green_battery_pending  # noqa: SLF001

    coord._green_battery_pending[RANDOM_SERIAL] = (
        False,
        switch_mod.time.monotonic() + 10,
    )  # noqa: SLF001
    assert (
        switch_mod._effective_evse_toggle_state(
            coord, "_green_battery_pending", RANDOM_SERIAL, False
        )
        is False
    )
    assert RANDOM_SERIAL not in coord._green_battery_pending  # noqa: SLF001

    coord._green_battery_pending[RANDOM_SERIAL] = (
        True,
        switch_mod.time.monotonic() - 1,
    )  # noqa: SLF001
    assert (
        switch_mod._effective_evse_toggle_state(
            coord, "_green_battery_pending", RANDOM_SERIAL, False
        )
        is False
    )
    assert RANDOM_SERIAL not in coord._green_battery_pending  # noqa: SLF001

    coord._green_battery_pending[RANDOM_SERIAL] = (True, object())  # noqa: SLF001
    assert (
        switch_mod._effective_evse_toggle_state(
            coord, "_green_battery_pending", RANDOM_SERIAL, False
        )
        is False
    )
    assert RANDOM_SERIAL not in coord._green_battery_pending  # noqa: SLF001

    coord._pending_charging = {RANDOM_SERIAL: ("bad",)}  # noqa: SLF001
    assert switch_mod._pending_charging_state(coord, RANDOM_SERIAL) is None

    coord._pending_charging = {
        RANDOM_SERIAL: (True, switch_mod.time.monotonic() - 1)
    }  # noqa: SLF001
    assert switch_mod._pending_charging_state(coord, RANDOM_SERIAL) is None
    assert RANDOM_SERIAL not in coord._pending_charging  # noqa: SLF001

    coord._pending_charging = {RANDOM_SERIAL: (True, object())}  # noqa: SLF001
    assert switch_mod._pending_charging_state(coord, RANDOM_SERIAL) is None


def test_ac_battery_sleep_mode_switch_state_and_attributes(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._ac_battery_sleep_state = "pending"  # noqa: SLF001
    coord._ac_battery_control_pending = True  # noqa: SLF001
    coord._ac_battery_selected_sleep_min_soc = 25  # noqa: SLF001
    coord._ac_battery_aggregate_status_details = {  # noqa: SLF001
        "sleep_state_raw": {"BAT-AC-1": "cancel"},
        "sleep_state_map": {"BAT-AC-1": "pending"},
    }
    coord.async_set_ac_battery_sleep_mode = AsyncMock()

    sw = AcBatterySleepModeSwitch(coord)

    assert sw.available is True
    assert sw.is_on is True
    assert sw.extra_state_attributes["pending"] is True


def test_ac_battery_sleep_mode_switch_edge_branches(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_acb = False  # noqa: SLF001
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._ac_battery_sleep_state = None  # noqa: SLF001

    sw = AcBatterySleepModeSwitch(coord)

    assert sw.suggested_object_id == "ac_battery_sleep_mode"
    assert sw.available is False
    assert sw.device_info["name"] == "AC Battery"

    coord._battery_has_acb = True  # noqa: SLF001
    coord.last_update_success = False
    assert sw.available is False


@pytest.mark.asyncio
async def test_ac_battery_sleep_mode_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._ac_battery_sleep_state = "off"  # noqa: SLF001
    coord.async_set_ac_battery_sleep_mode = AsyncMock()

    sw = AcBatterySleepModeSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_ac_battery_sleep_mode.assert_awaited_with(True)

    await sw.async_turn_off()
    coord.async_set_ac_battery_sleep_mode.assert_awaited_with(False)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_ac_battery_sleep_mode_switch(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._ac_battery_sleep_state = "on"  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, AcBatterySleepModeSwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_battery_schedule_day_switches(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _attach_battery_editor_runtime(config_entry, coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert (
        sum(isinstance(entity, BatteryScheduleEditorDaySwitch) for entity in added) == 7
    )


@pytest.fixture
def coordinator_factory(hass, config_entry, monkeypatch):
    """Create a configured coordinator with controllable client behavior."""

    def _create(extra: dict | None = None) -> EnphaseCoordinator:
        monkeypatch.setattr(
            "custom_components.enphase_ev.coordinator.async_get_clientsession",
            lambda *args, **kwargs: object(),
        )
        coord = EnphaseCoordinator(hass, config_entry.data, config_entry=config_entry)
        coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
            {
                "envoy": {
                    "type_key": "envoy",
                    "type_label": "Gateway",
                    "count": 1,
                    "devices": [{"name": "IQ Gateway"}],
                },
                "encharge": {
                    "type_key": "encharge",
                    "type_label": "Battery",
                    "count": 1,
                    "devices": [{"name": "IQ Battery"}],
                },
                "iqevse": {
                    "type_key": "iqevse",
                    "type_label": "EV Chargers",
                    "count": 1,
                    "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
                },
            },
            ["envoy", "encharge", "iqevse"],
        )
        coord._schedule_refresh = MagicMock()
        base = {
            RANDOM_SERIAL: {
                "name": "Garage EV",
                "display_name": "Garage EV",
                "charging": False,
                "plugged": True,
                "min_amp": 6,
                "max_amp": 32,
            }
        }
        if extra:
            base[RANDOM_SERIAL].update(extra)
        coord.data = base
        coord.last_set_amps = {}
        coord._ensure_serial_tracked(RANDOM_SERIAL)
        coord._battery_user_is_owner = True  # noqa: SLF001
        coord._battery_user_is_installer = False  # noqa: SLF001

        original_set_desired = coord.set_desired_charging
        coord.set_desired_charging = MagicMock(wraps=original_set_desired)
        original_set_last = coord.set_last_set_amps
        coord.set_last_set_amps = MagicMock(wraps=original_set_last)
        original_require = coord.require_plugged
        coord.require_plugged = MagicMock(wraps=original_require)

        coord.client = SimpleNamespace(
            start_charging=AsyncMock(return_value={"status": "ok"}),
            stop_charging=AsyncMock(return_value=None),
            set_green_battery_setting=AsyncMock(return_value={"status": "ok"}),
            set_app_authentication=AsyncMock(return_value={"status": "ok"}),
            set_storm_guard=AsyncMock(return_value={"status": "ok"}),
            start_live_stream=AsyncMock(
                return_value={"status": "accepted", "duration_s": 900}
            ),
        )
        coord.async_request_refresh = AsyncMock()
        coord.kick_fast = MagicMock()
        coord.set_charging_expectation = MagicMock()
        coord.pick_start_amps = MagicMock(return_value=32)
        return coord

    return _create


@pytest.mark.asyncio
async def test_async_setup_entry_syncs_chargers(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    listener_spy = MagicMock(wraps=coord.async_add_listener)
    monkeypatch.setattr(coord, "async_add_listener", listener_spy)

    await async_setup_entry(hass, config_entry, _capture)
    charger_entities = [ent for ent in added if hasattr(ent, "_sn")]
    assert {ent._sn for ent in charger_entities} == {RANDOM_SERIAL}
    assert any(isinstance(entity, ChargingSwitch) for entity in charger_entities)
    assert any(isinstance(entity, StormGuardEvseSwitch) for entity in charger_entities)
    listener_spy.assert_called_once()
    listener = listener_spy.call_args[0][0]

    new_serial = "EV0002"
    coord.data[new_serial] = {
        "name": "Second Charger",
        "charging": False,
        "plugged": True,
    }
    coord._ensure_serial_tracked(new_serial)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_label": "EV Chargers",
                "count": 2,
                "devices": [
                    {"serial_number": RANDOM_SERIAL},
                    {"serial_number": new_serial},
                ],
            }
        },
        ["iqevse"],
    )

    listener()
    charger_entities = [ent for ent in added if hasattr(ent, "_sn")]
    assert {ent._sn for ent in charger_entities} == {RANDOM_SERIAL, new_serial}

    listener()
    charger_entities = [ent for ent in added if hasattr(ent, "_sn")]
    assert {ent._sn for ent in charger_entities} == {RANDOM_SERIAL, new_serial}
    assert config_entry._on_unload and callable(config_entry._on_unload[0])


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_cfg_switches_while_permission_unknown(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_write_access_confirmed = False  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord._storm_guard_state = "disabled"  # noqa: SLF001
    coord._storm_evse_enabled = None  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    listener_callbacks = []
    original_add_listener = coord.async_add_listener

    def _capture_listener(callback):
        listener_callbacks.append(callback)
        return original_add_listener(callback)

    coord.async_add_listener = MagicMock(side_effect=_capture_listener)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    charge_switch = next(
        entity for entity in added if isinstance(entity, ChargeFromGridSwitch)
    )
    schedule_switch = next(
        entity for entity in added if isinstance(entity, ChargeFromGridScheduleSwitch)
    )
    storm_guard_switch = next(
        entity for entity in added if isinstance(entity, StormGuardSwitch)
    )
    assert charge_switch.available is True
    assert schedule_switch.available is True
    assert storm_guard_switch.available is True

    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    listener_callbacks[0]()

    assert charge_switch.available is True
    assert schedule_switch.available is True
    assert sum(isinstance(entity, ChargeFromGridSwitch) for entity in added) == 1
    assert (
        sum(isinstance(entity, ChargeFromGridScheduleSwitch) for entity in added) == 1
    )


@pytest.mark.asyncio
async def test_async_setup_entry_adds_supported_battery_site_switches_and_prunes_types(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_show_storm_guard = True  # noqa: SLF001
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_evse_enabled = True  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord._battery_dtg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {"show": True, "showDaySchedule": True, "scheduleSupported": True}
        )
    )
    coord._battery_dtg_schedule_id = "sched-dtg"  # noqa: SLF001
    coord._battery_dtg_begin_time = 1080  # noqa: SLF001
    coord._battery_dtg_end_time = 1380  # noqa: SLF001
    coord._battery_rbd_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {"show": True, "showDaySchedule": True, "scheduleSupported": True}
        )
    )
    coord._battery_rbd_schedule_id = "sched-rbd"  # noqa: SLF001
    coord._battery_rbd_begin_time = 60  # noqa: SLF001
    coord._battery_rbd_end_time = 960  # noqa: SLF001
    current_types = {"envoy", "encharge", "iqevse"}
    coord.inventory_view.has_type_for_entities = (
        lambda type_key: type_key in current_types
    )
    listener_callbacks = []
    original_add_listener = coord.async_add_listener

    def _capture_listener(callback):
        listener_callbacks.append(callback)
        return original_add_listener(callback)

    coord.async_add_listener = MagicMock(side_effect=_capture_listener)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    prune_spy = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.prune_managed_entities", prune_spy
    )

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, StormGuardSwitch) for entity in added)
    assert any(isinstance(entity, SavingsUseBatteryAfterPeakSwitch) for entity in added)
    assert any(isinstance(entity, DischargeToGridScheduleSwitch) for entity in added)
    assert any(
        isinstance(entity, RestrictBatteryDischargeScheduleSwitch) for entity in added
    )

    current_types = {"encharge", "iqevse"}
    listener_callbacks[0]()
    active_unique_ids = prune_spy.call_args_list[-1].kwargs["active_unique_ids"]
    assert f"enphase_ev_site_{coord.site_id}_storm_guard" not in active_unique_ids

    current_types = {"envoy", "iqevse"}
    listener_callbacks[0]()
    active_unique_ids = prune_spy.call_args_list[-1].kwargs["active_unique_ids"]
    assert (
        f"enphase_ev_site_{coord.site_id}_savings_use_battery_after_peak"
        not in active_unique_ids
    )
    assert f"enphase_ev_site_{coord.site_id}_charge_from_grid" not in active_unique_ids


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_loaded_site_switches_when_support_disproven(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord._battery_show_storm_guard = True  # noqa: SLF001
    listener_callbacks = []
    original_add_listener = coord.async_add_listener

    def _capture_listener(callback):
        listener_callbacks.append(callback)
        return original_add_listener(callback)

    coord.async_add_listener = MagicMock(side_effect=_capture_listener)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    prune_spy = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.prune_managed_entities", prune_spy
    )

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, StormGuardSwitch) for entity in added)
    assert any(isinstance(entity, ChargeFromGridSwitch) for entity in added)
    assert any(isinstance(entity, ChargeFromGridScheduleSwitch) for entity in added)

    prune_spy.reset_mock()
    coord._battery_show_storm_guard = False  # noqa: SLF001
    coord._battery_cfg_control = (  # noqa: SLF001
        coord.battery_runtime._parse_battery_control_capability(
            {
                "show": False,
                "showDaySchedule": False,
                "forceScheduleSupported": False,
            }
        )
    )
    listener_callbacks[0]()

    active_unique_ids = prune_spy.call_args_list[0].kwargs["active_unique_ids"]
    assert f"enphase_ev_site_{coord.site_id}_storm_guard" in active_unique_ids
    assert f"enphase_ev_site_{coord.site_id}_charge_from_grid" in active_unique_ids
    assert (
        f"enphase_ev_site_{coord.site_id}_charge_from_grid_schedule"
        in active_unique_ids
    )

    added.clear()
    prune_spy.reset_mock()
    coord._battery_show_storm_guard = True  # noqa: SLF001
    coord._battery_cfg_control = (  # noqa: SLF001
        coord.battery_runtime._parse_battery_control_capability(
            {
                "show": True,
                "showDaySchedule": True,
                "forceScheduleSupported": True,
            }
        )
    )
    listener_callbacks[0]()

    assert not any(isinstance(entity, StormGuardSwitch) for entity in added)
    assert not any(isinstance(entity, ChargeFromGridSwitch) for entity in added)
    assert not any(isinstance(entity, ChargeFromGridScheduleSwitch) for entity in added)
    active_unique_ids = prune_spy.call_args_list[0].kwargs["active_unique_ids"]
    assert f"enphase_ev_site_{coord.site_id}_storm_guard" in active_unique_ids
    assert f"enphase_ev_site_{coord.site_id}_charge_from_grid" in active_unique_ids
    assert (
        f"enphase_ev_site_{coord.site_id}_charge_from_grid_schedule"
        in active_unique_ids
    )


@pytest.mark.asyncio
async def test_async_setup_entry_migrates_charge_from_grid_schedule_switch_entity_id(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_schedule"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=unique_id,
            entity_id="switch.custom_charge_from_grid_schedule",
        )
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_called_once_with(
        "switch.custom_charge_from_grid_schedule",
        new_entity_id="switch.charge_from_grid_schedule",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_migrates_charge_from_grid_schedule_switch_suffix(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_schedule"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=unique_id,
            entity_id="switch.custom_charge_from_grid_schedule_3",
        )
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_called_once_with(
        "switch.custom_charge_from_grid_schedule_3",
        new_entity_id="switch.charge_from_grid_schedule_3",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_switch_migration_skips_canonical_and_unrelated(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_schedule"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=unique_id,
            entity_id="switch.charge_from_grid_schedule",
        ),
        SimpleNamespace(
            unique_id="enphase_ev_site_other_unrelated_switch",
            entity_id="switch.unrelated",
        ),
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_ignores_stale_schedule_slot_remove_failure(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    _attach_evse_editor_runtime(config_entry, coord)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "switch",
        "enphase_ev",
        "enphase_ev_other_schedule_edit_mon",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    coord.iter_serials = lambda: [RANDOM_SERIAL]

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    remove_spy.assert_called_once_with(stale.entity_id)


@pytest.mark.asyncio
async def test_async_setup_entry_switch_migration_handles_rename_conflict(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    unique_id = f"enphase_ev_site_{coord.site_id}_charge_from_grid_schedule"
    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock(side_effect=ValueError("duplicate"))
    entries = [
        SimpleNamespace(
            unique_id=unique_id,
            entity_id="switch.custom_charge_from_grid_schedule",
        )
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_called_once_with(
        "switch.custom_charge_from_grid_schedule",
        new_entity_id="switch.charge_from_grid_schedule",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_migrates_storm_guard_evse_switch_entity_id(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    fake_registry = MagicMock()
    fake_registry.async_update_entity = MagicMock()
    entries = [
        SimpleNamespace(
            unique_id=f"enphase_ev_{RANDOM_SERIAL}_storm_guard_evse_charge",
            entity_id="switch.iq_ev_charger_1707_storm_guard_ev_charge",
        )
    ]
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.er.async_entries_for_config_entry",
        lambda _registry, _entry_id: entries,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_update_entity.assert_called_once_with(
        "switch.iq_ev_charger_1707_storm_guard_ev_charge",
        new_entity_id="switch.iq_ev_charger_1707_storm_guard_evse_charge",
    )


@pytest.mark.asyncio
async def test_async_setup_entry_switch_cleanup_waits_for_inventory_ready(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._devices_inventory_ready = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "switch",
        "enphase_ev",
        f"enphase_ev_{RANDOM_SERIAL}_app_authentication",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    remove_spy.assert_not_called()
    assert ent_reg.async_get(stale.entity_id) is not None


@pytest.mark.asyncio
async def test_async_setup_entry_switch_ignores_blank_serials_in_feature_loops(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "app_auth_supported": True}
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.iter_serials = lambda: [RANDOM_SERIAL, "", None]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    prune_spy = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.switch.prune_managed_entities", prune_spy
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    active_unique_ids = list(prune_spy.call_args_list[-1].kwargs["active_unique_ids"])
    assert f"enphase_ev_{RANDOM_SERIAL}_green_battery" in active_unique_ids
    assert all("None" not in unique_id for unique_id in active_unique_ids)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_schedule_when_sync_missing(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.schedule_sync = None
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, EvseScheduleEditorDaySwitch) for entity in added)
    assert any(isinstance(entity, ChargingSwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_battery_site_switches_without_battery(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, ChargingSwitch) for entity in added)
    assert not any(isinstance(entity, StormGuardSwitch) for entity in added)
    assert not any(
        isinstance(entity, SavingsUseBatteryAfterPeakSwitch) for entity in added
    )
    assert not any(isinstance(entity, ChargeFromGridSwitch) for entity in added)
    assert not any(isinstance(entity, ChargeFromGridScheduleSwitch) for entity in added)
    assert not any(isinstance(entity, StormGuardEvseSwitch) for entity in added)
    assert not any(isinstance(entity, GreenBatterySwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_green_battery_switch_when_supported(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": True}
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    listener_spy = MagicMock(wraps=coord.async_add_listener)
    monkeypatch.setattr(coord, "async_add_listener", listener_spy)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, GreenBatterySwitch) for entity in added)
    listener = listener_spy.call_args[0][0]
    listener()


@pytest.mark.asyncio
async def test_async_setup_entry_skips_green_battery_switch_when_unsupported(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory({"green_battery_supported": False})
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, GreenBatterySwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_app_auth_switch_when_supported(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory({"app_auth_supported": True, "app_auth_enabled": True})
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    listener_spy = MagicMock(wraps=coord.async_add_listener)
    monkeypatch.setattr(coord, "async_add_listener", listener_spy)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, AppAuthenticationSwitch) for entity in added)
    listener = listener_spy.call_args[0][0]
    listener()


@pytest.mark.asyncio
async def test_async_setup_entry_skips_app_auth_switch_when_unsupported(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory({"app_auth_supported": False})
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, AppAuthenticationSwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_feature_switches_when_inventory_ready(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": True}
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    listener_spy = MagicMock(wraps=coord.async_add_listener)
    monkeypatch.setattr(coord, "async_add_listener", listener_spy)

    await async_setup_entry(hass, config_entry, _capture)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "switch",
        "enphase_ev",
        f"enphase_ev_{RANDOM_SERIAL}_green_battery",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    coord.data[RANDOM_SERIAL]["green_battery_supported"] = False
    listener = listener_spy.call_args[0][0]
    listener()

    remove_spy.assert_called_with(stale.entity_id)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_schedule_switches(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _attach_evse_editor_runtime(config_entry, coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert sum(isinstance(entity, EvseScheduleEditorDaySwitch) for entity in added) == 7


@pytest.mark.asyncio
async def test_async_setup_entry_reenables_integration_disabled_evse_day_switches(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _attach_evse_editor_runtime(config_entry, coord)

    ent_reg = er.async_get(hass)
    reg_entry = ent_reg.async_get_or_create(
        "switch",
        "enphase_ev",
        f"enphase_ev_{RANDOM_SERIAL}_schedule_edit_mon",
        config_entry=config_entry,
    )
    disabler = getattr(er, "RegistryEntryDisabler", None)
    if disabler is not None:
        ent_reg.async_update_entity(
            reg_entry.entity_id,
            disabled_by=disabler.INTEGRATION,
        )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    updated = ent_reg.async_get(reg_entry.entity_id)
    assert updated is not None
    assert updated.disabled_by is None


@pytest.mark.asyncio
async def test_async_setup_entry_skips_duplicate_schedule_switches(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _attach_evse_editor_runtime(config_entry, coord)

    added: list = []
    callback_holder = {}

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    def _capture_listener(callback):
        callback_holder["callback"] = callback
        return MagicMock()

    coord.async_add_listener = MagicMock(side_effect=_capture_listener)

    await async_setup_entry(hass, config_entry, _capture)
    callback_holder["callback"]()

    schedule_switches = [
        entity for entity in added if isinstance(entity, EvseScheduleEditorDaySwitch)
    ]
    assert len(schedule_switches) == 7


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_stale_schedule_slot_switches(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    _attach_evse_editor_runtime(config_entry, coord)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "switch",
        "enphase_ev",
        "enphase_ev_other_schedule_edit_tue",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    remove_spy.assert_called_with(stale.entity_id)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_read_only_slots(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    missing_time_id = f"site:{RANDOM_SERIAL}:slot-missing-time"
    coord.schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            missing_time_id: {
                "id": missing_time_id,
                "startTime": None,
                "endTime": "09:00",
                "scheduleType": "CUSTOM",
                "enabled": True,
            },
        }
    }
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, EvseScheduleEditorDaySwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_off_peak_schedule_switch(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _attach_evse_editor_runtime(config_entry, coord)
    coord.schedule_sync._slot_cache[RANDOM_SERIAL] = {
        f"site:{RANDOM_SERIAL}:slot-off-peak": {
            "id": f"site:{RANDOM_SERIAL}:slot-off-peak",
            "startTime": None,
            "endTime": None,
            "scheduleType": "OFF_PEAK",
            "days": [1],
            "enabled": False,
            "chargingLevelAmp": 32,
        }
    }

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, EvseScheduleEditorDaySwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_off_peak_when_ineligible(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    off_peak_id = f"site:{RANDOM_SERIAL}:slot-off-peak"
    coord.schedule_sync._slot_cache = {
        RANDOM_SERIAL: {
            off_peak_id: {
                "id": off_peak_id,
                "startTime": None,
                "endTime": None,
                "scheduleType": "OFF_PEAK",
                "enabled": False,
            }
        }
    }
    coord.schedule_sync._config_cache = {RANDOM_SERIAL: {"isOffPeakEligible": False}}
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, EvseScheduleEditorDaySwitch) for entity in added)


@pytest.mark.asyncio
async def test_async_added_to_hass_restores_last_on_state(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass
    sw.entity_id = "switch.enphase_ev_charging"
    sw.async_get_last_state = AsyncMock(return_value=State(sw.entity_id, STATE_ON))
    sw.async_write_ha_state = MagicMock()

    await sw.async_added_to_hass()

    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, True)
    coord.kick_fast.assert_called_once_with(60)
    coord.async_request_refresh.assert_awaited_once()
    sw.async_write_ha_state.assert_called_once()
    assert coord.get_desired_charging(RANDOM_SERIAL) is True


@pytest.mark.asyncio
async def test_async_added_to_hass_swallows_refresh_failure(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.async_request_refresh = AsyncMock(side_effect=RuntimeError("boom"))
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass
    sw.entity_id = "switch.enphase_ev_charging"
    sw.async_get_last_state = AsyncMock(return_value=State(sw.entity_id, STATE_ON))
    sw.async_write_ha_state = MagicMock()

    await sw.async_added_to_hass()

    coord.kick_fast.assert_called_once_with(60)
    coord.async_request_refresh.assert_awaited_once()
    sw.async_write_ha_state.assert_not_called()
    assert sw._restored_state is True


@pytest.mark.asyncio
async def test_async_added_to_hass_without_restore_sets_current_state(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory({"charging": True})
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass
    sw.async_get_last_state = AsyncMock(return_value=None)

    await sw.async_added_to_hass()

    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, True)
    assert sw._restored_state is True


def test_is_on_prefers_restored_state_when_unavailable(coordinator_factory) -> None:
    coord = coordinator_factory({"charging": True})
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw._restored_state = False
    sw._has_data = False

    assert sw.is_on is False
    sw._restored_state = True
    assert sw.is_on is True


@pytest.mark.asyncio
async def test_async_turn_on_not_ready_clears_desired(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord.client.start_charging = AsyncMock(return_value={"status": "not_ready"})
    coord.set_charging_expectation.reset_mock()
    coord.kick_fast.reset_mock()
    coord.async_request_refresh.reset_mock()
    coord.track_entry_background_task = MagicMock(
        wraps=coord.track_entry_background_task
    )

    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass

    await sw.async_turn_on()

    coord.client.start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, 32, 1, include_level=None, strict_preference=False
    )
    coord.set_last_set_amps.assert_called_once_with(RANDOM_SERIAL, 32)
    coord.set_desired_charging.assert_called_with(RANDOM_SERIAL, False)
    coord.set_charging_expectation.assert_not_called()
    coord.kick_fast.assert_called_once_with(FAST_TOGGLE_POLL_HOLD_S)
    assert coord.async_request_refresh.called
    coord.track_entry_background_task.assert_called_once()
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_async_turn_on_plugged_validation_triggers_fast_refresh(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory({"plugged": False})
    coord.kick_fast.reset_mock()
    coord.async_request_refresh.reset_mock()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass
    sw.entity_id = "switch.enphase_ev_charging"
    sw.async_write_ha_state = MagicMock()

    with pytest.raises(ServiceValidationError):
        await sw.async_turn_on()

    coord.kick_fast.assert_called_once_with(FAST_TOGGLE_POLL_HOLD_S)
    assert coord.async_request_refresh.called
    sw.async_write_ha_state.assert_called_once()
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_async_turn_on_validation_without_hass_skips_refresh(
    coordinator_factory,
) -> None:
    coord = coordinator_factory({"plugged": False})
    coord.kick_fast.reset_mock()
    coord.async_request_refresh.reset_mock()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)

    with pytest.raises(ServiceValidationError):
        await sw.async_turn_on()

    coord.kick_fast.assert_called_once_with(FAST_TOGGLE_POLL_HOLD_S)
    assert coord.async_request_refresh.called is False


@pytest.mark.asyncio
async def test_async_turn_off_triggers_stop(coordinator_factory) -> None:
    coord = coordinator_factory()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)

    await sw.async_turn_off()

    coord.client.stop_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_turn_on_success_writes_local_state_when_attached(
    hass, coordinator_factory
) -> None:
    coord = coordinator_factory()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw.hass = hass
    sw.entity_id = "switch.enphase_ev_charging"
    sw.async_write_ha_state = MagicMock()

    await sw.async_turn_on()

    sw.async_write_ha_state.assert_called_once()


def test_handle_coordinator_update_clears_restored_state(coordinator_factory) -> None:
    coord = coordinator_factory()
    sw = ChargingSwitch(coord, RANDOM_SERIAL)
    sw._restored_state = True

    with patch.object(
        EnphaseBaseEntity, "_handle_coordinator_update", autospec=True
    ) as mock_super:
        sw._handle_coordinator_update()

    mock_super.assert_called_once_with(sw)
    assert sw._restored_state is None


def test_green_battery_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": None}
    )
    sw = GreenBatterySwitch(coord, RANDOM_SERIAL)
    assert sw.available is False

    coord.data[RANDOM_SERIAL]["green_battery_enabled"] = False
    sw_updated = GreenBatterySwitch(coord, RANDOM_SERIAL)
    assert sw_updated.available is True
    assert sw_updated.is_on is False


def test_green_battery_switch_ignores_long_lived_cache_without_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": False}
    )
    coord.set_green_battery_cache(RANDOM_SERIAL, True)

    assert GreenBatterySwitch(coord, RANDOM_SERIAL).is_on is False


def test_green_battery_switch_is_on_prefers_short_pending_override(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": False}
    )
    coord._green_battery_pending[RANDOM_SERIAL] = (  # noqa: SLF001
        True,
        switch_mod.time.monotonic() + 10,
    )

    assert GreenBatterySwitch(coord, RANDOM_SERIAL).is_on is True


def test_green_battery_switch_unavailable_without_data(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": True}
    )
    sw = GreenBatterySwitch(coord, RANDOM_SERIAL)
    sw._has_data = False
    assert sw.available is False


def test_green_battery_switch_unavailable_when_unsupported(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": False, "green_battery_enabled": True}
    )
    sw = GreenBatterySwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


def test_green_battery_switch_unavailable_when_scheduler_down(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": True}
    )
    coord._scheduler_available = False  # noqa: SLF001
    sw = GreenBatterySwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


@pytest.mark.asyncio
async def test_green_battery_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"green_battery_supported": True, "green_battery_enabled": False}
    )
    coord._green_battery_cache.clear()
    coord.evse_runtime.async_set_green_battery_setting = AsyncMock(
        wraps=coord.evse_runtime.async_set_green_battery_setting
    )
    sw = GreenBatterySwitch(coord, RANDOM_SERIAL)

    await sw.async_turn_on()
    coord.evse_runtime.async_set_green_battery_setting.assert_awaited_once_with(
        RANDOM_SERIAL, enabled=True
    )
    coord.client.set_green_battery_setting.assert_awaited_once_with(
        RANDOM_SERIAL, enabled=True
    )
    assert coord._green_battery_cache[RANDOM_SERIAL][0] is True
    assert coord._green_battery_pending[RANDOM_SERIAL][0] is True

    await sw.async_turn_off()
    assert coord.evse_runtime.async_set_green_battery_setting.await_count == 2
    coord.evse_runtime.async_set_green_battery_setting.assert_awaited_with(
        RANDOM_SERIAL, enabled=False
    )
    assert coord.client.set_green_battery_setting.await_count == 2
    coord.client.set_green_battery_setting.assert_awaited_with(
        RANDOM_SERIAL, enabled=False
    )
    assert coord._green_battery_cache[RANDOM_SERIAL][0] is False
    assert coord._green_battery_pending[RANDOM_SERIAL][0] is False
    assert coord.async_request_refresh.await_count == 2


def test_app_auth_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory({"app_auth_supported": True, "app_auth_enabled": None})
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    assert sw.available is True
    assert sw.is_on is False

    coord.data[RANDOM_SERIAL]["app_auth_enabled"] = False
    sw_updated = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    assert sw_updated.available is True
    assert sw_updated.is_on is False


def test_app_auth_switch_unavailable_without_data(coordinator_factory) -> None:
    coord = coordinator_factory({"app_auth_supported": True, "app_auth_enabled": True})
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    sw._has_data = False
    assert sw.available is False


def test_app_auth_switch_unavailable_when_unsupported(coordinator_factory) -> None:
    coord = coordinator_factory({"app_auth_supported": False, "app_auth_enabled": True})
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


def test_app_auth_switch_ignores_feature_flag_for_availability(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(
        {
            "app_auth_supported": True,
            "app_auth_enabled": True,
            "auth_feature_supported": False,
        }
    )
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    assert sw.available is True


def test_app_auth_switch_unavailable_when_service_down(coordinator_factory) -> None:
    coord = coordinator_factory({"app_auth_supported": True, "app_auth_enabled": True})
    coord._auth_settings_available = False  # noqa: SLF001
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


def test_app_auth_switch_ignores_long_lived_cache_without_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory({"app_auth_supported": True, "app_auth_enabled": False})
    coord.set_app_auth_cache(RANDOM_SERIAL, True)

    assert AppAuthenticationSwitch(coord, RANDOM_SERIAL).is_on is False


def test_app_auth_switch_is_on_prefers_short_pending_override(
    coordinator_factory,
) -> None:
    coord = coordinator_factory({"app_auth_supported": True, "app_auth_enabled": False})
    coord._app_auth_pending[RANDOM_SERIAL] = (
        True,
        switch_mod.time.monotonic() + 10,
    )  # noqa: SLF001

    assert AppAuthenticationSwitch(coord, RANDOM_SERIAL).is_on is True


@pytest.mark.asyncio
async def test_app_auth_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory({"app_auth_supported": True, "app_auth_enabled": False})
    coord._auth_settings_cache.clear()
    coord.evse_runtime.async_set_app_authentication = AsyncMock(
        wraps=coord.evse_runtime.async_set_app_authentication
    )
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)

    await sw.async_turn_on()
    coord.evse_runtime.async_set_app_authentication.assert_awaited_once_with(
        RANDOM_SERIAL, enabled=True
    )
    coord.client.set_app_authentication.assert_awaited_once_with(
        RANDOM_SERIAL, enabled=True
    )
    assert coord._auth_settings_cache[RANDOM_SERIAL][0] is True
    assert coord._app_auth_pending[RANDOM_SERIAL][0] is True

    await sw.async_turn_off()
    assert coord.evse_runtime.async_set_app_authentication.await_count == 2
    coord.evse_runtime.async_set_app_authentication.assert_awaited_with(
        RANDOM_SERIAL, enabled=False
    )
    assert coord.client.set_app_authentication.await_count == 2
    coord.client.set_app_authentication.assert_awaited_with(
        RANDOM_SERIAL, enabled=False
    )
    assert coord._auth_settings_cache[RANDOM_SERIAL][0] is False
    assert coord._app_auth_pending[RANDOM_SERIAL][0] is False
    assert coord.async_request_refresh.await_count == 2


@pytest.mark.asyncio
async def test_app_auth_switch_handles_auth_settings_unavailable(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory({"app_auth_supported": True, "app_auth_enabled": False})
    coord.client.set_app_authentication = AsyncMock(
        side_effect=AuthSettingsUnavailable("down")
    )
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)

    with pytest.raises(
        HomeAssistantError, match="Authentication settings are unavailable"
    ):
        await sw.async_turn_on()

    assert any(
        issue[2]["translation_key"] == "auth_settings_unavailable"
        for issue in mock_issue_registry.created
    )


@pytest.mark.asyncio
async def test_app_auth_switch_turn_off_handles_auth_settings_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory({"app_auth_supported": True, "app_auth_enabled": True})
    coord.client.set_app_authentication = AsyncMock(
        side_effect=AuthSettingsUnavailable("down")
    )
    sw = AppAuthenticationSwitch(coord, RANDOM_SERIAL)

    with pytest.raises(
        HomeAssistantError, match="Authentication settings are unavailable"
    ):
        await sw.async_turn_off()


def test_battery_schedule_editor_day_switch_without_editor(
    config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    sw = BatteryScheduleEditorDaySwitch(coord, config_entry, day_key="mon")

    assert sw.available is False
    assert sw.is_on is False


@pytest.mark.asyncio
async def test_battery_schedule_editor_day_switch_uses_editor_state_and_actions(
    config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    editor = _attach_battery_editor_runtime(config_entry, coord)
    sw = BatteryScheduleEditorDaySwitch(coord, config_entry, day_key="mon")

    assert sw.available is True
    assert sw.is_on is True

    await sw.async_turn_off()
    assert editor.edit.days["mon"] is False

    await sw.async_turn_on()
    assert editor.edit.days["mon"] is True


def test_battery_schedule_editor_day_switch_uses_inventory_device_info(
    config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _attach_battery_editor_runtime(config_entry, coord)
    sw = BatteryScheduleEditorDaySwitch(coord, config_entry, day_key="mon")

    assert sw.device_info["name"] == "IQ Battery"


@pytest.mark.asyncio
async def test_battery_schedule_editor_day_switch_fallback_device_info_and_guard_paths(
    config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _attach_battery_editor_runtime(config_entry, coord)
    coord.inventory_view.type_device_info = lambda *_args: None
    sw = BatteryScheduleEditorDaySwitch(coord, config_entry, day_key="sun")

    assert sw.device_info["manufacturer"] == "Enphase"
    assert sw.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }

    runtime = config_entry.runtime_data
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    no_editor = BatteryScheduleEditorDaySwitch(coord, config_entry, day_key="sun")
    await no_editor.async_turn_on()
    await no_editor.async_turn_off()
    config_entry.runtime_data = runtime


@pytest.mark.asyncio
async def test_evse_schedule_editor_day_switch_uses_editor_state_and_actions(
    config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    editor = _attach_evse_editor_runtime(config_entry, coord)
    sw = EvseScheduleEditorDaySwitch(coord, config_entry, RANDOM_SERIAL, "mon")

    assert sw.available is True
    assert sw.is_on is True

    sw._editor.set_edit_day(RANDOM_SERIAL, "mon", False)  # noqa: SLF001
    assert sw.is_on is False

    await sw.async_turn_on()
    assert editor.form_state(RANDOM_SERIAL).days["mon"] is True

    await sw.async_turn_off()
    assert editor.form_state(RANDOM_SERIAL).days["mon"] is False


def test_evse_schedule_editor_day_switch_without_editor(
    config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    sw = EvseScheduleEditorDaySwitch(coord, config_entry, RANDOM_SERIAL, "mon")

    assert sw.available is False
    assert sw.is_on is False


def test_storm_guard_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._storm_guard_state = None  # noqa: SLF001
    coord._storm_evse_enabled = None  # noqa: SLF001
    sw = StormGuardSwitch(coord)
    assert sw.available is False

    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_evse_enabled = True  # noqa: SLF001
    assert sw.available is True
    assert sw.is_on is True


def test_storm_guard_switch_hidden_by_site_settings(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_show_storm_guard = False  # noqa: SLF001
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_evse_enabled = True  # noqa: SLF001
    sw = StormGuardSwitch(coord)
    assert sw.available is False


def test_storm_guard_switch_is_on_prefers_pending_state(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._storm_guard_state = "disabled"  # noqa: SLF001
    coord._storm_guard_pending_state = "enabled"  # noqa: SLF001
    coord._storm_guard_pending_expires_mono = (
        switch_mod.time.monotonic() + 60
    )  # noqa: SLF001

    assert StormGuardSwitch(coord).is_on is True


def test_storm_guard_switch_available_while_write_access_unknown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_evse_enabled = True  # noqa: SLF001
    sw = StormGuardSwitch(coord)
    assert sw.available is True


def test_storm_guard_switch_unavailable_when_write_access_denied(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_evse_enabled = True  # noqa: SLF001

    assert StormGuardSwitch(coord).available is False


def test_storm_guard_switch_unavailable_without_coordinator(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.last_update_success = False
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_evse_enabled = True  # noqa: SLF001
    sw = StormGuardSwitch(coord)
    assert sw.available is False


def test_savings_use_battery_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    sw = SavingsUseBatteryAfterPeakSwitch(coord)

    assert sw.available is True
    assert sw.is_on is False

    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    assert sw.is_on is True

    coord._battery_profile = "self-consumption"  # noqa: SLF001
    assert sw.available is False

    coord._battery_profile = "ai_optimisation"  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    assert sw.available is False
    assert sw.is_on is False


def test_savings_use_battery_switch_unavailable_without_coordinator(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.last_update_success = False
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    sw = SavingsUseBatteryAfterPeakSwitch(coord)
    assert sw.available is False


def test_savings_use_battery_switch_available_while_write_access_unknown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    sw = SavingsUseBatteryAfterPeakSwitch(coord)
    assert sw.available is True


def test_charge_from_grid_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    sw = ChargeFromGridSwitch(coord)
    assert sw.available is True
    assert sw.is_on is False

    coord._battery_hide_charge_from_grid = True  # noqa: SLF001
    assert sw.available is False


def test_charge_from_grid_switch_exposes_schedule_attributes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 95  # noqa: SLF001
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001

    attrs = ChargeFromGridSwitch(coord).extra_state_attributes

    assert attrs == {"write_pending": False, "write_age_seconds": None}


@pytest.mark.asyncio
async def test_charge_from_grid_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord.async_set_charge_from_grid = AsyncMock()
    sw = ChargeFromGridSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_charge_from_grid.assert_awaited_with(True)

    await sw.async_turn_off()
    coord.async_set_charge_from_grid.assert_awaited_with(False)


def test_charge_from_grid_schedule_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    sw = ChargeFromGridScheduleSwitch(coord)

    assert sw.available is True
    assert sw.is_on is True


def test_charge_from_grid_schedule_switch_unavailable_without_force_support(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord._battery_cfg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "forceScheduleSupported": False,
            }
        )
    )

    assert ChargeFromGridScheduleSwitch(coord).available is False


def test_charge_from_grid_schedule_switch_suggested_object_id(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    sw = ChargeFromGridScheduleSwitch(coord)
    assert sw.suggested_object_id == "charge_from_grid_schedule"


def test_charge_from_grid_schedule_switch_exposes_schedule_attributes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = False  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001
    coord._battery_cfg_schedule_status = "active"  # noqa: SLF001

    attrs = ChargeFromGridScheduleSwitch(coord).extra_state_attributes

    assert attrs["start_time"] == "02:00"
    assert attrs["end_time"] == "05:00"
    assert attrs["schedule_limit"] == 80
    assert attrs["schedule_status"] == "active"
    assert attrs["schedule_pending"] is False
    assert attrs["schedule_enabled"] is False


@pytest.mark.asyncio
async def test_charge_from_grid_schedule_switch_turn_on_off(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = False  # noqa: SLF001
    coord.async_set_charge_from_grid_schedule_enabled = AsyncMock()
    sw = ChargeFromGridScheduleSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_charge_from_grid_schedule_enabled.assert_awaited_with(True)

    await sw.async_turn_off()
    coord.async_set_charge_from_grid_schedule_enabled.assert_awaited_with(False)


def test_discharge_to_grid_schedule_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_dtg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_dtg_schedule_id = "sched-dtg"  # noqa: SLF001
    coord._battery_dtg_begin_time = 1080  # noqa: SLF001
    coord._battery_dtg_end_time = 1380  # noqa: SLF001
    coord._battery_dtg_schedule_enabled = True  # noqa: SLF001

    sw = DischargeToGridScheduleSwitch(coord)

    assert sw.available is True
    assert sw.is_on is True


def test_discharge_to_grid_schedule_switch_exposes_schedule_attributes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_dtg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {"show": True, "showDaySchedule": True, "scheduleSupported": True}
        )
    )
    coord._battery_dtg_schedule_id = "sched-dtg"  # noqa: SLF001
    coord._battery_dtg_begin_time = 1080  # noqa: SLF001
    coord._battery_dtg_end_time = 1380  # noqa: SLF001
    coord._battery_dtg_schedule_enabled = True  # noqa: SLF001
    coord._battery_dtg_schedule_limit = 25  # noqa: SLF001
    coord._battery_dtg_schedule_status = "pending"  # noqa: SLF001

    attrs = DischargeToGridScheduleSwitch(coord).extra_state_attributes

    assert attrs["start_time"] == "18:00"
    assert attrs["end_time"] == "23:00"
    assert attrs["schedule_limit"] == 25
    assert attrs["schedule_status"] == "pending"
    assert attrs["schedule_pending"] is True
    assert attrs["schedule_enabled"] is True


def test_restrict_battery_discharge_schedule_switch_exposes_schedule_attributes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_rbd_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_rbd_begin_time = 60  # noqa: SLF001
    coord._battery_rbd_end_time = 960  # noqa: SLF001
    coord._battery_rbd_schedule_limit = 100  # noqa: SLF001
    coord._battery_rbd_schedule_enabled = False  # noqa: SLF001
    coord._battery_rbd_schedule_status = "active"  # noqa: SLF001

    attrs = RestrictBatteryDischargeScheduleSwitch(coord).extra_state_attributes

    assert attrs["start_time"] == "01:00"
    assert attrs["end_time"] == "16:00"
    assert attrs["schedule_limit"] == 100
    assert attrs["schedule_status"] == "active"
    assert attrs["schedule_pending"] is False
    assert attrs["schedule_enabled"] is False


def test_base_battery_schedule_switch_extra_state_attributes_empty(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev import switch as switch_mod

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001

    sw = switch_mod._BaseBatteryScheduleSwitch(
        coord,
        unique_suffix="custom_schedule",
        availability_attr="custom_available",
        enabled_attr="custom_enabled",
        setter_name="async_custom_schedule_setter",
        suggested_object_id="custom_schedule",
    )

    assert sw._extra_schedule_state_attributes() == {}  # noqa: SLF001
    assert "schedule_status" not in sw.extra_state_attributes


@pytest.mark.asyncio
async def test_discharge_to_grid_schedule_switch_turn_on_off(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_dtg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_dtg_schedule_id = "sched-dtg"  # noqa: SLF001
    coord._battery_dtg_begin_time = 1080  # noqa: SLF001
    coord._battery_dtg_end_time = 1380  # noqa: SLF001
    coord.async_set_discharge_to_grid_schedule_enabled = AsyncMock()

    sw = DischargeToGridScheduleSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_discharge_to_grid_schedule_enabled.assert_awaited_with(True)

    await sw.async_turn_off()
    coord.async_set_discharge_to_grid_schedule_enabled.assert_awaited_with(False)


def test_restrict_battery_discharge_schedule_switch_availability(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_rbd_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_rbd_schedule_id = "sched-rbd"  # noqa: SLF001
    coord._battery_rbd_begin_time = 60  # noqa: SLF001
    coord._battery_rbd_end_time = 960  # noqa: SLF001
    coord._battery_rbd_schedule_enabled = True  # noqa: SLF001

    sw = RestrictBatteryDischargeScheduleSwitch(coord)

    assert sw.available is True
    assert sw.is_on is True


def test_restrict_battery_discharge_schedule_switch_available_without_schedule(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_rbd_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "enabled": True,
                "locked": False,
            }
        )
    )
    coord._battery_rbd_schedule_id = None  # noqa: SLF001
    coord._battery_rbd_begin_time = None  # noqa: SLF001
    coord._battery_rbd_end_time = None  # noqa: SLF001
    coord._battery_rbd_schedule_enabled = True  # noqa: SLF001

    sw = RestrictBatteryDischargeScheduleSwitch(coord)

    assert coord.restrict_battery_discharge_schedule_available is False
    assert coord.restrict_battery_discharge_schedule_supported is True
    assert sw.available is True
    assert sw.is_on is True


@pytest.mark.asyncio
async def test_restrict_battery_discharge_schedule_switch_turn_on_off(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_rbd_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    coord._battery_rbd_schedule_id = "sched-rbd"  # noqa: SLF001
    coord._battery_rbd_begin_time = 60  # noqa: SLF001
    coord._battery_rbd_end_time = 960  # noqa: SLF001
    coord.async_set_restrict_battery_discharge_schedule_enabled = AsyncMock()

    sw = RestrictBatteryDischargeScheduleSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_restrict_battery_discharge_schedule_enabled.assert_awaited_with(
        True
    )

    await sw.async_turn_off()
    coord.async_set_restrict_battery_discharge_schedule_enabled.assert_awaited_with(
        False
    )


def test_base_battery_schedule_switch_fallbacks(coordinator_factory) -> None:
    from custom_components.enphase_ev import switch as switch_mod

    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord.inventory_view.type_device_info = lambda _type_key: None
    coord.custom_schedule_available = True
    coord.custom_schedule_enabled = None
    coord.async_custom_schedule_enabled = AsyncMock()

    switch = switch_mod._BaseBatteryScheduleSwitch(
        coord,
        unique_suffix="custom_schedule",
        availability_attr="custom_schedule_available",
        enabled_attr="custom_schedule_enabled",
        setter_name="async_custom_schedule_enabled",
        suggested_object_id="custom_schedule",
    )

    assert switch.suggested_object_id == "custom_schedule"
    assert switch.is_on is False
    assert switch.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }

    expected = {"identifiers": {("enphase_ev", "provided")}}
    coord.inventory_view.type_device_info = MagicMock(return_value=expected)
    assert switch.device_info is expected

    coord.last_update_success = False
    assert switch.available is False


def test_site_switch_device_info_fallbacks(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.inventory_view.type_device_info = lambda _type_key: None

    assert StormGuardSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:envoy")
    }
    assert SavingsUseBatteryAfterPeakSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }
    assert ChargeFromGridSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }
    assert ChargeFromGridScheduleSwitch(coord).device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }


def test_site_switch_device_info_prefers_type_info_and_storm_guard_envoy_gate(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    expected_envoy = {"identifiers": {("enphase_ev", "envoy")}}
    expected_encharge = {"identifiers": {("enphase_ev", "encharge")}}
    coord.inventory_view.type_device_info = MagicMock(
        side_effect=[
            expected_envoy,
            expected_encharge,
            expected_encharge,
            expected_encharge,
        ]
    )

    assert StormGuardSwitch(coord).device_info is expected_envoy
    assert SavingsUseBatteryAfterPeakSwitch(coord).device_info is expected_encharge
    assert ChargeFromGridSwitch(coord).device_info is expected_encharge
    assert ChargeFromGridScheduleSwitch(coord).device_info is expected_encharge

    coord.inventory_view.type_device_info = lambda _type_key: None
    coord.inventory_view.has_type_for_entities = lambda _type_key: False
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._storm_evse_enabled = True  # noqa: SLF001
    assert StormGuardSwitch(coord).available is False


def test_charge_from_grid_switches_unavailable_when_coordinator_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.last_update_success = False
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001

    assert ChargeFromGridSwitch(coord).available is False
    assert ChargeFromGridScheduleSwitch(coord).available is False


@pytest.mark.asyncio
async def test_savings_use_battery_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord.async_set_savings_use_battery_after_peak = AsyncMock()
    sw = SavingsUseBatteryAfterPeakSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_savings_use_battery_after_peak.assert_awaited_with(True)

    await sw.async_turn_off()
    coord.async_set_savings_use_battery_after_peak.assert_awaited_with(False)


@pytest.mark.asyncio
async def test_storm_guard_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._storm_guard_state = "disabled"  # noqa: SLF001
    coord._storm_evse_enabled = False  # noqa: SLF001
    coord.async_set_storm_guard_enabled = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    sw = StormGuardSwitch(coord)

    await sw.async_turn_on()
    coord.async_set_storm_guard_enabled.assert_awaited_with(True)
    coord.async_request_refresh.assert_awaited_once()

    coord.async_request_refresh.reset_mock()
    await sw.async_turn_off()
    coord.async_set_storm_guard_enabled.assert_awaited_with(False)
    coord.async_request_refresh.assert_awaited_once()


def test_charging_switch_is_on_prefers_pending_expectation(coordinator_factory) -> None:
    coord = coordinator_factory({"charging": False})
    coord.evse_runtime.set_charging_expectation(RANDOM_SERIAL, True, hold_for=60)

    assert ChargingSwitch(coord, RANDOM_SERIAL).is_on is True


def test_storm_guard_evse_switch_availability(coordinator_factory) -> None:
    coord = coordinator_factory({"storm_guard_state": None, "storm_evse_enabled": None})
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    sw = StormGuardEvseSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False

    coord.data[RANDOM_SERIAL]["storm_guard_state"] = "enabled"
    coord.data[RANDOM_SERIAL]["storm_evse_enabled"] = True
    assert sw.available is True
    assert sw.is_on is True


def test_storm_guard_evse_switch_hidden_by_site_settings(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"storm_guard_state": "enabled", "storm_evse_enabled": True}
    )
    coord._battery_show_storm_guard = False  # noqa: SLF001
    sw = StormGuardEvseSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


def test_storm_guard_evse_switch_unavailable_without_confirmed_write_access(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(
        {"storm_guard_state": "enabled", "storm_evse_enabled": True}
    )
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    sw = StormGuardEvseSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


def test_storm_guard_evse_switch_unavailable_without_data(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.data = {}
    sw = StormGuardEvseSwitch(coord, RANDOM_SERIAL)
    assert sw.available is False


def test_storm_guard_evse_switch_ignores_feature_flag_for_availability(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(
        {
            "storm_guard_state": "enabled",
            "storm_evse_enabled": True,
            "storm_guard_supported": False,
        }
    )
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    sw = StormGuardEvseSwitch(coord, RANDOM_SERIAL)
    assert sw.available is True


def test_storm_guard_evse_switch_is_on_prefers_site_state(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"storm_guard_state": "enabled", "storm_evse_enabled": False}
    )
    coord._storm_evse_enabled = True  # noqa: SLF001

    assert StormGuardEvseSwitch(coord, RANDOM_SERIAL).is_on is True


@pytest.mark.asyncio
async def test_storm_guard_evse_switch_turn_on_off(coordinator_factory) -> None:
    coord = coordinator_factory(
        {"storm_guard_state": "enabled", "storm_evse_enabled": False}
    )
    coord.async_set_storm_evse_enabled = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    sw = StormGuardEvseSwitch(coord, RANDOM_SERIAL)

    await sw.async_turn_on()
    coord.async_set_storm_evse_enabled.assert_awaited_with(True)
    coord.async_request_refresh.assert_awaited_once()

    coord.async_request_refresh.reset_mock()
    await sw.async_turn_off()
    coord.async_set_storm_evse_enabled.assert_awaited_with(False)
    coord.async_request_refresh.assert_awaited_once()
