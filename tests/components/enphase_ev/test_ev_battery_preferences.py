"""Regression coverage for the site BatteryConfig EV preference contract."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.enphase_ev.api_client import battery_surface
from custom_components.enphase_ev.ev_battery_preferences import (
    EVBatteryPreference,
    EVBatteryPreferences,
    parse_preference,
    preferences_for,
)
from custom_components.enphase_ev.number import EVBatteryLimitNumber
from custom_components.enphase_ev.switch import GreenBatterySwitch

from .random_ids import RANDOM_SERIAL


def payload(enabled=False, limit=90, minimum=10):
    return {
        "data": {
            "devices": {
                "iqEvse": {
                    "useBatteryForEVSE": enabled,
                    "batteryLimit": limit,
                    "minBatteryLimit": minimum,
                }
            },
            "batteryBackupPercentage": 37,
            "batteryBackupPercentageMax": 80,
        }
    }


@pytest.fixture
def preferences():
    coord = SimpleNamespace(
        client=SimpleNamespace(
            battery_settings_details=AsyncMock(), set_ev_battery_preference=AsyncMock()
        ),
        battery_write_access_confirmed=True,
        battery_system_task=False,
        publish_runtime_state_update=Mock(),
    )
    runtime = SimpleNamespace(
        coordinator=coord, async_ensure_battery_write_access_confirmed=AsyncMock()
    )
    prefs = EVBatteryPreferences(runtime)
    coord.battery_runtime = runtime
    runtime.ev_preferences = prefs
    state = payload()

    async def read():
        return state

    async def write(*, enabled, limit):
        nonlocal state
        state = payload(enabled, limit)
        return {"type": "iqevse-battery-preference", "data": {"message": "success"}}

    coord.client.battery_settings_details.side_effect = read
    coord.client.set_ev_battery_preference.side_effect = write
    prefs.observe(state)
    return prefs


@pytest.mark.parametrize(
    "enabled,limit,minimum",
    [(False, 0, 20), (False, 95, 10), (True, 91, 20), (True, 100, 10)],
)
def test_parse_valid(enabled, limit, minimum):
    assert parse_preference(payload(enabled, limit, minimum)) == EVBatteryPreference(
        enabled, limit, minimum
    )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"data": []},
        {"data": {"devices": []}},
        {"data": {"devices": {"iqEvse": {}}}},
        payload(True, 0, 20),
        payload(False, 9, 10),
        payload(True, 101),
        payload(True, 90, -1),
        payload(True, 90, 101),
        payload("true"),
        payload(True, True),
        payload(True, 90.5),
        payload(True, 90, None),
    ],
)
def test_parse_missing_invalid(bad):
    assert parse_preference(bad) is None


@pytest.mark.asyncio
async def test_all_actions_preserve_companion(preferences):
    prefs = preferences
    await prefs.async_update(enabled=True)
    await prefs.async_update(limit=95)
    await prefs.async_update(enabled=False)
    await prefs.async_update(limit=91)
    assert (
        prefs.runtime.coordinator.client.set_ev_battery_preference.await_args_list
        == [
            call(enabled=True, limit=90),
            call(enabled=True, limit=95),
            call(enabled=False, limit=95),
            call(enabled=False, limit=91),
        ]
    )
    assert prefs.value == EVBatteryPreference(False, 91, 10)


@pytest.mark.asyncio
async def test_concurrent_actions_read_companion_after_lock(preferences):
    await asyncio.gather(
        preferences.async_update(limit=95), preferences.async_update(enabled=True)
    )
    assert preferences.value == EVBatteryPreference(True, 95, 10)
    assert (
        preferences.runtime.coordinator.client.set_ev_battery_preference.await_args_list
        == [
            call(enabled=False, limit=95),
            call(enabled=True, limit=95),
        ]
    )


@pytest.mark.parametrize("minimum", [10, 20])
@pytest.mark.asyncio
async def test_disabled_zero_enable_uses_dynamic_minimum(preferences, minimum):
    client = preferences.runtime.coordinator.client
    client.battery_settings_details.side_effect = [
        payload(False, 0, minimum),
        payload(True, minimum, minimum),
    ]
    await preferences.async_update(enabled=True)
    client.set_ev_battery_preference.assert_awaited_once_with(
        enabled=True, limit=minimum
    )
    assert preferences.value.minimum == minimum


@pytest.mark.parametrize("limit", [9, 101, 90.5, True, float("nan"), float("inf")])
@pytest.mark.asyncio
async def test_invalid_percentage_never_writes(preferences, limit):
    with pytest.raises(ServiceValidationError):
        await preferences.async_update(limit=limit)
    preferences.runtime.coordinator.client.set_ev_battery_preference.assert_not_awaited()


@pytest.mark.parametrize("state", [None, {}, payload(True, 0), payload(True, 95, None)])
@pytest.mark.asyncio
async def test_missing_fields_never_fall_back_or_write(preferences, state):
    preferences.runtime.coordinator.client.battery_settings_details.side_effect = [
        state
    ]
    with pytest.raises(ServiceValidationError):
        await preferences.async_update(enabled=True)
    assert preferences.seen
    assert not preferences.available
    preferences.runtime.coordinator.client.set_ev_battery_preference.assert_not_awaited()


@pytest.mark.parametrize("kind", ["unsupported", "permission", "busy"])
@pytest.mark.asyncio
async def test_capability_permission_guards(preferences, kind):
    if kind == "unsupported":
        preferences.observe_capabilities({"isUseBatteryForEVSESupported": False})
    elif kind == "permission":
        preferences.runtime.coordinator.battery_write_access_confirmed = False
    else:
        preferences.runtime.coordinator.battery_system_task = True
    with pytest.raises(ServiceValidationError):
        await preferences.async_update(enabled=True)
    preferences.runtime.coordinator.client.set_ev_battery_preference.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", ["STORM_GUARD_ACTIVE", "permission denied", "timeout"]
)
@pytest.mark.asyncio
async def test_write_failure_not_optimistic(preferences, failure):
    preferences.runtime.coordinator.client.set_ev_battery_preference.side_effect = (
        HomeAssistantError(failure)
    )
    with pytest.raises(HomeAssistantError, match=failure):
        await preferences.async_update(enabled=True)
    assert preferences.value is None
    assert preferences.seen


@pytest.mark.asyncio
async def test_success_readback_mismatch(preferences):
    preferences.runtime.coordinator.client.battery_settings_details.side_effect = [
        payload(),
        payload(),
    ]
    with pytest.raises(HomeAssistantError, match="not confirmed"):
        await preferences.async_update(enabled=True)
    assert preferences.value is None


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"type": "iqevse-battery-preference", "data": {"message": "failure"}},
        {"type": "iqevse-battery-preference", "data": None},
    ],
)
@pytest.mark.asyncio
async def test_invalid_success_response(preferences, response):
    client = preferences.runtime.coordinator.client
    client.set_ev_battery_preference.side_effect = None
    client.set_ev_battery_preference.return_value = response
    with pytest.raises(HomeAssistantError, match="acknowledge"):
        await preferences.async_update(enabled=True)


@pytest.mark.asyncio
async def test_readback_failure(preferences):
    preferences.runtime.coordinator.client.battery_settings_details.side_effect = [
        payload(),
        TimeoutError(),
    ]
    with pytest.raises(TimeoutError):
        await preferences.async_update(enabled=True)
    assert not preferences.available


def test_capability_and_accessor(preferences):
    preferences.observe_capabilities({"isUseBatteryForEVSESupported": True})
    assert preferences.available
    preferences.observe_capabilities({})
    assert preferences.available
    assert preferences_for(preferences.runtime.coordinator) is preferences
    assert preferences_for(object()) is None
    preferences.observe(None)
    assert preferences.seen
    assert not preferences.available


@pytest.mark.asyncio
async def test_api_shape():
    client = SimpleNamespace(_site="site", _battery_config_request=AsyncMock())
    await battery_surface.set_ev_battery_preference(client, enabled=False, limit=95)
    client._battery_config_request.assert_awaited_once_with(
        "PUT",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/device/battery/preference/site",
        json_body={"useBatteryForEVSE": False, "batteryLimit": 95},
        endpoint_family="ev_battery_preference",
        bootstrap_xsrf=True,
    )


@pytest.mark.asyncio
async def test_entities_preserve_switch_identity_and_site_number(coordinator_factory):
    coord = coordinator_factory()
    coord.battery_runtime.async_ensure_battery_write_access_confirmed = AsyncMock()
    prefs = coord.battery_runtime.ev_preferences
    prefs.observe(payload(True, 95, 20))
    prefs.async_update = AsyncMock()
    coord.battery_state._battery_user_is_owner = True
    switch = GreenBatterySwitch(coord, RANDOM_SERIAL)
    number = EVBatteryLimitNumber(coord)
    assert switch.unique_id == f"enphase_ev_{RANDOM_SERIAL}_green_battery"
    assert number.unique_id == f"enphase_ev_site_{coord.site_id}_ev_battery_limit"
    assert switch.is_on is True
    assert number.native_value == 95
    assert number.native_min_value == 20
    assert number.native_max_value == 100
    assert number.native_step == 1
    await switch.async_turn_off()
    prefs.async_update.assert_awaited_with(enabled=False)
    await number.async_set_native_value(91)
    prefs.async_update.assert_awaited_with(limit=91)
    prefs.observe(payload(False, 0, 20))
    assert number.native_value is None
    prefs.observe({})
    assert not switch.available
    assert not number.available
    assert number.native_value is None
    assert number.native_min_value == 0


@pytest.mark.asyncio
async def test_discovery_retains_registry_customizations(
    hass, config_entry, coordinator_factory, monkeypatch
):
    from homeassistant.helpers import entity_registry as er
    from custom_components.enphase_ev import number, switch
    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData

    coord = coordinator_factory()
    coord.battery_state._battery_user_is_owner = True
    coord.battery_runtime.ev_preferences.observe(payload())
    coord.data[RANDOM_SERIAL]["green_battery_supported"] = False
    coord._devices_inventory_ready = True
    monkeypatch.setattr(number, "_type_available", lambda *args: True)
    monkeypatch.setattr(switch, "_site_has_battery", lambda *args: True)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "switch",
        "enphase_ev",
        f"enphase_ev_{RANDOM_SERIAL}_green_battery",
        config_entry=config_entry,
    )
    registry.async_update_entity(old.entity_id, name="My existing automation switch")
    added_switches, added_numbers = [], []
    await switch.async_setup_entry(
        hass, config_entry, lambda entities, **kwargs: added_switches.extend(entities)
    )
    await number.async_setup_entry(
        hass, config_entry, lambda entities, **kwargs: added_numbers.extend(entities)
    )
    assert len([e for e in added_switches if isinstance(e, GreenBatterySwitch)]) == 1
    assert len([e for e in added_numbers if isinstance(e, EVBatteryLimitNumber)]) == 1
    coord.battery_runtime.ev_preferences.observe({})
    coord.publish_runtime_state_update("test")
    assert registry.async_get(old.entity_id).name == "My existing automation switch"
    assert len([e for e in added_switches if isinstance(e, GreenBatterySwitch)]) == 1
    assert len([e for e in added_numbers if isinstance(e, EVBatteryLimitNumber)]) == 1


@pytest.mark.asyncio
async def test_client_facade():
    from custom_components.enphase_ev.api import EnphaseEVClient

    client = SimpleNamespace(_site="site", _battery_config_request=AsyncMock())
    await EnphaseEVClient.set_ev_battery_preference(client, enabled=True, limit=91)
    assert client._battery_config_request.await_args.kwargs["json_body"] == {
        "useBatteryForEVSE": True,
        "batteryLimit": 91,
    }


@pytest.mark.asyncio
async def test_poll_discovers_and_invalidates_preferences(coordinator_factory):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord.client.battery_settings_details = AsyncMock(return_value=payload())
    assert await runtime.async_refresh_battery_settings(force=True)
    assert runtime.ev_preferences.value == EVBatteryPreference(False, 90, 10)
    coord.client.battery_settings_details.side_effect = TimeoutError()
    assert not await runtime.async_refresh_battery_settings(force=True)
    assert runtime.ev_preferences.value is None
    assert runtime.ev_preferences.seen


@pytest.mark.asyncio
async def test_inflight_poll_cannot_overwrite_newer_preference(coordinator_factory):
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    async def stale_read():
        runtime.ev_preferences.generation += 2
        runtime.ev_preferences.observe(payload(True, 95))
        return payload(False, 90)

    coord.client.battery_settings_details = AsyncMock(side_effect=stale_read)
    await runtime.async_refresh_battery_settings(force=True)
    assert runtime.ev_preferences.value == EVBatteryPreference(True, 95, 10)


@pytest.mark.asyncio
async def test_cancelled_write_is_unavailable(preferences):
    preferences.runtime.coordinator.client.set_ev_battery_preference.side_effect = (
        asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await preferences.async_update(enabled=True)
    assert preferences.value is None
    assert preferences.generation % 2 == 0


def test_preference_path_redaction():
    from custom_components.enphase_ev.log_redaction import redact_text
    from .random_ids import RANDOM_SITE_ID

    url = f"/service/batteryConfig/api/v1/device/battery/preference/{RANDOM_SITE_ID}"
    assert str(RANDOM_SITE_ID) not in redact_text(url)


@pytest.mark.asyncio
async def test_number_without_runtime_is_unavailable(coordinator_factory, monkeypatch):
    from custom_components.enphase_ev import number

    coord = coordinator_factory()
    entity = EVBatteryLimitNumber(coord)
    monkeypatch.setattr(number, "preferences_for", lambda coord: None)
    assert not entity.available
    assert entity.native_value is None
    assert entity.native_min_value == 0
    assert entity.device_info is not None
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(90)


@pytest.mark.asyncio
async def test_number_registry_survives_cold_optional_warmup(
    hass, config_entry, coordinator_factory, monkeypatch
):
    from homeassistant.helpers import entity_registry as er
    from custom_components.enphase_ev import number
    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData

    coord = coordinator_factory()
    monkeypatch.setattr(number, "_type_available", lambda *args: True)
    monkeypatch.setattr(number, "_type_device_info", lambda *args: None)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "number",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_ev_battery_limit",
        config_entry=config_entry,
    )
    registry.async_update_entity(old.entity_id, name="Existing EV threshold")
    added = []
    await number.async_setup_entry(
        hass, config_entry, lambda entities, **kwargs: added.extend(entities)
    )
    entity = next(e for e in added if isinstance(e, EVBatteryLimitNumber))
    assert not entity.available
    assert entity.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }
    assert registry.async_get(old.entity_id).name == "Existing EV threshold"


@pytest.mark.asyncio
async def test_enable_and_disable_share_site_state_between_chargers(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord.battery_state._battery_user_is_owner = True
    coord.data["second-charger"] = {"sn": "second-charger"}
    prefs = coord.battery_runtime.ev_preferences
    prefs.observe(payload(True, 95))
    first = GreenBatterySwitch(coord, RANDOM_SERIAL)
    second = GreenBatterySwitch(coord, "second-charger")
    assert first.available
    assert first.is_on and second.is_on
    prefs.observe(payload(False, 95))
    assert not first.is_on and not second.is_on
    assert EVBatteryLimitNumber(coord).available
