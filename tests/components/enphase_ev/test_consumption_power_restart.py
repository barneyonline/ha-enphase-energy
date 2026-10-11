"""Restart recovery preserves a valid reading's original freshness deadline."""

import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import orjson
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers import restore_state
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseSiteConsumptionPowerSensor,
    _SiteConsumptionPowerRestoreData,
)


def stored_balance(timestamp, **changes):
    data = {"balance_power_w": 2000, "balance_sample_ts": timestamp.timestamp()}
    data.update(changes)
    return _SiteConsumptionPowerRestoreData.from_dict(data)


def publish_power(hass, coord, timestamp, values=(1000, 1500, -500)):
    entities = []
    for key, value in zip(
        ("current_production_power", "grid_power", "battery_power"), values, strict=True
    ):
        entry = er.async_get(hass).async_get_or_create(
            "sensor", DOMAIN, f"{DOMAIN}_site_{coord.site_id}_{key}"
        )
        hass.states.async_set(
            entry.entity_id,
            value,
            {"unit_of_measurement": "W", "sampled_at_utc": timestamp.isoformat()},
        )
        entities.append(entry.entity_id)
    return entities


async def add_sensor(
    hass, config_entry, coord, *, extra=None, last_state=None, use_cache=False
):
    coord.update_interval = None
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.entity_id = "sensor.restart_consumption"
    if not use_cache:
        sensor.async_get_last_extra_data = AsyncMock(return_value=extra)
        sensor.async_get_last_state = AsyncMock(return_value=last_state)
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    return sensor, component


async def test_balance_survives_restart_without_renewing_sample_age(
    hass, config_entry, coordinator_factory, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    accepted_at = now[0]
    sources = publish_power(hass, coord, accepted_at)
    original, component = await add_sensor(hass, config_entry, coord)
    assert hass.states.get(original.entity_id).state == "2000"
    cache = restore_state.async_get(hass)

    def persisted_record(entity_id):
        record = next(
            record
            for record in cache.async_get_stored_states()
            if record.state.entity_id == entity_id
        )
        return restore_state.StoredState.from_dict(
            orjson.loads(orjson.dumps(record.as_dict()))
        )

    saved = persisted_record(original.entity_id)
    for entity_id in sources:
        hass.states.async_remove(entity_id)
    await component.async_remove_entity(original.entity_id)
    cache.last_states[original.entity_id] = saved
    now[0] += timedelta(minutes=10)
    coord = coordinator_factory()
    restored, component = await add_sensor(hass, config_entry, coord, use_cache=True)
    assert hass.states.get(restored.entity_id).state == "2000"
    assert hass.states.get(restored.entity_id).attributes["using_cached"] is True
    assert restored.extra_state_attributes["sampled_at_utc"] == accepted_at.isoformat()
    # A restored display reading must not seed a cumulative-energy baseline.
    assert restored.extra_restore_state_data.as_dict()["latest_bucket_wh"] is None
    assert restored.extra_restore_state_data.as_dict()["last_power_w"] is None

    # A second restart keeps the same persisted clock and remaining grace.
    saved_again = persisted_record(restored.entity_id)
    await component.async_remove_entity(restored.entity_id)
    cache.last_states[restored.entity_id] = saved_again
    now[0] += timedelta(minutes=10)
    restored, component = await add_sensor(hass, config_entry, coord, use_cache=True)
    assert hass.states.get(restored.entity_id).state == "2000"
    now[0] = accepted_at + timedelta(seconds=1799)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(restored.entity_id).state == "2000"
    now[0] += timedelta(seconds=1)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(restored.entity_id).state == "unavailable"

    publish_power(hass, coord, now[0], (2000, 1000, -250))
    await hass.async_block_till_done()
    assert hass.states.get(restored.entity_id).state == "2750"
    assert restored.extra_state_attributes["using_cached"] is False
    await component.async_remove_entity(restored.entity_id)


async def test_live_startup_reading_takes_priority_over_restore(
    hass, config_entry, coordinator_factory
):
    now = dt_util.utcnow()
    coord = coordinator_factory()
    publish_power(hass, coord, now)
    sensor, component = await add_sensor(
        hass, config_entry, coord, extra=stored_balance(now, balance_power_w=999)
    )
    assert hass.states.get(sensor.entity_id).state == "2000"
    assert sensor.extra_state_attributes["using_cached"] is False
    await component.async_remove_entity(sensor.entity_id)


@pytest.mark.parametrize("power_w", [0, 2000])
async def test_stale_startup_inputs_do_not_replace_fresh_restore(
    hass, config_entry, coordinator_factory, monkeypatch, power_w
):
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    coord = coordinator_factory()
    publish_power(hass, coord, now - timedelta(minutes=31))
    sampled_at = now - timedelta(minutes=5)
    sensor, component = await add_sensor(
        hass,
        config_entry,
        coord,
        extra=stored_balance(sampled_at, balance_power_w=power_w),
    )
    assert hass.states.get(sensor.entity_id).state == str(power_w)
    assert sensor.extra_state_attributes["sampled_at_utc"] == sampled_at.isoformat()
    assert sensor.extra_state_attributes["using_cached"] is True
    await component.async_remove_entity(sensor.entity_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"balance_power_w": None},
        {"balance_power_w": -1},
        {"balance_power_w": -0.1},
        {"balance_power_w": 0.5},
        {"balance_power_w": True},
        {"balance_power_w": "invalid"},
        {"balance_power_w": float("nan")},
        {"balance_power_w": float("inf")},
        {"balance_power_w": 1_000_000},
        {"balance_sample_ts": None},
        {"balance_sample_ts": 0},
        {"balance_sample_ts": -1},
        {"balance_sample_ts": float("nan")},
        {"balance_sample_ts": float("inf")},
        {"balance_sample_ts": 1e100},
        {"age": -1},
        {"age": 1800},
        {"age": 1801},
    ],
)
async def test_invalid_or_expired_balance_is_not_restored(
    hass, config_entry, coordinator_factory, monkeypatch, changes
):
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    changes = dict(changes)
    age = changes.pop("age", 60)
    sensor, component = await add_sensor(
        hass,
        config_entry,
        coordinator_factory(),
        extra=stored_balance(now - timedelta(seconds=age), **changes),
    )
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    assert sensor.native_value is None
    assert sensor._cancel_freshness_expiry is None
    await component.async_remove_entity(sensor.entity_id)


@pytest.mark.parametrize("battery_present", [False, True])
@pytest.mark.parametrize("value,unit,expected", [("2", "kW", "2000"), ("0", "W", "0")])
async def test_legacy_balance_restore_uses_original_clocks_and_display_unit(
    hass, config_entry, coordinator_factory, battery_present, value, unit, expected
):
    now = dt_util.utcnow()
    sampled = now - timedelta(minutes=5)
    coord = coordinator_factory()
    coord._battery_has_encharge = battery_present
    coord._battery_has_acb = False
    clocks = {
        "current_production_power": sampled.isoformat(),
        "grid_power": now.isoformat(),
    }
    if battery_present:
        clocks["battery_power"] = now.isoformat()
    sensor, component = await add_sensor(
        hass,
        config_entry,
        coord,
        last_state=State(
            "sensor.restart_consumption",
            value,
            {
                "method": "power_balance",
                "unit_of_measurement": unit,
                "source_sampled_at_utc": clocks,
            },
        ),
    )
    assert hass.states.get(sensor.entity_id).state == expected
    assert sensor.extra_state_attributes["sampled_at_utc"] == sampled.isoformat()
    assert sensor.extra_state_attributes["using_cached"] is True
    await component.async_remove_entity(sensor.entity_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "seeded"},
        {"source_sampled_at_utc": None},
        {"source_sampled_at_utc": {}},
        {"clock": "invalid"},
        {"clock": float("inf")},
        {"clock": 0},
        {"clock_age": 1800},
        {"clock_age": -1},
        {"future_source": True},
        {"value": "unavailable"},
        {"value": "-2"},
        {"unit_of_measurement": "kWh"},
    ],
)
async def test_legacy_balance_requires_valid_value_and_every_source_clock(
    hass, config_entry, coordinator_factory, monkeypatch, changes
):
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    changes = dict(changes)
    clock = changes.pop(
        "clock", (now - timedelta(seconds=changes.pop("clock_age", 60))).isoformat()
    )
    attrs = {
        "method": "power_balance",
        "unit_of_measurement": "W",
        "source_sampled_at_utc": dict.fromkeys(
            ("current_production_power", "grid_power", "battery_power"), clock
        ),
    }
    value = changes.pop("value", "2000")
    if changes.pop("future_source", False):
        attrs["source_sampled_at_utc"]["grid_power"] = (
            now + timedelta(hours=1)
        ).isoformat()
    attrs.update(changes)
    sensor, component = await add_sensor(
        hass,
        config_entry,
        coordinator_factory(),
        last_state=State("sensor.restart_consumption", value, attrs),
    )
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    assert sensor.native_value is None
    await component.async_remove_entity(sensor.entity_id)
