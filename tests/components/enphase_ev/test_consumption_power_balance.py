"""Consumption responds to published power readings without a bucket warmup."""

import logging
from unittest.mock import AsyncMock
from homeassistant.helpers.entity_component import EntityComponent

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseSiteConsumptionPowerSensor,
)


def source(hass, coord, key, value, unit="W", name=None):
    entry = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_{key}",
        suggested_object_id=name or key,
    )
    if value is not None:
        hass.states.async_set(entry.entity_id, value, {"unit_of_measurement": unit})
    return entry.entity_id


@pytest.mark.parametrize(
    ("production", "grid", "battery", "expected"),
    [
        (1992, 0, 0, 1992),
        (1992, -1000, -500, 492),
        (0, 1000, 500, 1500),
        (100, -500, 0, 0),
    ],
)
async def test_immediate_balance(
    hass, coordinator_factory, production, grid, battery, expected
):
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    source(hass, coord, "current_production_power", production)
    source(hass, coord, "grid_power", grid)
    source(hass, coord, "battery_power", battery)
    assert sensor.native_value == expected
    assert sensor.available
    assert sensor.extra_state_attributes["method"] == "power_balance"
    assert coord.energy.consumption_power_diagnostics["power_balance_w"] == expected


async def test_source_updates_renames_and_late_creation(
    hass, config_entry, coordinator_factory
):
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    sensor.entity_id = "sensor.test_consumption"
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    production = source(
        hass, coord, "current_production_power", "1.992", "kW", "renamed_solar"
    )
    grid = source(hass, coord, "grid_power", 0)
    battery = source(hass, coord, "battery_power", 0)
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "1992"
    hass.states.async_set(battery, -500, {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "1492"
    registry = er.async_get(hass)
    registry.async_update_entity(production, new_entity_id="sensor.solar_renamed_again")
    await hass.async_block_till_done()
    hass.states.async_set(
        "sensor.solar_renamed_again", 1000, {"unit_of_measurement": "W"}
    )
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "500"
    source(hass, coord, "unrelated_power", 4000)
    hass.states.async_set("sensor.unregistered_power", 5000)
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "500"

    hass.states.async_set(grid, 300, {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "800"
    await component.async_remove_entity(sensor.entity_id)
    removed_state = hass.states.get(sensor.entity_id)
    hass.states.async_set(grid, 600, {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id) is removed_state
    assert sensor._balance_power_w == 800


@pytest.mark.parametrize(
    "value,unit",
    [
        (None, "W"),
        ("unavailable", "W"),
        ("unknown", "W"),
        ("nan", "W"),
        ("inf", "W"),
        (1, "kWh"),
        (1, None),
    ],
)
async def test_missing_invalid_inputs_are_not_zero(
    hass, coordinator_factory, value, unit
):
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    source(hass, coord, "current_production_power", 1992)
    assert sensor.native_value is None
    source(hass, coord, "grid_power", value, unit)
    source(hass, coord, "battery_power", 0)
    assert sensor.native_value is None
    assert not sensor.available


async def test_no_battery_and_retained_reading(hass, coordinator_factory):
    coord = coordinator_factory()
    coord._battery_has_encharge = False
    coord._battery_has_acb = False
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    source(hass, coord, "current_production_power", 1992)
    grid = source(hass, coord, "grid_power", -1000)
    assert sensor.native_value == 992
    hass.states.async_set(grid, "unavailable")
    assert sensor.native_value == 992
    assert sensor.available
    assert sensor.extra_state_attributes["using_cached"] is True
    hass.states.async_set(grid, 0, {"unit_of_measurement": "W"})
    assert sensor.native_value == 1992
    assert sensor.extra_state_attributes["using_cached"] is False


async def test_unknown_battery_requires_reading(hass, coordinator_factory):
    coord = coordinator_factory()
    coord._battery_has_encharge = None
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    source(hass, coord, "current_production_power", 1992)
    source(hass, coord, "grid_power", 0)
    assert sensor.native_value is None


async def test_balance_bypasses_restored_bucket_validation(hass, coordinator_factory):
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    sensor._restored_pending_validation = True
    sensor._last_power_w = 900
    source(hass, coord, "current_production_power", 1992)
    source(hass, coord, "grid_power", 0)
    source(hass, coord, "battery_power", 0)
    assert sensor.available
    assert sensor.native_value == 1992
    assert not coord.energy.consumption_power_diagnostics["restored_pending_validation"]
