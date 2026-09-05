"""Whole integration lifecycle with real platforms and cloud boundary doubles."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.enphase_ev.api import EnphaseEVClient
from custom_components.enphase_ev.const import (
    CONF_SELECTED_TYPE_KEYS,
    CONF_SERIALS,
    CONF_SITE_ONLY,
    OPT_WEATHER_ENABLED,
)
from .random_ids import RANDOM_SERIAL


@pytest.fixture
def freezer():
    with freeze_time("2026-09-05 01:00:00", real_asyncio=True) as clock:
        yield clock


@pytest.fixture
def platform_cloud(monkeypatch, load_fixture):
    """Only replace cloud calls; retain coordinators, sessions and HA platforms."""
    monkeypatch.setattr(
        "custom_components.enphase_ev.async_create_clientsession",
        async_create_clientsession,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.async_get_clientsession",
        async_get_clientsession,
    )
    responses = {
        "status": load_fixture("status_idle.json"),
        "summary_v2": [],
        "site_tariff_bundle": ({}, {}),
        "charger_auth_settings": [],
        "devices_inventory": {
            "result": [
                {
                    "type": "envoy",
                    "devices": [{"serial_number": "GW-TEST", "name": "IQ Gateway"}],
                }
            ]
        },
        "inverters_inventory": {"inverters": [], "total": 0},
        "system_dashboard_envoy_inverters": {"inverters": []},
        "inverter_status": {},
        "charge_mode": "MANUAL_CHARGING",
        "evse_timeseries_lifetime_energy": {},
        "evse_timeseries_daily_energy": {},
        "site_livestream_payload": {},
        "battery_site_settings": {"data": {"hasEncharge": False, "hasEnpower": False}},
        "battery_backup_history": {"data": []},
        "battery_settings_details": {"data": {}},
        "battery_schedules": {"data": []},
        "battery_status": {"data": {}},
        "storm_guard_profile": {"data": {}},
        "storm_guard_alert": {"criticalAlertActive": False, "stormAlerts": []},
        "off_grid_due_to_grid_outage": {},
        "green_charging_settings": [],
        "charger_config": [],
        "evse_feature_flags": {},
        "evse_fw_details": [],
        "homeowner_events_page": {"events": []},
        "system_dashboard_events": {"events": []},
        "system_dashboard_standing_alarms": {"alarms": []},
        "phase_map_multiple_envoy": {},
        "hems_consumption_lifetime": {},
        "dry_contacts_settings": {},
        "session_history": {"data": []},
        "session_history_filter_criteria": {},
        "weather": {"code": "sunny", "temperature": {"value": 20, "display": "20°C"}},
        "latest_power": {"value": 1250, "units": "W", "precision": 0},
        "lifetime_energy": {
            "production": [1000],
            "consumption": [2000],
            "interval": 15,
        },
        "get_schedules": {"data": [], "meta": {"serverTimeStamp": 1}},
    }
    calls = {}
    unexpected = []
    for name, method in inspect.getmembers(
        EnphaseEVClient, inspect.iscoroutinefunction
    ):
        if name.startswith("_") or name == "async_close":
            continue

        async def response(*_args, _name=name, **_kwargs):
            if _name not in responses:
                unexpected.append(_name)
                raise AssertionError(f"Unexpected cloud call: {_name}")
            return deepcopy(responses[_name])

        calls[name] = AsyncMock(side_effect=response)
        monkeypatch.setattr(EnphaseEVClient, name, calls[name])
    return responses, calls, unexpected


async def _finish_startup(hass, entry):
    await hass.async_block_till_done()
    warmup = entry.runtime_data.coordinator._warmup_task
    assert warmup is not None
    await asyncio.wait_for(asyncio.shield(warmup), 5)
    await hass.async_block_till_done()


@pytest.mark.session_history_real
@pytest.mark.parametrize("site_only", [False, True])
async def test_real_platform_setup_reload_and_unload(
    hass, config_entry, platform_cloud, site_only, freezer, monkeypatch
):
    responses, calls, unexpected = platform_cloud
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SERIALS: [] if site_only else [RANDOM_SERIAL],
            CONF_SITE_ONLY: site_only,
            CONF_SELECTED_TYPE_KEYS: ["envoy"] if site_only else ["envoy", "iqevse"],
        },
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await _finish_startup(hass, config_entry)
    assert unexpected == [], ", ".join(unexpected)
    assert config_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    power = next(
        e for e in entries if e.unique_id.endswith("_current_production_power")
    )
    assert hass.states.get(power.entity_id).state == "1250"
    assert bool([e for e in entries if RANDOM_SERIAL in e.unique_id]) is not site_only
    if not site_only:
        assert hass.states.get("binary_sensor.garage_ev_charging").state == "off"
        calls["get_schedules"].assert_awaited()
    else:
        calls["status"].assert_not_awaited()

    registry.async_update_entity(power.entity_id, name="Roof production")
    identities = {e.unique_id: (e.entity_id, e.device_id) for e in entries}
    old = config_entry.runtime_data
    old_session = old.coordinator.client._cookie_header_session
    # Exercise the real topology option listener and its snapshot handoff.
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, OPT_WEATHER_ENABLED: True}
    )
    await hass.async_block_till_done()
    await _finish_startup(hass, config_entry)
    current = config_entry.runtime_data
    assert current.coordinator is not old.coordinator
    assert old_session.closed
    assert not current.coordinator.client._cookie_header_session.closed
    assert registry.async_get(power.entity_id).name == "Roof production"
    restored = {
        e.unique_id: (e.entity_id, e.device_id)
        for e in er.async_entries_for_config_entry(registry, config_entry.entry_id)
    }
    # Confirmed battery absence removes provisional battery controls. All
    # retained entities keep their entity and device registry identities.
    site = current.coordinator.site_id
    unsupported = {
        f"enphase_ev_site_{site}_storm_guard",
        f"enphase_ev_site_{site}_system_profile",
    }
    assert identities.keys() - restored.keys() == unsupported
    assert all(
        restored[key] == identities[key] for key in identities.keys() - unsupported
    )
    assert hass.states.get(power.entity_id).state == "1250"

    before = calls["latest_power"].await_count
    responses["latest_power"]["value"] = 2500
    responses["status"]["evChargerData"][0]["charging"] = True
    responses["status"]["evChargerData"][0]["connectorStatusType"] = "CHARGING"
    power_changed = asyncio.Event()

    @callback
    def on_state(event):
        state = event.data.get("new_state")
        if (
            state is not None
            and state.entity_id == power.entity_id
            and state.state == "2500"
        ):
            power_changed.set()

    unsubscribe = hass.bus.async_listen("state_changed", on_state)
    try:
        freezer.tick(timedelta(minutes=16))
        async_fire_time_changed(hass, dt_util.utcnow())
        await asyncio.wait_for(power_changed.wait(), 5)
        await hass.async_block_till_done()
    finally:
        unsubscribe()
    assert unexpected == [], ", ".join(unexpected)
    assert calls["latest_power"].await_count > before
    assert hass.states.get(power.entity_id).state == "2500"
    if not site_only:
        assert hass.states.get("binary_sensor.garage_ev_charging").state == "on"

    restart = None
    if not site_only:
        responses.update(
            {
                "stop_charging": {"status": "ok"},
                "start_charging": {"status": "ok"},
                "start_live_stream": {"status": "ok", "duration_s": 90},
                "stop_live_stream": {"status": "ok"},
            }
        )
        sleeping = asyncio.Event()

        async def restart_delay(_seconds):
            sleeping.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            "custom_components.enphase_ev.evse_runtime.asyncio",
            SimpleNamespace(**(vars(asyncio) | {"sleep": restart_delay})),
        )
        current.coordinator.schedule_amp_restart(RANDOM_SERIAL, delay=30)
        restart = current.coordinator._amp_restart_tasks[RANDOM_SERIAL]
        await asyncio.wait_for(sleeping.wait(), 5)
        calls["stop_charging"].assert_awaited_once_with(RANDOM_SERIAL)
        calls["start_charging"].assert_not_awaited()

    session = current.coordinator.client._cookie_header_session
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert session.closed
    if restart is not None:
        assert restart.cancelled()
        assert current.coordinator._amp_restart_tasks == {}
        calls["start_charging"].assert_not_awaited()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert getattr(config_entry, "runtime_data", None) is None
    # HA retains a registry placeholder for unloaded entities.
    assert hass.states.get(power.entity_id).state == "unavailable"
    assert registry.async_get(power.entity_id).name == "Roof production"
    call_counts = {name: call.await_count for name, call in calls.items()}
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert {name: call.await_count for name, call in calls.items()} == call_counts
    assert unexpected == [], ", ".join(unexpected)
