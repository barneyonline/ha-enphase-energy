"""Array aggregation, independent discovery, completeness and freshness."""

import math
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.const import DOMAIN, OPT_MICROINVERTER_POWER_ENABLED
from custom_components.enphase_ev.sensor_inverter_array import (
    EnphaseInverterArraySensor,
    array_members,
    setup_array_sensors,
)


@pytest.fixture
def array_coord(coordinator_factory):
    coord = coordinator_factory(serials=[])
    coord._selected_type_keys = {"microinverter"}
    coord._devices_inventory_ready = True
    coord._inverters_inventory_payload = {}
    coord.last_update_success = True
    coord.inventory_runtime._set_type_device_buckets(
        {"microinverter": {"count": 3, "devices": []}}, ["microinverter"]
    )
    coord._inverter_order = ["A", "B", "C"]
    coord._inverter_data = {
        "A": {
            "array_name": "North",
            "lifetime_production_wh": 1001,
            "telemetry": {"power": 100},
        },
        "B": {
            "array_name": "North",
            "lifetime_production_wh": 2002,
            "telemetry": {"power": 150},
        },
        "C": {
            "array_name": "West",
            "lifetime_production_wh": 4000,
            "telemetry": {"power": 300},
        },
    }
    coord._inverter_parameter_success_mono = {
        serial: {"power": time.monotonic()} for serial in coord._inverter_order
    }
    return coord


def test_complete_array_totals_and_stable_identity(array_coord):
    energy = EnphaseInverterArraySensor(array_coord, "North")
    power = EnphaseInverterArraySensor(array_coord, "North", power=True)
    assert energy.available and power.available
    assert energy.native_value == 3.003
    assert power.native_value == 250
    assert energy.state_class == SensorStateClass.TOTAL
    assert power.state_class == SensorStateClass.MEASUREMENT
    assert energy.extra_state_attributes == {"array_name": "North", "inverter_count": 2}
    assert energy.device_info == power.device_info
    assert (
        energy.unique_id == EnphaseInverterArraySensor(array_coord, "North").unique_id
    )
    assert (
        energy.unique_id != EnphaseInverterArraySensor(array_coord, "N-orth").unique_id
    )
    array_coord._inverter_order.reverse()
    assert energy.native_value == 3.003
    assert EnphaseInverterArraySensor(array_coord, "West").native_value == 4
    assert energy._freshness_deadline() is None


@pytest.mark.parametrize("power", [False, True])
@pytest.mark.parametrize("invalid", [None, True, "invalid", math.nan, math.inf, -1])
def test_incomplete_array_never_reports_partial_total(array_coord, power, invalid):
    sensor = EnphaseInverterArraySensor(array_coord, "North", power=power)
    if power:
        array_coord._inverter_data["B"]["telemetry"]["power"] = invalid
    else:
        array_coord._inverter_data["B"]["lifetime_production_wh"] = invalid
    assert sensor.native_value is None
    assert not sensor.available


def test_zero_is_valid_and_membership_changes_are_not_meter_resets(array_coord):
    energy = EnphaseInverterArraySensor(array_coord, "North")
    array_coord._inverter_data["B"]["array_name"] = "West"
    assert energy.native_value == 1.001
    assert energy.state_class == SensorStateClass.TOTAL
    array_coord._inverter_data["A"]["lifetime_production_wh"] = 0
    assert energy.native_value == 0
    array_coord._inverter_data["A"]["telemetry"]["power"] = 0
    assert (
        EnphaseInverterArraySensor(array_coord, "North", power=True).native_value == 0
    )


@pytest.mark.parametrize("stamp", [None, float("nan"), -10000, float("inf")])
def test_power_requires_fresh_samples_for_every_member(array_coord, stamp):
    array_coord._inverter_parameter_success_mono["B"]["power"] = stamp
    sensor = EnphaseInverterArraySensor(array_coord, "North", power=True)
    assert not sensor.available
    assert sensor.native_value is None


def test_future_sample_and_missing_array_are_unavailable(array_coord):
    array_coord._inverter_parameter_success_mono["B"]["power"] = time.monotonic() + 100
    assert not EnphaseInverterArraySensor(array_coord, "North", power=True).available
    absent = EnphaseInverterArraySensor(array_coord, "Absent", power=True)
    assert absent.native_value is None
    assert absent._freshness_deadline() <= dt_util.utcnow()


def test_names_and_authoritative_inventory(array_coord):
    array_coord._inverter_data["A"]["array_name"] = " North "
    array_coord._inverter_data["C"]["array_name"] = None
    assert array_members(array_coord) == {"North": ("A", "B")}
    array_coord._devices_inventory_ready = False
    assert array_members(array_coord) == {}
    assert not EnphaseInverterArraySensor(array_coord, "North").available


@pytest.mark.parametrize("power_enabled", [False, True])
async def test_discovery_independent_of_microinverter_power_option(
    hass, config_entry, array_coord, power_enabled
):
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_MICROINVERTER_POWER_ENABLED: power_enabled}
    )
    callbacks = []
    array_coord.async_add_listener = lambda cb: callbacks.append(cb) or (lambda: None)
    added = []
    setup_array_sensors(
        config_entry,
        array_coord,
        added.extend,
    )
    assert len(added) == 4
    assert {entity._power for entity in added} == {False, True}
    assert all(entity.entity_registry_enabled_default for entity in added)
    callbacks[0]()
    assert len(added) == 4
    array_coord._inverter_data["C"]["array_name"] = "East"
    callbacks[0]()
    assert len(added) == 6
    assert {entity._array_name for entity in added} == {"North", "West", "East"}


async def test_power_opt_out_preserves_registered_array_power(
    hass, config_entry, array_coord
):
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_MICROINVERTER_POWER_ENABLED: False}
    )
    registry = er.async_get(hass)
    power = EnphaseInverterArraySensor(array_coord, "North", power=True)
    energy = EnphaseInverterArraySensor(array_coord, "North")
    entries = [
        registry.async_get_or_create("sensor", DOMAIN, uid, config_entry=config_entry)
        for uid in (power.unique_id, energy.unique_id, "unrelated_power")
    ]
    setup_array_sensors(
        config_entry,
        array_coord,
        Mock(),
    )
    assert all(registry.async_get(entry.entity_id) for entry in entries)


@pytest.mark.parametrize("provider_timestamp", [False, True])
async def test_power_expires_without_coordinator_updates(
    hass, array_coord, monkeypatch, provider_timestamp
):
    from datetime import timedelta
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.enphase_ev import sensor_inverter_array as array_module

    clock = [time.monotonic()]
    monkeypatch.setattr(
        array_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    sensor = EnphaseInverterArraySensor(array_coord, "North", power=True)
    sensor.hass = hass
    sensor.entity_id = "sensor.north_array_power"
    sensor.async_write_ha_state = Mock()
    if provider_timestamp:
        array_coord._inverter_data["B"]["telemetry"]["sampled_at"] = {
            "power": (dt_util.utcnow() - timedelta(seconds=1799)).isoformat()
        }
    else:
        array_coord._inverter_parameter_success_mono["B"]["power"] = clock[0] - 1799
    await sensor.async_added_to_hass()
    assert sensor.available
    clock[0] += 2
    future = dt_util.utcnow() + timedelta(seconds=2)
    monkeypatch.setattr(dt_util, "utcnow", lambda: future)
    async_fire_time_changed(hass, future)
    await hass.async_block_till_done()
    sensor.async_write_ha_state.assert_called()
    assert not sensor.available
    assert sensor.native_value is None
    await sensor.async_will_remove_from_hass()


@pytest.mark.parametrize("sample", ["old", "invalid", "future"])
def test_power_rejects_stale_or_invalid_provider_timestamp(array_coord, sample):
    from datetime import timedelta

    timestamps = {
        "old": (dt_util.utcnow() - timedelta(hours=4)).isoformat(),
        "invalid": "not-a-timestamp",
        "future": (dt_util.utcnow() + timedelta(hours=1)).isoformat(),
    }
    array_coord._inverter_data["B"]["telemetry"]["sampled_at"] = {
        "power": timestamps[sample]
    }
    sensor = EnphaseInverterArraySensor(array_coord, "North", power=True)
    assert sensor.native_value is None
    assert not sensor.available


def test_power_deadline_uses_oldest_provider_measurement(array_coord):
    from datetime import timedelta

    sampled = dt_util.utcnow() - timedelta(minutes=20)
    array_coord._inverter_data["B"]["telemetry"]["sampled_at"] = {
        "power": sampled.isoformat()
    }
    sensor = EnphaseInverterArraySensor(array_coord, "North", power=True)
    assert sensor.native_value == 250
    assert sensor._freshness_deadline() == sampled + timedelta(minutes=30)
