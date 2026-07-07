from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.enphase_ev.battery_schedule_editor import (
    BatteryScheduleEditorManager,
)
from custom_components.enphase_ev.const import OPT_BATTERY_SCHEDULES_ENABLED
from custom_components.enphase_ev.number import (
    BatteryReserveNumber,
    BatteryScheduleEditLimitNumber,
    BatteryShutdownLevelNumber,
    ChargingAmpsNumber,
    DefaultChargeLevelNumber,
    EnphaseTariffRateNumber,
    async_setup_entry,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.tariff import (
    TariffRateSnapshot,
    parse_tariff_rate,
    tariff_rate_sensor_specs,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _enable_battery_schedule_editor(coord) -> BatteryScheduleEditorManager:
    payload = {
        "cfg": {
            "scheduleStatus": "active",
            "details": [
                {
                    "scheduleId": "cfg-1",
                    "startTime": "02:00:00",
                    "endTime": "05:00:00",
                    "limit": 80,
                    "days": [1, 2, 3],
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
    editor = BatteryScheduleEditorManager(coord)
    editor.sync_from_coordinator()
    return editor


def _make_coordinator(hass, config_entry, data):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    with patch(
        "custom_components.enphase_ev.coordinator.async_get_clientsession",
        return_value=None,
    ):
        coord = EnphaseCoordinator(hass, config_entry.data, config_entry=config_entry)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
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
        ["encharge", "iqevse"],
    )
    coord.data = data
    coord.last_set_amps = {}
    coord.async_request_refresh = AsyncMock()
    coord.set_last_set_amps = MagicMock(wraps=coord.set_last_set_amps)
    coord.client = SimpleNamespace()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    return coord


def test_evse_resolved_charge_mode_handles_data_access_failure() -> None:
    from custom_components.enphase_ev.entity import evse_resolved_charge_mode

    class _BoomCoord:
        @property
        def data(self):
            raise RuntimeError("boom")

    assert evse_resolved_charge_mode(_BoomCoord(), RANDOM_SERIAL) is None


def test_number_battery_write_access_confirmed_falls_back_to_roles() -> None:
    from custom_components.enphase_ev import number as number_mod

    coord = SimpleNamespace(
        battery_write_access_confirmed=None,
        battery_user_is_owner=None,
        battery_user_is_installer=None,
    )
    assert number_mod._battery_write_access_confirmed(coord) is False

    coord.battery_user_is_owner = True
    assert number_mod._battery_write_access_confirmed(coord) is True

    coord.battery_user_is_owner = False
    coord.battery_user_is_installer = True
    assert number_mod._battery_write_access_confirmed(coord) is True

    coord.battery_user_is_installer = False
    coord.battery_write_access_confirmed = True
    assert number_mod._battery_write_access_confirmed(coord) is True


def test_retained_site_number_unique_ids_follow_scheduler_state() -> None:
    from custom_components.enphase_ev import number as number_mod

    coord = SimpleNamespace(
        site_id="site",
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "encharge"
        ),
        client=SimpleNamespace(),
        battery_write_access_confirmed=True,
        battery_user_is_owner=False,
        battery_user_is_installer=False,
        battery_reserve_editable=True,
    )

    assert number_mod._retained_site_number_unique_ids(
        coord, SimpleNamespace(options={OPT_BATTERY_SCHEDULES_ENABLED: False})
    ) == {
        "enphase_ev_site_site_battery_reserve",
        "enphase_ev_site_site_battery_shutdown_level",
    }

    coord.client.battery_schedules = lambda: None
    coord.client.create_battery_schedule = lambda: None
    coord.client.update_battery_schedule = lambda: None
    coord.client.delete_battery_schedule = lambda: None
    assert number_mod._retained_site_number_unique_ids(
        coord, SimpleNamespace(options={OPT_BATTERY_SCHEDULES_ENABLED: True})
    ) == {
        "enphase_ev_site_site_battery_reserve",
        "enphase_ev_site_site_battery_shutdown_level",
        "enphase_ev_site_site_battery_schedule_edit_limit",
    }

    coord.inventory_view.has_type_for_entities = lambda _type_key: False
    assert (
        number_mod._retained_site_number_unique_ids(
            coord, SimpleNamespace(options={OPT_BATTERY_SCHEDULES_ENABLED: True})
        )
        == set()
    )


@pytest.mark.asyncio
async def test_async_setup_entry_adds_site_numbers_and_charger_numbers(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    editor = _enable_battery_schedule_editor(coord)
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: True})
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        battery_schedule_editor=editor,
    )

    added: list = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert any(isinstance(ent, BatteryReserveNumber) for ent in added)
    assert any(isinstance(ent, BatteryShutdownLevelNumber) for ent in added)
    assert any(isinstance(ent, BatteryScheduleEditLimitNumber) for ent in added)
    assert any(isinstance(ent, ChargingAmpsNumber) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_default_charge_level_when_supported(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage",
                "default_charge_level_supported": True,
                "default_charge_level": 30,
            }
        }
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert any(isinstance(ent, ChargingAmpsNumber) for ent in added)
    assert any(isinstance(ent, DefaultChargeLevelNumber) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_hides_schedule_edit_limit_when_scheduler_disabled(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_write_access_confirmed = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: False})
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert any(isinstance(ent, BatteryReserveNumber) for ent in added)
    assert any(isinstance(ent, BatteryShutdownLevelNumber) for ent in added)
    assert not any(isinstance(ent, BatteryScheduleEditLimitNumber) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_stale_number_entities_when_inventory_ready(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    coord._devices_inventory_ready = True  # noqa: SLF001
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: False})
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "number",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_battery_new_schedule_limit",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    remove_spy.assert_called_with(stale.entity_id)


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_site_numbers_while_permission_unknown(
    hass, config_entry, monkeypatch
) -> None:
    coord = SimpleNamespace()
    coord.site_id = "123456"
    coord.battery_write_access_confirmed = False
    coord.inventory_view = SimpleNamespace(
        has_type_for_entities=lambda type_key: type_key == "encharge",
        type_device_info=lambda _type_key: None,
    )
    coord.serials = {RANDOM_SERIAL}
    coord._serial_order = [RANDOM_SERIAL]
    coord.data = {RANDOM_SERIAL: {"name": "Garage EV"}}
    coord.iter_serials = lambda: [RANDOM_SERIAL]
    listener_callbacks = []

    def _capture_listener(callback):
        listener_callbacks.append(callback)
        return lambda: None

    coord.async_add_listener = MagicMock(side_effect=_capture_listener)
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.last_update_success = True
    coord.battery_reserve_editable = True
    coord.battery_shutdown_level_available = True

    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    prune_spy = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.number.prune_managed_entities", prune_spy
    )

    added = []
    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert any(isinstance(ent, ChargingAmpsNumber) for ent in added)
    reserve = next(ent for ent in added if isinstance(ent, BatteryReserveNumber))
    shutdown = next(ent for ent in added if isinstance(ent, BatteryShutdownLevelNumber))
    assert reserve.available is True
    assert shutdown.available is True
    assert not any(isinstance(ent, BatteryScheduleEditLimitNumber) for ent in added)

    reserve_unique_id = f"enphase_ev_site_{coord.site_id}_battery_reserve"
    shutdown_unique_id = f"enphase_ev_site_{coord.site_id}_battery_shutdown_level"

    added.clear()
    prune_spy.reset_mock()
    coord.battery_user_is_owner = False
    coord.battery_user_is_installer = False
    listener_callbacks[0]()

    active_unique_ids = prune_spy.call_args.kwargs["active_unique_ids"]
    assert reserve_unique_id in active_unique_ids
    assert shutdown_unique_id in active_unique_ids
    assert reserve.available is False
    assert shutdown.available is False

    prune_spy.reset_mock()
    coord.battery_write_access_confirmed = True
    listener_callbacks[0]()

    assert not any(isinstance(ent, BatteryReserveNumber) for ent in added)
    assert not any(isinstance(ent, BatteryShutdownLevelNumber) for ent in added)
    active_unique_ids = prune_spy.call_args.kwargs["active_unique_ids"]
    assert reserve_unique_id in active_unique_ids
    assert shutdown_unique_id in active_unique_ids

    prune_spy.reset_mock()
    coord.inventory_view = SimpleNamespace(
        has_type_for_entities=lambda _type_key: False,
        type_device_info=lambda _type_key: None,
    )
    listener_callbacks[0]()

    active_unique_ids = prune_spy.call_args.kwargs["active_unique_ids"]
    assert reserve_unique_id not in active_unique_ids
    assert shutdown_unique_id not in active_unique_ids


@pytest.mark.asyncio
async def test_async_setup_entry_skips_site_numbers_when_write_access_denied(
    hass, config_entry
) -> None:
    coord = SimpleNamespace()
    coord.site_id = "123456"
    coord.battery_write_access_confirmed = False
    coord.battery_user_is_owner = False
    coord.battery_user_is_installer = False
    coord.inventory_view = SimpleNamespace(
        has_type_for_entities=lambda type_key: type_key == "encharge",
        type_device_info=lambda _type_key: None,
    )
    coord.serials = {RANDOM_SERIAL}
    coord._serial_order = [RANDOM_SERIAL]
    coord.data = {RANDOM_SERIAL: {"name": "Garage EV"}}
    coord.iter_serials = lambda: [RANDOM_SERIAL]
    coord.async_add_listener = MagicMock(return_value=lambda: None)
    coord._devices_inventory_ready = True  # noqa: SLF001

    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []
    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert any(isinstance(ent, ChargingAmpsNumber) for ent in added)
    assert not any(isinstance(ent, BatteryReserveNumber) for ent in added)
    assert not any(isinstance(ent, BatteryShutdownLevelNumber) for ent in added)


def test_charging_number_converts_values(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": "36",
                "min_amp": "6",
                "max_amp": "48",
            }
        },
    )
    coord.pick_start_amps = MagicMock(return_value=30)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == 36.0
    assert number.native_min_value == 6.0
    assert number.native_max_value == 48.0
    assert number.native_step == 1.0


def test_charging_number_falls_back_to_safe_defaults(hass, config_entry) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging_level": "bad", "min_amp": "bad", "max_amp": None}},
    )
    coord.pick_start_amps = MagicMock(return_value=30)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    assert number.native_value == 30.0
    assert number.native_min_value == 6.0
    assert number.native_max_value == 40.0


def test_charging_number_helper_coercions_cover_edge_inputs() -> None:
    assert ChargingAmpsNumber._safe_limit_active(None) is False
    assert ChargingAmpsNumber._safe_limit_active(True) is True
    assert ChargingAmpsNumber._safe_limit_active("bad") is False
    assert ChargingAmpsNumber._charging_active(None) is False
    assert ChargingAmpsNumber._charging_active(True) is True
    assert ChargingAmpsNumber._charging_active(1) is True
    assert ChargingAmpsNumber._charging_active("yes") is True
    assert ChargingAmpsNumber._charging_active("off") is False
    assert ChargingAmpsNumber._charging_active("mystery") is False
    assert ChargingAmpsNumber._charging_active(object()) is False
    assert ChargingAmpsNumber._coerce_amp("16.0") == 16
    assert ChargingAmpsNumber._coerce_amp("bad") is None


@pytest.mark.asyncio
async def test_charging_number_set_value_records_and_restarts_when_active(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {RANDOM_SERIAL: {"charging": True, "charging_level": 20}},
    )
    coord.schedule_amp_restart = MagicMock()

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)
    await number.async_set_native_value(24)

    coord.set_last_set_amps.assert_called_once_with(RANDOM_SERIAL, 24)
    coord.async_request_refresh.assert_awaited_once()
    coord.schedule_amp_restart.assert_called_once_with(RANDOM_SERIAL)


def test_charging_number_uses_pick_start_for_non_applicable_and_safe_limit(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "charging_level": 20,
                "charge_mode_pref": "GREEN_CHARGING",
                "safe_limit_state": 1,
                "charging": "yes",
                "max_amp": "bad",
            }
        },
    )
    coord.pick_start_amps = MagicMock(return_value=26)

    number = ChargingAmpsNumber(coord, RANDOM_SERIAL)

    assert number.native_value == 26.0
    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "MANUAL"
    assert number.native_value == 8.0
    coord.data[RANDOM_SERIAL]["min_amp"] = "16"
    assert number.native_value == 16.0
    assert number.native_max_value == 40.0


def test_default_charge_level_number_uses_config_value_and_bounds(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "default_charge_level_supported": True,
                "default_charge_level_supported_source": "charger_config",
                "default_charge_level": "30",
                "charging_level": "24",
                "min_amp": "6",
                "max_amp": "48",
                "max_current": "50",
                "amp_granularity": "2",
            }
        },
    )

    number = DefaultChargeLevelNumber(coord, RANDOM_SERIAL)

    assert number.available is True
    assert number.native_value == 30.0
    assert number.native_min_value == 6.0
    assert number.native_max_value == 48.0
    assert number.native_step == 2.0
    assert number.extra_state_attributes == {
        "min_amp": 6,
        "max_amp": 48,
        "max_current": 50,
        "amp_granularity": 2,
        "charging_amps": 24,
        "default_charge_level_supported_source": "charger_config",
    }


def test_default_charge_level_number_unavailable_when_unsupported(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "default_charge_level_supported": False,
                "default_charge_level": "30",
            }
        },
    )

    number = DefaultChargeLevelNumber(coord, RANDOM_SERIAL)

    assert number.available is False
    assert number.native_value == 30.0


@pytest.mark.asyncio
async def test_default_charge_level_number_writes_persistent_default(
    hass, config_entry
) -> None:
    coord = _make_coordinator(
        hass,
        config_entry,
        {
            RANDOM_SERIAL: {
                "default_charge_level_supported": True,
                "default_charge_level": 24,
            }
        },
    )
    coord.evse_runtime.async_set_default_charge_level = AsyncMock()

    number = DefaultChargeLevelNumber(coord, RANDOM_SERIAL)
    await number.async_set_native_value(30)

    coord.evse_runtime.async_set_default_charge_level.assert_awaited_once_with(
        RANDOM_SERIAL,
        30,
    )


def test_battery_reserve_number_dynamic_bounds_and_device_info(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_backup_percentage = 25  # noqa: SLF001
    coord._battery_backup_percentage_min = 10  # noqa: SLF001
    coord._battery_backup_percentage_max = 90  # noqa: SLF001
    coord.inventory_view.type_device_info = lambda _type_key: None

    number = BatteryReserveNumber(coord)

    assert number.available is True
    assert number.native_value == 25.0
    assert number.native_min_value == 10.0
    assert number.native_max_value == 90.0
    assert number.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }


@pytest.mark.asyncio
async def test_battery_reserve_number_sets_value(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.async_set_battery_reserve = AsyncMock()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_backup_percentage = 25  # noqa: SLF001

    await BatteryReserveNumber(coord).async_set_native_value(40)

    coord.async_set_battery_reserve.assert_awaited_once_with(40)


@pytest.mark.asyncio
async def test_battery_reserve_number_ignores_current_value(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.async_set_battery_reserve = AsyncMock()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_backup_percentage = 25  # noqa: SLF001

    await BatteryReserveNumber(coord).async_set_native_value(25)

    coord.async_set_battery_reserve.assert_not_awaited()


def test_battery_reserve_number_handles_none_and_super_unavailable(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord.last_update_success = False
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_backup_percentage = None  # noqa: SLF001

    number = BatteryReserveNumber(coord)

    assert number.available is False
    assert number.native_value is None


def test_battery_shutdown_level_number_bounds_and_availability(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001
    coord._battery_very_low_soc_max = 25  # noqa: SLF001

    number = BatteryShutdownLevelNumber(coord)

    assert number.available is True
    assert number.native_value == 15.0
    assert number.native_min_value == 10.0
    assert number.native_max_value == 25.0


@pytest.mark.asyncio
async def test_battery_shutdown_level_number_sets_value(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord.async_set_battery_shutdown_level = AsyncMock()

    await BatteryShutdownLevelNumber(coord).async_set_native_value(12)

    coord.async_set_battery_shutdown_level.assert_awaited_once_with(12)


@pytest.mark.asyncio
async def test_battery_shutdown_level_number_ignores_current_value(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord.async_set_battery_shutdown_level = AsyncMock()

    await BatteryShutdownLevelNumber(coord).async_set_native_value(15)

    coord.async_set_battery_shutdown_level.assert_not_awaited()


def test_battery_shutdown_level_number_handles_none_and_super_unavailable(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord.last_update_success = False
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = None  # noqa: SLF001

    number = BatteryShutdownLevelNumber(coord)

    assert number.available is False
    assert number.native_value is None


def test_battery_schedule_edit_limit_number_uses_editor_and_device_info(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    editor = _enable_battery_schedule_editor(coord)
    expected = {"identifiers": {("enphase_ev", "provided")}}
    coord.inventory_view.type_device_info = MagicMock(return_value=expected)
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: True})
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        battery_schedule_editor=editor,
    )

    number = BatteryScheduleEditLimitNumber(coord, config_entry)

    assert number.available is True
    assert number.native_value == 80.0
    assert number.device_info is expected


@pytest.mark.asyncio
async def test_battery_schedule_edit_limit_number_sets_editor_limit(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    editor = _enable_battery_schedule_editor(coord)
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: True})
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        battery_schedule_editor=editor,
    )

    number = BatteryScheduleEditLimitNumber(coord, config_entry)
    await number.async_set_native_value(95)

    assert editor.edit.limit == 95


def test_battery_schedule_edit_limit_number_unavailable_without_editor(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_write_access_confirmed = True  # noqa: SLF001
    coord.client.battery_schedules = AsyncMock()
    coord.client.create_battery_schedule = AsyncMock()
    coord.client.update_battery_schedule = AsyncMock()
    coord.client.delete_battery_schedule = AsyncMock()
    object.__setattr__(config_entry, "options", {OPT_BATTERY_SCHEDULES_ENABLED: True})
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    assert BatteryScheduleEditLimitNumber(coord, config_entry).available is False


def test_tariff_rate_number_exposes_value_unit_attributes_and_device_info(
    hass, config_entry
) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord.client.site_tariff = AsyncMock()
    coord.client.site_tariff_update = AsyncMock()
    envoy_info = {"identifiers": {("enphase_ev", "envoy")}}
    coord.inventory_view.type_device_info = lambda type_key: (
        envoy_info if type_key == "envoy" else None
    )
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "currency": "$",
            "purchase": {
                "typeKind": "single",
                "typeId": "tou",
                "source": "manual",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [
                                    {
                                        "id": "peak-1",
                                        "type": "peak",
                                        "rate": "0.31",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    spec = tariff_rate_sensor_specs(coord.tariff_import_rate)[0]

    number = EnphaseTariffRateNumber(coord, spec, is_import=True)

    assert number.available is True
    assert number.native_value == 0.31
    assert number.native_unit_of_measurement == "$/kWh"
    assert number.extra_state_attributes["tariff_locator"]["branch"] == "purchase"
    assert number.entity_category == "config"
    assert number.device_info is envoy_info

    cloud_info = {"identifiers": {("enphase_ev", "cloud")}}
    coord.inventory_view.type_device_info = lambda type_key: (
        cloud_info if type_key == "cloud" else None
    )
    assert number.device_info is cloud_info


def test_tariff_rate_number_fallback_paths(hass, config_entry) -> None:
    from custom_components.enphase_ev import number as number_mod

    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord.client.site_tariff = AsyncMock()
    coord.client.site_tariff_update = AsyncMock()
    coord.inventory_runtime._set_type_device_buckets({}, [])  # noqa: SLF001
    coord.inventory_view.type_device_info = lambda *_args, **_kwargs: None
    coord.tariff_import_rate = TariffRateSnapshot(
        state="Flat",
        rate_structure="Flat",
        variation_type="Single",
        source="manual",
        currency=None,
        export_plan=None,
        seasons=(
            {
                "id": "default",
                "days": [{"id": "week", "periods": [{"rate": "0.18"}]}],
            },
        ),
    )
    assert number_mod._tariff_rate_number_entities(coord) == {}

    coord.tariff_import_rate = parse_tariff_rate(
        {
            "purchase": {
                "typeKind": "single",
                "typeId": "flat",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"id": "off-peak", "rate": "0.18"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    spec = tariff_rate_sensor_specs(coord.tariff_import_rate)[0]
    number = EnphaseTariffRateNumber(coord, spec, is_import=True)
    number._detail_key = "missing"  # noqa: SLF001

    assert number.available is False
    assert number.native_value is None
    assert number.native_unit_of_measurement is None
    assert number.extra_state_attributes == {}
    assert number.device_info["identifiers"] == {
        ("enphase_ev", f"site:{coord.site_id}")
    }


def test_tariff_rate_number_uses_home_assistant_currency(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord.client.site_tariff = AsyncMock()
    coord.client.site_tariff_update = AsyncMock()
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "purchase": {
                "typeKind": "single",
                "typeId": "flat",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"id": "off-peak", "rate": "0.18"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    spec = tariff_rate_sensor_specs(coord.tariff_import_rate)[0]
    number = EnphaseTariffRateNumber(coord, spec, is_import=True)
    number.hass = hass
    hass.config.currency = "EUR"

    assert number.native_unit_of_measurement == "EUR/kWh"


@pytest.mark.asyncio
async def test_tariff_rate_number_sets_value(hass, config_entry) -> None:
    coord = _make_coordinator(hass, config_entry, {RANDOM_SERIAL: {}})
    coord.tariff_runtime.async_set_tariff_rate = AsyncMock()
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "purchase": {
                "typeKind": "single",
                "typeId": "flat",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"id": "off-peak", "rate": "0.18"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    spec = tariff_rate_sensor_specs(coord.tariff_import_rate)[0]

    await EnphaseTariffRateNumber(coord, spec, is_import=True).async_set_native_value(
        0.22
    )

    coord.tariff_runtime.async_set_tariff_rate.assert_awaited_once_with(
        spec["attributes"]["tariff_locator"], 0.22
    )


@pytest.mark.asyncio
async def test_tariff_rate_numbers_are_created_and_stale_entries_pruned(
    hass, config_entry
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = _make_coordinator(hass, config_entry, {})
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "purchase": {
                "typeKind": "single",
                "typeId": "flat",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"id": "off-peak", "rate": "0.18"}],
                            }
                        ],
                    }
                ],
            },
        },
        "purchase",
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    stale_unique_id = f"enphase_ev_site_{coord.site_id}_tariff_import_rate_old_number"
    ent_reg.async_get_or_create(
        "number",
        "enphase_ev",
        stale_unique_id,
        config_entry=config_entry,
    )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    tariff_numbers = [
        entity.unique_id for entity in added if "tariff_import_rate" in entity.unique_id
    ]
    assert tariff_numbers == [
        f"enphase_ev_site_{coord.site_id}_tariff_import_rate_default_week_off_peak_number"
    ]
    assert ent_reg.async_get_entity_id("number", "enphase_ev", stale_unique_id) is None


@pytest.mark.asyncio
async def test_tariff_rate_number_registry_is_kept_when_loaded_spec_disappears(
    hass, config_entry, monkeypatch
) -> None:
    payload = {
        "purchase": {
            "typeKind": "single",
            "typeId": "flat",
            "seasons": [
                {
                    "id": "default",
                    "days": [
                        {
                            "id": "week",
                            "periods": [{"id": "off-peak", "rate": "0.18"}],
                        }
                    ],
                }
            ],
        },
    }
    coord = _make_coordinator(hass, config_entry, {})
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.tariff_import_rate = parse_tariff_rate(payload, "purchase")
    listener_callbacks = []

    def _capture_listener(callback):
        listener_callbacks.append(callback)
        return lambda: None

    coord.async_add_listener = MagicMock(side_effect=_capture_listener)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    prune_spy = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.number.prune_managed_entities", prune_spy
    )
    added = []

    await async_setup_entry(
        hass, config_entry, lambda entities, **_: added.extend(entities)
    )

    unique_id = (
        f"enphase_ev_site_{coord.site_id}"
        "_tariff_import_rate_default_week_off_peak_number"
    )
    assert any(
        isinstance(entity, EnphaseTariffRateNumber) and entity.unique_id == unique_id
        for entity in added
    )

    added.clear()
    prune_spy.reset_mock()
    coord.tariff_import_rate = None
    listener_callbacks[0]()

    active_unique_ids = prune_spy.call_args.kwargs["active_unique_ids"]
    assert unique_id in active_unique_ids

    prune_spy.reset_mock()
    coord.tariff_import_rate = parse_tariff_rate(payload, "purchase")
    listener_callbacks[0]()

    assert not any(isinstance(entity, EnphaseTariffRateNumber) for entity in added)
    active_unique_ids = prune_spy.call_args.kwargs["active_unique_ids"]
    assert unique_id in active_unique_ids
