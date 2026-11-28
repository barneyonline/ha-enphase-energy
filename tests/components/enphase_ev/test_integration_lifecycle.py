"""Whole integration lifecycle with real platforms and cloud boundary doubles."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from http import HTTPStatus
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientResponseError, RequestInfo
import pytest
from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState, RELOAD_AFTER_UPDATE_DELAY
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import async_fire_time_changed
from yarl import URL

from custom_components.enphase_ev.api import EnphaseEVClient
from custom_components.enphase_ev.api_client import evse_surface
from custom_components.enphase_ev.const import (
    CONF_EAUTH,
    CONF_INCLUDE_INVERTERS,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SERIALS,
    CONF_SITE_ONLY,
    DIRECTIONAL_BATTERY_ENERGY_FIELDS,
    DOMAIN,
    OPT_DIRECTIONAL_BATTERY_ENERGY_ENABLED,
    OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
    OPT_WEATHER_ENABLED,
)
from custom_components.enphase_ev.diagnostics import async_get_config_entry_diagnostics
from custom_components.enphase_ev.directional_battery_energy import (
    directional_battery_store,
)
from .random_ids import RANDOM_SERIAL


@pytest.fixture
def freezer():
    # aiohttp needs a real localtime result with a timezone offset for access logs.
    with freeze_time(
        "2026-09-05 01:00:00", real_asyncio=True, ignore=["aiohttp.web_log"]
    ) as clock:
        yield clock


@pytest.fixture
def platform_cloud(monkeypatch, load_fixture, freezer):
    """Only replace cloud calls; retain coordinators, sessions and HA platforms."""
    monkeypatch.setattr(
        "custom_components.enphase_ev.async_create_clientsession",
        async_create_clientsession,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.async_get_clientsession",
        async_get_clientsession,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.firmware_catalog.FirmwareCatalogManager.async_get_catalog",
        AsyncMock(return_value=None),
    )
    responses = {
        "site_bootstrap": {
            "app": {
                "timezone": "Australia/Melbourne",
                "user": {
                    "id": 1,
                    "isAdmin": False,
                    "isInstaller": True,
                    "isHost": False,
                    "hasConsumptionDataAccess": True,
                },
                "owner": {"id": 1},
            }
        },
        "system_dashboard_summary": {"country_code": "AU", "currency_unit": "AUD"},
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
        "array_builder_inventory": {"arrays": [], "inventory_details": []},
        "array_panel_ratings": {},
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
            "last_report_date": dt_util.utcnow().timestamp(),
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
async def test_directional_battery_real_platform_lifecycle(
    hass, config_entry, platform_cloud, freezer, monkeypatch
):
    responses, _, unexpected = platform_cloud
    responses["devices_inventory"]["result"].append(
        {"type": "encharge", "devices": [{"serial_number": "BAT-TEST"}]}
    )
    responses["battery_site_settings"] = {"data": {"hasEncharge": True}}
    responses["battery_status"] = {
        "storages": [{"serial_number": "BAT-TEST", "current_charge": 70}]
    }
    responses["battery_backup_history"] = {"histories": []}
    responses["site_livestream_payload"] = {
        "meters": {"gridRelay": "OPER_RELAY_CLOSED"}
    }
    responses["lifetime_energy"].update(
        start_date="2026-09-05",
        charge=[12300],
        discharge=[500],
        grid_battery=[12000],
        solar_battery=[300],
        battery_home=[500],
        battery_grid=[0],
    )
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_INCLUDE_INVERTERS: False,
            CONF_SERIALS: [],
            CONF_SITE_ONLY: True,
            CONF_SELECTED_TYPE_KEYS: ["envoy", "encharge"],
        },
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await _finish_startup(hass, config_entry)
    registry = er.async_get(hass)
    coord = config_entry.runtime_data.coordinator
    ids = {
        key: registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_site_{coord.site_id}_{key}"
        )
        for key in DIRECTIONAL_BATTERY_ENERGY_FIELDS
    }
    assert all(ids.values())
    assert (
        len({registry.async_get(entity_id).device_id for entity_id in ids.values()})
        == 1
    )
    grid_id = ids["battery_energy_from_grid"]
    disabled_id = ids["battery_energy_to_grid"]
    device = dr.async_get(hass).async_get(registry.async_get(grid_id).device_id)
    assert (DOMAIN, f"type:{coord.site_id}:encharge") in device.identifiers
    state = hass.states.get(grid_id)
    assert float(state.state) == 12.0
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["state_class"] == "total_increasing"
    assert float(hass.states.get(disabled_id).state) == 0.0

    registry.async_update_entity(disabled_id, disabled_by=er.RegistryEntryDisabler.USER)
    reload = AsyncMock(wraps=hass.config_entries.async_reload)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_DIRECTIONAL_BATTERY_ENERGY_ENABLED: False}
    )
    await _finish_startup(hass, config_entry)
    reload.assert_awaited_once_with(config_entry.entry_id)
    assert (
        registry.async_get(grid_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    )
    assert registry.async_get(disabled_id).disabled_by is er.RegistryEntryDisabler.USER

    lifetime = responses["lifetime_energy"]
    lifetime["battery_grid"] = [None]
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_DIRECTIONAL_BATTERY_ENERGY_ENABLED: True}
    )
    await hass.async_block_till_done()
    reload.assert_awaited_once_with(config_entry.entry_id)
    freezer.tick(timedelta(seconds=RELOAD_AFTER_UPDATE_DELAY + 1))
    lifetime["last_report_date"] = dt_util.utcnow().timestamp()
    async_fire_time_changed(hass, dt_util.utcnow())
    await _finish_startup(hass, config_entry)
    assert reload.await_count == 2
    assert registry.async_get(grid_id).disabled_by is None
    assert registry.async_get(disabled_id).disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(grid_id).state == STATE_UNAVAILABLE
    freezer.tick(timedelta(seconds=RELOAD_AFTER_UPDATE_DELAY + 1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert reload.await_count == 2

    # A normal reload starts a fresh guard. A shrinking snapshot at startup
    # must not turn a later recovery into a new Recorder increment.
    lifetime.update(
        charge=[1300],
        grid_battery=[1000],
        battery_grid=[0],
        last_report_date=dt_util.utcnow().timestamp(),
    )
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await _finish_startup(hass, config_entry)
    assert hass.states.get(grid_id).state == STATE_UNAVAILABLE
    coord = config_entry.runtime_data.coordinator

    async def publish_grid(bucket: int, *, advance: bool) -> str:
        lifetime.update(charge=[300 + bucket], grid_battery=[bucket])
        if advance:
            freezer.tick(timedelta(minutes=5))
            lifetime["last_report_date"] = dt_util.utcnow().timestamp()
        await coord.energy._async_refresh_site_energy(force=True)
        coord.async_set_updated_data(dict(coord.data))
        await hass.async_block_till_done()
        return hass.states.get(grid_id).state

    assert await publish_grid(1000, advance=False) == STATE_UNAVAILABLE
    assert await publish_grid(1000, advance=False) == STATE_UNAVAILABLE
    assert float(await publish_grid(12003, advance=True)) == 12.003
    assert await publish_grid(0, advance=True) == STATE_UNAVAILABLE
    assert await publish_grid(1000, advance=True) == STATE_UNAVAILABLE
    assert await publish_grid(1000, advance=True) == STATE_UNAVAILABLE
    assert "last_reset_at" not in hass.states.get(grid_id).attributes
    lifetime.update(grid_battery=[11_900], solar_battery=[403], charge=[12_303])
    await coord.energy._async_refresh_site_energy(force=True)
    coord.async_set_updated_data(dict(coord.data))
    solar_id = ids["battery_energy_from_solar"]
    assert hass.states.get(grid_id).state == STATE_UNAVAILABLE
    assert hass.states.get(solar_id).state == STATE_UNAVAILABLE
    lifetime["solar_battery"] = [300]

    # Reject a shifted history before the first platform state write on reload.
    lifetime["start_date"] = "2026-09-06"
    lifetime["grid_battery"] = [24_000]
    lifetime["charge"] = [24_300]
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await _finish_startup(hass, config_entry)
    coord = config_entry.runtime_data.coordinator
    assert hass.states.get(grid_id).state == STATE_UNAVAILABLE
    assert await publish_grid(24_000, advance=True) == STATE_UNAVAILABLE
    assert await publish_grid(24_001, advance=True) == STATE_UNAVAILABLE
    lifetime["start_date"] = "2026-09-05"
    assert float(await publish_grid(12_004, advance=True)) == 12.004

    # Core reports storage loss as an empty store. Existing entity IDs cannot
    # safely treat a rerouted historical total as a fresh Recorder baseline.
    store = directional_battery_store(hass, config_entry.entry_id)
    history = await store.async_load()
    assert history
    await store.async_remove()
    lifetime.update(grid_battery=[12_104], solar_battery=[200], charge=[12_304])
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await _finish_startup(hass, config_entry)
    coord = config_entry.runtime_data.coordinator
    assert hass.states.get(grid_id).state == STATE_UNAVAILABLE
    assert hass.states.get(solar_id).state == STATE_UNAVAILABLE
    assert await store.async_load() is None
    await store.async_save(history)
    lifetime["solar_battery"] = [300]
    assert float(await publish_grid(12_005, advance=True)) == 12.005

    assert all(registry.async_get(entity_id) is not None for entity_id in ids.values())
    assert not unexpected, ", ".join(unexpected)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert await directional_battery_store(hass, config_entry.entry_id).async_load()
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    assert (
        await directional_battery_store(hass, config_entry.entry_id).async_load()
        is None
    )


@pytest.mark.session_history_real
async def test_computed_names_of_grouped_devices_and_main_charger(
    hass, config_entry, platform_cloud
):
    """Core composes names from the device and independent entity names."""
    responses, _, unexpected = platform_cloud
    inverter_serials = ("INV-A", "INV-B")
    battery_names = {"BAT-A": "Rack left", "BAT-B": None}
    responses["devices_inventory"]["result"].append(
        {
            "type": "encharge",
            "devices": [{"serial_number": serial} for serial in battery_names],
        }
    )
    responses["battery_site_settings"] = {"data": {"hasEncharge": True}}
    responses["battery_status"] = {
        "storages": [
            {"serial_number": serial, "name": name, "current_charge": 70}
            for serial, name in battery_names.items()
        ]
    }
    responses["inverters_inventory"] = {
        "inverters": [{"serial_number": serial} for serial in inverter_serials],
        "total": len(inverter_serials),
    }
    responses["system_dashboard_master_data"] = {}
    responses["system_dashboard_data_columns"] = {}
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SELECTED_TYPE_KEYS: ["envoy", "encharge", "microinverter", "iqevse"],
        },
        options={
            **config_entry.options,
            OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED: True,
        },
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await _finish_startup(hass, config_entry)
    registry = er.async_get(hass)
    entries = {
        entry.unique_id: entry
        for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id)
    }
    site = config_entry.runtime_data.coordinator.site_id

    batteries = [
        entries[f"enphase_ev_site_{site}_battery_{serial}_charge_level"]
        for serial in battery_names
    ]
    assert len({entry.device_id for entry in batteries}) == 1
    assert [er.async_get_full_entity_name(hass, entry) for entry in batteries] == [
        "IQ Battery Rack left",
        "IQ Battery BAT-B",
    ]
    inverters = [
        entries[f"enphase_ev_inverter_{serial}_lifetime_energy"]
        for serial in inverter_serials
    ]
    assert len({entry.device_id for entry in inverters}) == 1
    assert [er.async_get_full_entity_name(hass, entry) for entry in inverters] == [
        "IQ Microinverters INV-A Lifetime Energy",
        "IQ Microinverters INV-B Lifetime Energy",
    ]

    charger = entries[f"enphase_ev_{RANDOM_SERIAL}_charging_switch"]
    assert charger.original_name is None
    assert er.async_get_full_entity_name(hass, charger) == "Garage EV"
    dr.async_get(hass).async_update_device(
        charger.device_id, name_by_user="Driveway EV"
    )
    assert er.async_get_full_entity_name(hass, charger) == "Driveway EV"
    assert registry.async_get_entity_id("switch", "enphase_ev", charger.unique_id) == (
        charger.entity_id
    )
    assert unexpected == [], ", ".join(unexpected)


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
    assert hass.states.get(power.entity_id).state == "unknown"
    # A production average needs two energy observations. Exercise the actual
    # acquisition and platforms while get_latest_power remains at 1250 W.
    freezer.tick(timedelta(minutes=15))
    responses["lifetime_energy"]["production"] = [1375]
    responses["lifetime_energy"]["last_report_date"] = dt_util.utcnow().timestamp()
    coord = config_entry.runtime_data.coordinator
    await coord.energy._async_refresh_site_energy(force=True)
    coord.async_set_updated_data(dict(coord.data))
    await hass.async_block_till_done()
    assert float(hass.states.get(power.entity_id).state) == 1500
    access = next(e for e in entries if e.unique_id.endswith("_account_access"))
    site_info = next(e for e in entries if e.unique_id.endswith("_site_information"))
    access_state = hass.states.get(access.entity_id)
    assert access_state.state == "installer"
    assert access_state.attributes["administrator"] is False
    assert access_state.attributes["installer"] is True
    assert access_state.attributes["owner"] is True
    info_state = hass.states.get(site_info.entity_id)
    assert info_state.attributes["timezone"] == "Australia/Melbourne"
    assert info_state.attributes["country"] == "AU"
    assert info_state.attributes["currency"] == "AUD"
    assert dr.async_get(hass).async_get(access.device_id).sw_version is None

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
    assert float(hass.states.get(power.entity_id).state) == 1500

    before = calls["lifetime_energy"].await_count
    responses["latest_power"]["value"] = 248
    responses["lifetime_energy"]["production"] = [2000]
    responses["lifetime_energy"]["last_report_date"] += 900
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
    assert calls["lifetime_energy"].await_count > before
    assert float(hass.states.get(power.entity_id).state) == 2500
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


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("change", ["failure", "duplicate"])
async def test_gateway_connectivity_publishes_summary_invalidity_and_recovery(
    hass, config_entry, platform_cloud, monkeypatch, connected, change
):
    """An unchanged charger must publish invalid summary evidence immediately."""
    responses, calls, unexpected = platform_cloud
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_SELECTED_TYPE_KEYS: ["envoy", "iqevse"]},
    )
    responses["summary_v2"] = [
        {
            "serialNumber": RANDOM_SERIAL,
            "isLocallyConnected": connected,
            "lastReportedAt": (dt_util.utcnow() - timedelta(hours=1)).isoformat(),
        }
    ]
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await _finish_startup(hass, config_entry)
    coord = config_entry.runtime_data.coordinator
    # Exercise the normal coordinator refresh/HA equality path. Force only the
    # optional summary acquisition; keep its cache so a failure reuses payload.
    monkeypatch.setattr(coord.summary, "prepare_refresh", lambda **_: True)
    await coord.async_refresh()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    sensor_id = registry.async_get_entity_id(
        "binary_sensor", "enphase_ev", f"enphase_ev_{RANDOM_SERIAL}_gateway_connected"
    )
    assert sensor_id is not None
    assert coord.always_update is False
    expected_state = "on" if connected else "off"
    assert hass.states.get(sensor_id).state == expected_state
    previous_data = deepcopy(dict(coord.data))
    publications = []
    summary_response = calls["summary_v2"].side_effect
    unsubscribe = coord.async_add_listener(lambda: publications.append(True))
    try:
        if change == "failure":
            calls["summary_v2"].side_effect = RuntimeError("Summary unavailable")
        else:
            responses["summary_v2"] *= 2
        await coord.async_refresh()
        await hass.async_block_till_done()
        assert coord.last_update_success
        assert dict(coord.data) == previous_data
        assert coord.summary.diagnostics()["using_stale"] is (change == "failure")
        assert hass.states.get(sensor_id).state == "unknown"
        assert len(publications) == 1

        # Retry/backoff counters alone must not generate duplicate publications.
        await coord.async_refresh()
        await hass.async_block_till_done()
        assert len(publications) == 1

        # Simulate expiry of the existing retry deadline without changing either
        # the cloud payload or the sensor's source timestamps.
        monkeypatch.setitem(coord.summary._state, "backoff_until", None)
        calls["summary_v2"].side_effect = summary_response
        responses["summary_v2"] = responses["summary_v2"][:1]
        await coord.async_refresh()
        await hass.async_block_till_done()
        assert dict(coord.data) == previous_data
        assert hass.states.get(sensor_id).state == expected_state
        assert len(publications) == 2
        await coord.async_refresh()
        await hass.async_block_till_done()
        assert len(publications) == 2

        received = hass.states.get(sensor_id).attributes["telemetry_received_at"]
        next_receipt = received + timedelta(minutes=1)
        monkeypatch.setattr(
            "custom_components.enphase_ev.summary.dt_util",
            SimpleNamespace(utcnow=lambda: next_receipt),
        )
        await coord.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(sensor_id).state == expected_state
        assert (
            hass.states.get(sensor_id).attributes["telemetry_received_at"]
            == next_receipt
        )
        assert len(publications) == 3

        # Cache hits keep the actual receipt timestamp and cause no publication.
        monkeypatch.setattr(coord.summary, "prepare_refresh", lambda **_: False)
        summary_calls = calls["summary_v2"].await_count
        await coord.async_refresh()
        await hass.async_block_till_done()
        assert calls["summary_v2"].await_count == summary_calls
        assert (
            hass.states.get(sensor_id).attributes["telemetry_received_at"]
            == next_receipt
        )
        assert len(publications) == 3
        assert unexpected == []
    finally:
        unsubscribe()
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


async def test_cold_setup_retries_after_enphase_rate_limit(
    hass, config_entry, platform_cloud, freezer
):
    """Let Core schedule a second setup at the first refresh's retry deadline."""
    _, calls, unexpected = platform_cloud
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_SELECTED_TYPE_KEYS: ["iqevse"]}
    )
    status = calls["status"]
    healthy_status = status.side_effect
    status_url = URL("https://enphase.example.test/evse/status")
    status.side_effect = ClientResponseError(
        RequestInfo(status_url, "GET", {}, status_url),
        (),
        status=429,
        headers={"Retry-After": "1200"},
    )

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert getattr(config_entry, "runtime_data", None) is None
    status.assert_awaited_once()

    status.side_effect = healthy_status
    freezer.tick(timedelta(seconds=1199))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    status.assert_awaited_once()

    loaded = asyncio.Event()

    @callback
    def signal_loaded():
        if config_entry.state is ConfigEntryState.LOADED:
            loaded.set()

    unsubscribe = config_entry.async_on_state_change(signal_loaded)
    try:
        freezer.tick(timedelta(seconds=2))
        async_fire_time_changed(hass, dt_util.utcnow())
        await asyncio.wait_for(loaded.wait(), timeout=5)
    finally:
        unsubscribe()

    assert config_entry.state is ConfigEntryState.LOADED
    await _finish_startup(hass, config_entry)
    assert status.await_count == 2
    assert unexpected == []
    assert await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    ("enabled_mode", "sensor_state", "select_state", "source"),
    [
        ("scheduledCharging", "SCHEDULED_CHARGING", "Scheduled", "scheduler_endpoint"),
        ("manualCharging", "MANUAL_CHARGING", "Manual", "scheduler_endpoint"),
        ("smartCharging", "SMART_CHARGING", "Smart", "scheduler_endpoint"),
        ("greenCharging", "GREEN_CHARGING", "Green", "scheduler_endpoint"),
        (None, "GREEN_CHARGING", "unknown", "schedule_type_fallback"),
    ],
)
async def test_charge_mode_preference_refresh_reaches_both_entities(
    hass,
    config_entry,
    platform_cloud,
    freezer,
    monkeypatch,
    enabled_mode,
    sensor_state,
    select_state,
    source,
):
    """Recheck scheduler preference when status only describes the policy window."""
    responses, calls, unexpected = platform_cloud
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_SELECTED_TYPE_KEYS: ["envoy", "iqevse"]},
    )
    charger = responses["status"]["evChargerData"][0]
    charger["mode"] = 1
    responses["get_schedules"]["slots"] = [
        {
            "id": "slot-1",
            "startTime": "08:00:00",
            "endTime": "09:30:00",
            "days": [1, 3, 5],
            "scheduleType": "CUSTOM",
            "enabled": True,
            "chargingLevelAmp": 24,
        }
    ]
    modes = {
        "smartCharging": {"enabled": True, "chargingMode": "SMART_CHARGING"},
        "scheduledCharging": {"enabled": False, "chargingMode": "SCHEDULED_CHARGING"},
        "manualCharging": {"enabled": False, "chargingMode": "MANUAL_CHARGING"},
    }

    async def scheduler_response(method, url, **_kwargs):
        assert method == "GET"
        assert url.endswith(f"/{RANDOM_SERIAL}/preference")
        return {"data": {"modes": deepcopy(modes)}}

    preference_requests = AsyncMock(side_effect=scheduler_response)
    monkeypatch.setattr(EnphaseEVClient, "charge_mode", evse_surface.charge_mode)
    monkeypatch.setattr(EnphaseEVClient, "_json", preference_requests)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    try:
        await _finish_startup(hass, config_entry)
        coordinator = config_entry.runtime_data.coordinator
        assert coordinator.schedule_sync.get_slot(RANDOM_SERIAL, "slot-1")["enabled"]
        registry = er.async_get(hass)
        sensor_id = registry.async_get_entity_id(
            "sensor", "enphase_ev", f"enphase_ev_{RANDOM_SERIAL}_charge_mode"
        )
        select_id = registry.async_get_entity_id(
            "select", "enphase_ev", f"enphase_ev_{RANDOM_SERIAL}_charge_mode_select"
        )
        assert sensor_id is not None
        assert select_id is not None
        assert hass.states.get(sensor_id).state == "SMART_CHARGING"
        assert hass.states.get(select_id).state == "Smart"

        # This status window is independent of which scheduler preference is enabled.
        charger["sch_d"] = {"status": 1, "info": [{"type": "greencharging"}]}
        if enabled_mode == "greenCharging":
            modes["greenCharging"] = {"chargingMode": "GREEN_CHARGING"}
        for key, mode in modes.items():
            mode["enabled"] = key == enabled_mode
        previous_reads = preference_requests.await_count
        previous_status_reads = calls["status"].await_count
        freezer.tick(timedelta(minutes=6))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert calls["status"].await_count == previous_status_reads + 1
        snapshot = coordinator.data[RANDOM_SERIAL]
        assert snapshot["charge_mode_source"] == "explicit_status"
        assert (
            hass.states.get(sensor_id).state,
            hass.states.get(select_id).state,
            snapshot["charge_mode_pref_source"],
        ) == (sensor_state, select_state, source)
        assert preference_requests.await_count == previous_reads + 1
        calls["set_charge_mode"].assert_not_awaited()
        calls["patch_schedules"].assert_not_awaited()
        assert unexpected == []
    finally:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.session_history_real
@pytest.mark.parametrize(
    "outcome",
    [
        "recovered",
        "pending",
        "rate_limited",
        "status_failed",
        "different_live",
        "stale",
        "empty_storage",
        "queued_work",
        "cooldown",
    ],
)
async def test_storm_guard_stuck_profile_warning_through_repairs(
    hass,
    config_entry,
    platform_cloud,
    freezer,
    hass_client,
    hass_ws_client,
    hass_owner_user,
    outcome,
):
    """Expose the warning and handle Check again through Home Assistant Repairs."""
    responses, calls, unexpected = platform_cloud
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SELECTED_TYPE_KEYS: ["envoy", "encharge", "iqevse"],
        },
    )
    responses["devices_inventory"]["result"].append(
        {"type": "encharge", "devices": [{"serial_number": "BAT-A"}]}
    )
    responses["battery_site_settings"] = {
        "data": {"hasEncharge": True, "userDetails": {"isOwner": True}}
    }
    responses["battery_status"] = {
        "storages": [
            {
                "serial_number": "BAT-A",
                "current_charge": 90,
                "battery_mode": "Self-Consumption",
                "last_report": dt_util.utcnow().timestamp(),
            }
        ]
    }
    responses["storm_guard_profile"] = {
        "data": {
            "profile": "backup_only",
            "batteryBackupPercentage": 100,
            "siteProfileStatus": "PROFILE_SWITCH_PENDING",
            "systemTask": True,
            "requestedConfig": {},
            "requestedConfigMqtt": {},
        }
    }
    advisory_id = "test-private-advisory-id"
    responses["storm_guard_alert"] = {
        "stormAlerts": [
            {
                "id": advisory_id,
                "status": "opted-out",
                "endTime": (dt_util.utcnow() + timedelta(hours=2)).timestamp() * 1000,
            }
        ],
        "criticalAlertActive": False,
    }

    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "repairs", {})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await _finish_startup(hass, config_entry)
    coord = config_entry.runtime_data.coordinator
    issue_id = f"storm_guard_stuck_profile_{config_entry.entry_id}"
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    for _ in range(7):
        freezer.tick(timedelta(minutes=5, seconds=1))
        responses["battery_status"]["storages"][0][
            "last_report"
        ] = dt_util.utcnow().timestamp()
        await coord.async_refresh()
        await hass.async_block_till_done()

    entities = er.async_get(hass)
    profile_id = entities.async_get_entity_id(
        "select", DOMAIN, f"{DOMAIN}_site_{coord.site_id}_system_profile"
    )
    reserve_id = entities.async_get_entity_id(
        "number", DOMAIN, f"{DOMAIN}_site_{coord.site_id}_battery_reserve"
    )
    assert hass.states.get(profile_id).state == STATE_UNAVAILABLE
    assert hass.states.get(reserve_id).state == STATE_UNAVAILABLE

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    metrics = diagnostics["coordinator"]["site_metrics"]
    assert metrics["storm_guard_stuck_profile"] == {
        "profile_matches": True,
        "live_self_consumption": True,
        "fresh_opt_out": True,
        "repair_reported": True,
    }
    assert diagnostics["entry_data"][CONF_EAUTH] == "**REDACTED**"
    assert advisory_id not in repr(diagnostics)

    refresh_token = await hass.auth.async_create_refresh_token(
        hass_owner_user, client_id="http://localhost"
    )
    access_token = hass.auth.async_create_access_token(refresh_token)
    client = await hass_ws_client(hass, access_token)
    try:
        http_client = await hass_client(access_token)
        await client.send_json({"id": 1, "type": "repairs/list_issues"})
        result = await client.receive_json()
        assert result["success"]
        issues = result["result"]["issues"]
        matching = [issue for issue in issues if issue["issue_id"] == issue_id]
        assert len(matching) == 1
        (issue,) = matching
        assert issue["severity"] == "warning"
        assert issue["is_fixable"] is True
        assert issue["translation_key"] == "storm_guard_stuck_profile"
        assert issue["translation_placeholders"] == {"site_id": coord.site_id}
        assert registry.async_get_issue(DOMAIN, issue_id).config_entry_id == (
            config_entry.entry_id
        )
        assert not any(
            item["translation_key"] == "battery_profile_mismatch" for item in issues
        )

        fix_url = "/api/repairs/issues/fix"
        response = await http_client.post(
            fix_url, json={"handler": DOMAIN, "issue_id": issue_id}
        )
        assert response.status == HTTPStatus.OK
        flow = await response.json()
        assert flow["type"] == FlowResultType.MENU
        assert flow["step_id"] == "init"
        assert flow["menu_options"] == ["check_again"]
        assert flow["description_placeholders"] == {"site_id": coord.site_id}
        check_url = f"{fix_url}/{flow['flow_id']}"

        profile_read = calls["storm_guard_profile"]
        live_read = calls["battery_status"]
        profile_read.reset_mock()
        live_read.reset_mock()
        calls["storm_guard_alert"].reset_mock()

        rate_limit = ClientResponseError(
            RequestInfo(
                URL("https://example.com"), "GET", {}, URL("https://example.com")
            ),
            (),
            status=429,
            headers={"Retry-After": "180"},
        )
        if outcome in {"recovered", "status_failed", "different_live"}:
            responses["storm_guard_profile"] = {
                "data": {
                    "profile": "self-consumption",
                    "batteryBackupPercentage": 20,
                    "siteProfileStatus": "READY",
                    "systemTask": False,
                }
            }
        if outcome == "rate_limited":
            profile_read.side_effect = rate_limit
        elif outcome == "status_failed":
            live_read.side_effect = TimeoutError
        elif outcome == "different_live":
            responses["battery_status"]["storages"][0]["battery_mode"] = "Full Backup"
        elif outcome == "stale":
            responses["battery_status"]["storages"][0]["last_report"] = (
                dt_util.utcnow() - timedelta(minutes=11)
            ).timestamp()
        elif outcome == "empty_storage":
            responses["battery_status"] = {"storages": []}
        elif outcome == "queued_work":
            responses["storm_guard_profile"]["data"]["requestedConfigMqtt"] = {
                "pendingGateways": ["test-gateway"]
            }
        elif outcome == "cooldown":
            coord.note_endpoint_family_failure("storm_guard", rate_limit)

        response = await http_client.post(
            check_url, json={"next_step_id": "check_again"}
        )
        assert response.status == HTTPStatus.OK
        flow = await response.json()
        await hass.async_block_till_done()
        if outcome == "recovered":
            assert flow["type"] == FlowResultType.CREATE_ENTRY
            assert hass.states.get(profile_id).state == "Self-Consumption"
            assert float(hass.states.get(reserve_id).state) == 20
        else:
            assert flow["type"] == FlowResultType.MENU
            assert flow["step_id"] == (
                "pending" if outcome == "pending" else "check_failed"
            )
            assert flow["menu_options"] == ["check_again"]
            assert coord.battery_system_task is True
            assert hass.states.get(profile_id).state == STATE_UNAVAILABLE
            assert hass.states.get(reserve_id).state == STATE_UNAVAILABLE
            response = await http_client.get(check_url)
            assert response.status == HTTPStatus.OK
            assert await response.json() == flow
        assert profile_read.await_count == (0 if outcome == "cooldown" else 1)
        assert live_read.await_count == (
            0 if outcome in {"cooldown", "rate_limited"} else 1
        )
        calls["storm_guard_alert"].assert_not_awaited()

        if outcome == "rate_limited":
            profile_read.reset_mock()
            response = await http_client.post(
                check_url, json={"next_step_id": "check_again"}
            )
            assert response.status == HTTPStatus.OK
            flow = await response.json()
            assert flow["step_id"] == "check_failed"
            profile_read.assert_not_awaited()

        if outcome == "status_failed":
            coord.sync_battery_profile_pending_issue()
        await client.send_json({"id": 2, "type": "repairs/list_issues"})
        result = await client.receive_json()
        assert result["success"]
        assert any(
            item["issue_id"] == issue_id for item in result["result"]["issues"]
        ) is (outcome != "recovered")
        calls["set_battery_profile"].assert_not_awaited()
        calls["set_battery_settings"].assert_not_awaited()
        assert unexpected == [], ", ".join(unexpected)
    finally:
        await client.close()
        await hass.config_entries.async_unload(config_entry.entry_id)
