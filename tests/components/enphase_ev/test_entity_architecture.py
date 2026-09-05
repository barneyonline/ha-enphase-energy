"""Regression coverage for the architectural entity/state corrections."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
import asyncio

import pytest
from homeassistant.core import State
from homeassistant.components.sensor import SensorStateClass
from homeassistant.components.sensor import recorder as sensor_recorder
from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor
from custom_components.enphase_ev.sensor_heatpump import (
    EnphaseHeatPumpDailyEnergySensor,
    EnphaseHeatPumpDailyGridEnergySensor,
    EnphaseHeatPumpDailySolarEnergySensor,
    EnphaseHeatPumpDailyBatteryEnergySensor,
)
from custom_components.enphase_ev.inverter_inventory import (
    async_fetch_inverter_pages,
    inverter_page,
)
from custom_components.enphase_ev.sensor_snapshot_helpers import restore_power_w


@pytest.mark.parametrize(
    "sensor_class,key",
    [
        (EnphaseHeatPumpDailyEnergySensor, "daily_energy_wh"),
        (EnphaseHeatPumpDailyGridEnergySensor, "daily_grid_wh"),
        (EnphaseHeatPumpDailySolarEnergySensor, "daily_solar_wh"),
        (EnphaseHeatPumpDailyBatteryEnergySensor, "daily_battery_wh"),
    ],
)
def test_daily_energy_recorder_day_boundary(
    hass, coordinator_factory, monkeypatch, sensor_class, key
):
    """Exercise HA's real sum compiler with source-day resets and corrections."""
    coord = coordinator_factory(serials=[])
    entity = sensor_class(coord)
    history = []
    start = datetime(2026, 10, 3, 13, 55, tzinfo=timezone.utc)
    for index, (day, value) in enumerate(
        [
            ("2026-10-03", 1000),
            ("2026-10-03", 2000),
            ("2026-10-03", 1500),
            ("2026-10-04", 100),
            ("2026-10-04", 300),
        ]
    ):
        coord._heatpump_daily_consumption = {
            key: value,
            "day_key": day,
            "timezone": "Australia/Melbourne",
            "details": [1, 2],
        }
        attrs = {
            "state_class": entity.state_class,
            "device_class": "energy",
            "unit_of_measurement": "kWh",
            "last_reset": entity.last_reset.isoformat(),
        }
        history.append(
            State(
                "sensor.daily",
                str(entity.native_value),
                attrs,
                last_changed=start + timedelta(seconds=index),
            )
        )
    assert history[0].attributes["last_reset"].startswith("2026-10-03T00:00:00+10:00")
    assert history[-1].attributes["last_reset"].startswith("2026-10-04T00:00:00+10:00")
    monkeypatch.setattr(sensor_recorder, "_get_sensor_states", lambda _: [history[-1]])
    monkeypatch.setattr(sensor_recorder, "get_instance", lambda _: MagicMock())
    monkeypatch.setattr(
        sensor_recorder.history,
        "get_full_significant_states_with_session",
        lambda *a, **k: {"sensor.daily": history},
    )
    monkeypatch.setattr(
        sensor_recorder.statistics, "get_metadata_with_session", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        sensor_recorder.statistics,
        "get_latest_short_term_statistics_with_session",
        lambda *a, **k: {},
    )
    result = sensor_recorder.compile_statistics(
        hass, MagicMock(), start, start + timedelta(minutes=5), {}
    )
    assert result.platform_stats[0]["stat"]["sum"] == pytest.approx(0.8)
    assert entity.state_class == SensorStateClass.TOTAL
    assert "details" in entity.extra_state_attributes
    assert "details" in entity._unrecorded_attributes
    coord._heatpump_daily_consumption = {
        key: 300,
        "day_key": "2026-10-05",
        "timezone": "Australia/Melbourne",
    }
    assert entity.last_reset.utcoffset() == timedelta(hours=11)


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"day_key": "bad", "timezone": "UTC"},
        {"day_key": "2026-01-01", "timezone": "not/a-zone"},
        {"day_key": 1, "timezone": "UTC"},
    ],
)
def test_daily_reset_invalid_source_metadata(coordinator_factory, snapshot):
    coord = coordinator_factory(serials=[])
    coord._heatpump_daily_consumption = {"daily_energy_wh": 1000, **snapshot}
    entity = EnphaseHeatPumpDailyEnergySensor(coord)
    assert entity.last_reset is None
    assert not entity.available


@pytest.mark.parametrize(
    "value", [float("inf"), float("nan"), float("-inf"), "inf", "nan", -1]
)
def test_inverter_nonfinite_samples_recover(coordinator_factory, value):
    coord = coordinator_factory()
    coord._inverter_data = {"INV-A": {"lifetime_production_wh": 1000}}
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    assert entity.native_value == 1
    coord._inverter_data = {"INV-A": {"lifetime_production_wh": value}}
    assert entity.native_value == 1
    entity._last_good_native_value = float("inf")
    assert entity.native_value is None
    coord._inverter_data = {"INV-A": {"lifetime_production_wh": 2000}}
    assert entity.native_value == 2


def test_inverter_snapshot_uses_retained_sources(coordinator_factory, monkeypatch):
    from custom_components.enphase_ev import sensor

    coord = coordinator_factory()
    coord._inverter_data = {}
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    monkeypatch.setattr(sensor, "id", lambda _: 1, raising=False)
    assert entity.native_value is None
    coord._inverter_data = {"INV-A": {"lifetime_production_wh": 1000}}
    coord.data = dict(coord.data)
    assert entity.native_value == 1
    sources = entity._snapshot_cache_sources
    assert sources[0] is coord.data and sources[1] is coord._inverter_data


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"result": []},
        {"inverters": "bad"},
        {"inverters": [None]},
        {"inverters": [], "total": "bad"},
        {"inverters": [], "total": -1},
        {"inverters": [{}], "total": 0},
    ],
)
def test_inverter_page_rejects_ambiguous_payloads(payload):
    assert inverter_page(payload) is None


@pytest.mark.asyncio
async def test_inverter_pages_wrapped_and_complete():
    fetch = AsyncMock(
        side_effect=[
            {"result": {"inverters": [{"serial_number": "A"}], "total": 2}},
            {"inverters": [{"serial_number": "B"}], "total": 2},
        ]
    )
    result = await async_fetch_inverter_pages(fetch)
    assert result.complete and result.reason is None
    assert [row["serial_number"] for row in result.payload["inverters"]] == ["A", "B"]
    assert [call.args[0] for call in fetch.await_args_list] == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pages,kwargs,reason",
    [
        ([None], {}, "invalid_page"),
        ([{"inverters": [{"id": "A"}], "total": 2}] * 2, {}, "no_progress"),
        ([{"inverters": [{}], "total": 200}], {"max_items": 5}, "item_limit"),
        ([{"inverters": [{"id": "A"}], "total": 2}], {"max_pages": 1}, "page_limit"),
        ([{"inverters": [], "total": 1}], {}, "no_progress"),
    ],
)
async def test_inverter_pagination_bounds(pages, kwargs, reason):
    result = await async_fetch_inverter_pages(AsyncMock(side_effect=pages), **kwargs)
    assert not result.complete and result.reason == reason


@pytest.mark.asyncio
async def test_inverter_pagination_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await async_fetch_inverter_pages(AsyncMock(side_effect=asyncio.CancelledError))


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (1.25, "kW", 1250),
        (1250, "W", 1250),
        ("nan", "W", None),
        (1, "unknown", None),
        ("bad", "W", None),
    ],
)
def test_restored_display_power_converts_to_native_watts(value, unit, expected):
    assert (
        restore_power_w(
            State("sensor.power", str(value), {"unit_of_measurement": unit})
        )
        == expected
    )


def test_instantaneous_outage_freshness_preserves_totals(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor_base import EnphaseSiteSensorEntity
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.util import dt as dt_util

    coord = coordinator_factory()
    now = dt_util.utcnow()
    coord.last_update_success = False
    coord.last_success_utc = now - timedelta(minutes=16)
    entity = EnphaseSiteSensorEntity(coord, "test", "Test", type_key=None)
    entity._attr_device_class = SensorDeviceClass.POWER
    assert not entity.available
    coord.last_success_utc = now - timedelta(minutes=1)
    assert entity.available
    coord.last_success_utc = now - timedelta(days=1)
    entity._attr_device_class = SensorDeviceClass.ENERGY
    assert entity.available


@pytest.mark.asyncio
async def test_inverter_runtime_rejects_poisoned_energy_and_recovers(
    coordinator_factory,
):
    """The cloud normalization boundary and entity both recover from bad values."""
    coord = coordinator_factory()
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}
    coord.client.inverters_inventory = AsyncMock(
        return_value={"inverters": [{"serial_number": "INV-A"}], "total": 1}
    )
    coord.client.inverter_status = AsyncMock(
        return_value={"1": {"serialNum": "INV-A", "deviceId": 1}}
    )
    coord._inverter_data = {
        "INV-A": {
            "serial_number": "INV-A",
            "inverter_id": "1",
            "lifetime_production_wh": float("inf"),
        }
    }
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    for sample, expected in [
        ("nan", None),
        ("inf", None),
        ("invalid", None),
        (2000, 2.0),
    ]:
        coord.client.inverter_production = AsyncMock(
            return_value={"production": {"1": sample}}
        )
        coord._inverter_production_cache_until = None
        health = coord._endpoint_family_state("inverter_production")
        health.next_retry_mono = None
        health.next_retry_utc = None
        health.cooldown_active = False
        await coord.inventory_runtime._async_refresh_inverters()
        assert entity.native_value == expected


@pytest.mark.asyncio
async def test_weather_expected_failure_is_sanitized(hass):
    from custom_components.enphase_ev.weather import (
        EnphaseWeatherCoordinator,
        WeatherEndpointUnsupported,
    )
    from homeassistant.helpers.update_coordinator import UpdateFailed
    import aiohttp
    from yarl import URL

    site = "7654321"
    request_info = SimpleNamespace(
        real_url=URL(
            f"https://enlighten.enphaseenergy.com/service/sites/{site}/weather"
        )
    )
    client = SimpleNamespace(
        weather=AsyncMock(
            side_effect=aiohttp.ClientResponseError(
                request_info, (), status=503, message="unavailable"
            )
        )
    )
    coord = EnphaseWeatherCoordinator(hass, client, locale="en", site_id=site)
    with pytest.raises(UpdateFailed) as exc:
        await coord._async_update_data()
    assert site not in str(exc.value)
    client.weather.side_effect = aiohttp.ClientResponseError(
        request_info, (), status=404
    )
    with pytest.raises(WeatherEndpointUnsupported):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_live_sensor_expires_without_another_poll(
    hass, coordinator_factory, monkeypatch
):
    """A suppressed repeat failure still publishes unavailable at its deadline."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    from custom_components.enphase_ev.sensor_base import EnphaseSiteSensorEntity
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.util import dt as dt_util

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_update_success = False
    coord.last_success_utc = now[0]
    entity = EnphaseSiteSensorEntity(coord, "expiry", "Expiry", type_key=None)
    entity.hass = hass
    entity.entity_id = "sensor.expiry"
    entity._attr_device_class = SensorDeviceClass.POWER
    entity._attr_native_unit_of_measurement = "W"
    entity._attr_native_value = 1250
    await entity.async_added_to_hass()
    entity.async_write_ha_state()
    assert hass.states.get(entity.entity_id).state == "1250"
    now[0] += timedelta(minutes=16)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unavailable"
    coord.last_update_success = True
    coord.last_success_utc = now[0]
    entity._handle_coordinator_update()
    assert hass.states.get(entity.entity_id).state == "1250"
    await entity.async_will_remove_from_hass()
    assert entity._cancel_freshness_expiry is None


def test_optional_battery_source_freshness_ignores_healthy_core(coordinator_factory):
    from custom_components.enphase_ev.sensor_base import EnphaseSiteSensorEntity
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.util import dt as dt_util

    coord = coordinator_factory()
    now = dt_util.utcnow()
    coord.last_update_success = True
    coord.last_success_utc = now
    assert coord.endpoint_family_last_success_utc("not_a_family") is None
    health = coord._endpoint_family_state("battery_status")
    health.last_success_utc = now - timedelta(hours=1)
    entity = EnphaseSiteSensorEntity(coord, "battery", "Battery", type_key="encharge")
    entity._attr_device_class = SensorDeviceClass.BATTERY
    assert not entity.available
    health.last_success_utc = now
    assert entity.available


def test_gateway_firmware_falls_back_to_controller(coordinator_factory):
    """Defensive inventory copies must not defeat gateway firmware fallback."""
    from custom_components.enphase_ev.inventory_view import InventoryView

    coord = coordinator_factory()
    coord._type_device_buckets = {
        "envoy": {
            "count": 2,
            "devices": [
                {"name": "IQ Gateway", "serial_number": "GATEWAY"},
                {
                    "name": "System Controller",
                    "serial_number": "CONTROLLER",
                    "sw_version": "1.2.3",
                },
            ],
        }
    }
    view = InventoryView(coord)
    assert view.type_device_sw_version("envoy") == "1.2.3"
    assert view.type_device_info("envoy")["sw_version"] == "1.2.3"
    members = view._type_bucket_members("envoy")
    members[0]["name"] = "changed"
    assert view._type_bucket_members("envoy")[0]["name"] == "IQ Gateway"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["heatpump", "inverter"])
async def test_recorder_serialization_excludes_live_metadata(
    hass, coordinator_factory, config_entry, kind
):
    """HA entity state info drives recorder exclusion without removing UI data."""
    from homeassistant.components.recorder.db_schema import StateAttributes
    from homeassistant.const import EVENT_STATE_CHANGED
    from custom_components.enphase_ev.sensor_inverter import (
        EnphaseInverterTelemetrySensor,
    )
    from homeassistant.util import dt as dt_util

    coord = coordinator_factory()
    coord.last_success_utc = dt_util.utcnow()
    coord.update_interval = None
    if kind == "heatpump":
        coord._heatpump_daily_consumption = {
            "daily_energy_wh": 1000,
            "day_key": "2026-09-05",
            "timezone": "UTC",
            "details": [1],
            "sampled_at_utc": "first",
        }
        entity = EnphaseHeatPumpDailyEnergySensor(coord)
        diagnostic_key = "details"
    else:
        coord._inverter_data = {
            "INV-A": {"telemetry": {"power": 1000, "sampled_at": {"power": "first"}}}
        }
        entity = EnphaseInverterTelemetrySensor(coord, "INV-A")
        diagnostic_key = "sampled_at"
    entity.hass = hass
    entity.entity_id = f"sensor.recorder_{kind}"
    entity._attr_entity_registry_enabled_default = True
    import logging
    from homeassistant.helpers.entity_component import EntityComponent

    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    events = []
    remove_listener = hass.bus.async_listen(EVENT_STATE_CHANGED, events.append)
    await component.async_add_entities([entity])
    await hass.async_block_till_done()
    first = StateAttributes.shared_attrs_bytes_from_event(events[-1], None)
    assert diagnostic_key in hass.states.get(entity.entity_id).attributes
    assert diagnostic_key.encode() not in first
    if kind == "heatpump":
        coord._heatpump_daily_consumption = {
            **coord._heatpump_daily_consumption,
            "details": [2],
            "sampled_at_utc": "second",
        }
    else:
        coord._inverter_data = {
            "INV-A": {"telemetry": {"power": 1000, "sampled_at": {"power": "second"}}}
        }
    entity.async_write_ha_state()
    await hass.async_block_till_done()
    assert StateAttributes.shared_attrs_bytes_from_event(events[-1], None) == first
    remove_listener()
    await component.async_remove_entity(entity.entity_id)


@pytest.mark.asyncio
async def test_family_freshness_reschedules_without_coordinator_notification(
    hass, coordinator_factory, monkeypatch
):
    """Unchanged successful refreshes advance the eventual outage deadline."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    from custom_components.enphase_ev.sensor_base import EnphaseSiteSensorEntity
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.util import dt as dt_util

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    health = coord._endpoint_family_state("battery_status")
    health.last_success_utc = now[0]
    entity = EnphaseSiteSensorEntity(coord, "expiry_battery", "Battery", "encharge")
    entity.hass = hass
    entity.entity_id = "sensor.expiry_battery"
    entity._attr_device_class = SensorDeviceClass.BATTERY
    entity._attr_native_unit_of_measurement = "%"
    entity._attr_native_value = 75
    await entity.async_added_to_hass()
    entity.async_write_ha_state()
    now[0] += timedelta(minutes=20)
    health.last_success_utc = now[0]  # equal data: coordinator listener suppressed
    now[0] += timedelta(minutes=11)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "75"
    assert entity._cancel_freshness_expiry is not None
    now[0] += timedelta(minutes=20)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unavailable"
    health.last_success_utc = now[0]
    entity._handle_coordinator_update()
    assert hass.states.get(entity.entity_id).state == "75"
    entity._schedule_freshness_expiry()  # replacing a still-active timer
    await entity.async_will_remove_from_hass()
    assert entity._cancel_freshness_expiry is None


def test_missing_family_freshness_is_not_extended_by_core(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor_base import EnphaseSiteSensorEntity
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.util import dt as dt_util

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.last_success_utc = now[0]
    entity = EnphaseSiteSensorEntity(coord, "battery", "Battery", "encharge")
    entity._attr_device_class = SensorDeviceClass.BATTERY
    assert entity.available
    now[0] += timedelta(minutes=31)
    coord.last_success_utc = now[0]
    assert not entity.available


@pytest.mark.parametrize("sample", [float("nan"), float("inf"), -float("inf")])
def test_daily_energy_rejects_nonfinite_samples(coordinator_factory, sample):
    coord = coordinator_factory()
    coord.last_success_utc = datetime.now(timezone.utc)
    coord._heatpump_daily_consumption = {
        "daily_energy_wh": sample,
        "day_key": "2026-09-05",
        "timezone": "UTC",
    }
    entity = EnphaseHeatPumpDailyEnergySensor(coord)
    assert entity.native_value is None
    assert not entity.available


def test_daily_energy_invalid_calendar_and_restore_helpers(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor_common import _normalize_utc_datetime
    from custom_components.enphase_ev.entity import evse_charging_active
    from homeassistant.util import dt as dt_util

    assert not evse_charging_active("enabled")
    assert not evse_charging_active("disabled")
    assert _normalize_utc_datetime(datetime(2026, 9, 5)) == datetime(
        2026, 9, 5, tzinfo=timezone.utc
    )
    assert restore_power_w(SimpleNamespace(state=None)) is None
    coord = coordinator_factory()
    coord._heatpump_daily_consumption = {
        "daily_energy_wh": 1,
        "day_key": "2026-09-05",
        "timezone": "UTC",
    }
    entity = EnphaseHeatPumpDailyEnergySensor(coord)
    monkeypatch.setattr(dt_util, "parse_date", MagicMock(side_effect=ValueError))
    assert entity.last_reset is None


@pytest.mark.asyncio
async def test_inventory_missing_serials_retains_last_complete_cache(
    coordinator_factory,
):
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord._inverters_inventory_payload = {
        "inverters": [{"serial_number": "INV-A"}],
        "total": 1,
    }
    coord.client.inverters_inventory = AsyncMock(
        return_value={"inverters": [{"inverter_id": "2"}], "total": 1}
    )
    await runtime._async_refresh_inverters()
    assert coord._inverter_order == ["INV-A"]
    assert (
        coord._endpoint_family_state("inverter_inventory").last_failure_utc is not None
    )
    # A legacy malformed cache has no authority to introduce device records.
    coord._inverters_inventory_payload = {"inverters": "malformed"}
    await runtime._async_refresh_inverters()
    assert coord._inverter_order == []
    assert runtime._coordinator_backed_attr("unknown_state", "default") == "default"


@pytest.mark.asyncio
async def test_current_power_restore_defensive_paths(
    hass, coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor_site_energy import (
        EnphaseCurrentPowerConsumptionSensor,
    )
    from custom_components.enphase_ev.sensor_base import EnphaseSiteSensorEntity
    from homeassistant.util import dt as dt_util

    coord = coordinator_factory()
    coord.last_success_utc = dt_util.utcnow()
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    entity.hass = hass
    monkeypatch.setattr(EnphaseSiteSensorEntity, "async_added_to_hass", AsyncMock())
    monkeypatch.setattr(
        entity,
        "async_get_last_sensor_data",
        AsyncMock(return_value=SimpleNamespace(native_value="invalid")),
    )
    monkeypatch.setattr(
        entity, "async_get_last_state", AsyncMock(side_effect=ValueError)
    )
    await entity.async_added_to_hass()
    assert entity._last_good_value is None
    monkeypatch.setattr(
        entity,
        "async_get_last_state",
        AsyncMock(
            return_value=State("sensor.power", "1", {"reported_precision": "invalid"})
        ),
    )
    await entity.async_added_to_hass()
    assert entity._last_good_reported_precision is None
    assert entity._freshness_reference_utc() >= coord.last_success_utc


@pytest.mark.asyncio
async def test_wrapped_inventory_preserves_metadata():
    result = await async_fetch_inverter_pages(
        AsyncMock(
            return_value={
                "result": {
                    "inverters": [{"serial_number": "A"}],
                    "total": 1,
                    "site_name": "nested",
                    "count": 1,
                },
                "site_name": "root",
            }
        )
    )
    assert result.complete
    assert result.payload == {
        "inverters": [{"serial_number": "A"}],
        "total": 1,
        "site_name": "root",
        "count": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("native_power", [None, 4200])
async def test_power_restore_converts_display_units_at_entity_boundary(
    hass, coordinator_factory, monkeypatch, native_power
):
    """Legacy kW display values convert to W; canonical native data wins."""
    from custom_components.enphase_ev.entity import EnphaseBaseEntity
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    coord = coordinator_factory()
    monkeypatch.setattr(EnphaseBaseEntity, "async_added_to_hass", AsyncMock())
    entity = EnphasePowerSensor(coord, next(iter(coord.serials)))
    entity.hass = hass
    extra = (
        SimpleNamespace(as_dict=lambda: {"last_power_w": native_power})
        if native_power is not None
        else None
    )
    monkeypatch.setattr(
        entity, "async_get_last_extra_data", AsyncMock(return_value=extra)
    )
    monkeypatch.setattr(
        entity,
        "async_get_last_state",
        AsyncMock(
            return_value=State("sensor.restored", "3.6", {"unit_of_measurement": "kW"})
        ),
    )
    await entity.async_added_to_hass()
    assert entity._last_power_w == (3600 if native_power is None else native_power)
