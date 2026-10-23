"""Consumption responds to published power readings without a bucket warmup."""

import logging
from datetime import timedelta
from unittest.mock import AsyncMock
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.entity_platform import EntityPlatform

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseBatteryPowerSensor,
    EnphaseGridPowerSensor,
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


async def test_balance_timer_uses_oldest_sample_and_recovers(
    hass, config_entry, coordinator_factory, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.entity_id = "sensor.consumption_expiry"
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)
    accepted_at = now[0]
    keys = ("current_production_power", "grid_power", "battery_power")
    entities = [source(hass, coord, key, 1000) for key in keys]

    def publish(source_time):
        for entity_id in entities:
            hass.states.async_set(
                entity_id,
                1000,
                {
                    "unit_of_measurement": "W",
                    "sampled_at_utc": source_time.isoformat(),
                    "cached_at_utc": now[0].isoformat(),
                },
            )

    publish(accepted_at)
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    assert hass.states.get(sensor.entity_id).state == "3000"
    now[0] += timedelta(seconds=1799)
    publish(accepted_at)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "3000"
    # Successful acquisitions and attribute changes cannot renew non-idle watts.
    now[0] += timedelta(seconds=1)
    publish(accepted_at)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    # Updating just one contributing sample cannot renew the whole balance.
    hass.states.async_set(
        entities[0],
        1000,
        {"unit_of_measurement": "W", "sampled_at_utc": now[0].isoformat()},
    )
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    publish(now[0])
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "3000"
    assert sensor._cancel_freshness_expiry is not None
    # Missing data retains only the bounded last valid calculation.
    hass.states.async_set(entities[1], "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "3000"
    now[0] += timedelta(minutes=30)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    await component.async_remove_entity(sensor.entity_id)


@pytest.mark.parametrize("clock", ["nan", "inf", -1, 1e100])
@pytest.mark.parametrize(
    "attribute", ["sampled_at_utc", "cached_at_utc", "freshness_reference_utc"]
)
async def test_invalid_balance_clock_cannot_seed(
    hass, coordinator_factory, clock, attribute
):
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    entity_id = source(hass, coord, "current_production_power", 1000)
    source(hass, coord, "grid_power", 0)
    source(hass, coord, "battery_power", 0)
    hass.states.async_set(
        entity_id, 1000, {"unit_of_measurement": "W", attribute: clock}
    )
    assert not sensor.available
    assert sensor.native_value is None


async def test_idle_production_and_acquisition_bound_balance(
    hass, coordinator_factory, monkeypatch
):
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    production = source(hass, coord, "current_production_power", 0)
    source(hass, coord, "grid_power", 1000)
    source(hass, coord, "battery_power", 0)
    attrs = {
        "unit_of_measurement": "W",
        "sampled_at_utc": (now - timedelta(hours=6)).isoformat(),
        "cached_at_utc": now.isoformat(),
    }
    hass.states.async_set(production, 0, attrs)
    assert sensor.available
    assert sensor.native_value == 1000
    # A fresh source clock cannot hide a stale acquisition; a missing clock also
    # follows acquisition rather than a recent HA attribute publication.
    attrs["cached_at_utc"] = (now - timedelta(minutes=30)).isoformat()
    attrs["sampled_at_utc"] = now.isoformat()
    hass.states.async_set(production, 1000, attrs)
    assert sensor.available
    assert sensor.native_value == 1000
    assert sensor.extra_state_attributes["using_cached"] is True
    attrs.pop("sampled_at_utc")
    hass.states.async_set(production, 1000, attrs)
    assert sensor.available
    assert sensor.native_value == 1000
    assert sensor.extra_state_attributes["using_cached"] is True


async def test_future_ha_publication_cannot_seed_missing_source_clock(
    hass, coordinator_factory, monkeypatch
):
    from homeassistant.core import State

    now = dt_util.utcnow()
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    for key in ("current_production_power", "grid_power", "battery_power"):
        source(hass, coord, key, 1000)
    monkeypatch.setattr(
        type(hass.states),
        "get",
        lambda _self, entity_id: State(
            entity_id,
            "1000",
            {"unit_of_measurement": "W"},
            last_updated=now + timedelta(days=1),
        ),
    )
    assert not sensor.available
    assert sensor.native_value is None


async def test_stale_numeric_source_preserves_last_valid_balance(
    hass, config_entry, coordinator_factory, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.entity_id = "sensor.retained_consumption"
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)
    sensor.async_get_last_state = AsyncMock(return_value=None)
    accepted_at = now[0]
    for key in ("current_production_power", "grid_power", "battery_power"):
        entity_id = source(hass, coord, key, 1000)
        hass.states.async_set(
            entity_id,
            1000,
            {"unit_of_measurement": "W", "sampled_at_utc": accepted_at.isoformat()},
        )
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    assert hass.states.get(sensor.entity_id).state == "3000"
    now[0] += timedelta(minutes=5)
    hass.states.async_set(
        entity_id,
        2000,
        {
            "unit_of_measurement": "W",
            "sampled_at_utc": (now[0] - timedelta(minutes=31)).isoformat(),
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "3000"
    assert sensor.extra_state_attributes["using_cached"] is True
    assert sensor.extra_state_attributes["sampled_at_utc"] == accepted_at.isoformat()
    now[0] = accepted_at + timedelta(minutes=30)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    await component.async_remove_entity(sensor.entity_id)


@pytest.mark.parametrize("kind", ["grid", "battery"])
async def test_balance_obeys_oldest_flow_clock(
    hass, config_entry, coordinator_factory, monkeypatch, kind
):
    from custom_components.enphase_ev.sensor_site_energy import (
        EnphaseGridPowerSensor,
        EnphaseBatteryPowerSensor,
    )

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord._battery_has_encharge = kind == "battery"
    coord._battery_has_acb = False
    entity = (EnphaseGridPowerSensor if kind == "grid" else EnphaseBatteryPowerSensor)(
        coord
    )
    first_flow, second_flow = entity._flow_signs
    oldest = now[0] - timedelta(minutes=20)
    coord.energy.site_energy = {
        first_flow: {
            "value_kwh": 1,
            "last_report_date": now[0] - timedelta(minutes=5),
        },
        second_flow: {"value_kwh": 1, "last_report_date": oldest},
    }
    assert entity.native_value is None
    coord.energy.site_energy[first_flow] = {
        "value_kwh": 1.5,
        "last_report_date": now[0],
    }
    entity.entity_id = source(hass, coord, kind + "_power", None)
    hass.states.async_set(
        entity.entity_id,
        entity.native_value,
        {"unit_of_measurement": "W", **entity.extra_state_attributes},
    )
    if kind == "battery":
        source(hass, coord, "grid_power", 0)
    production = source(hass, coord, "current_production_power", 1000)
    hass.states.async_set(
        production,
        1000,
        {"unit_of_measurement": "W", "sampled_at_utc": now[0].isoformat()},
    )
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.entity_id = "sensor.flow_bound_consumption"
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    expected = max(1000 + entity._flow_signs[first_flow] * 6000, 0)
    assert hass.states.get(sensor.entity_id).state == str(expected)
    now[0] += timedelta(minutes=10)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert not entity.available
    hass.states.async_set(entity.entity_id, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    await component.async_remove_entity(sensor.entity_id)


@pytest.mark.parametrize("kind", ["grid", "battery"])
@pytest.mark.parametrize(
    "rejection", ["outlier_ignored", "extreme_pending", "source_changed_reseed"]
)
async def test_rejected_flow_calculations_cannot_renew_consumption(
    hass, config_entry, coordinator_factory, monkeypatch, kind, rejection
):
    """New payload clocks cannot renew watts from a rejected calculation."""
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    accepted_at = now[0]
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    coord._battery_has_encharge = True
    power = (EnphaseGridPowerSensor if kind == "grid" else EnphaseBatteryPowerSensor)(
        coord
    )
    flow = next(iter(power._flow_signs))
    value = 1.0

    def payload(timestamp, *, fields=None):
        coord.energy.site_energy = {
            flow: {
                "value_kwh": value,
                "last_report_date": timestamp,
                "fields_used": fields or [flow],
                "interval_minutes": 5,
            }
        }

    other_entities = [
        source(hass, coord, key, 1000)
        for key in ("current_production_power", "grid_power", "battery_power")
        if key != kind + "_power"
    ]
    power.entity_id = source(hass, coord, kind + "_power", None)
    consumption = EnphaseSiteConsumptionPowerSensor(coord)
    consumption.entity_id = "sensor.rejected_balance"
    consumption.async_get_last_extra_data = AsyncMock(return_value=None)
    consumption.async_get_last_state = AsyncMock(return_value=None)
    power.async_get_last_state = AsyncMock(return_value=None)
    power.async_get_last_extra_data = AsyncMock(return_value=None)
    payload(accepted_at - timedelta(minutes=5))
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"] = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="sensor",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    component._platforms["sensor"].config_entry = config_entry
    await component._platforms["sensor"].async_add_entities([power, consumption])

    value += 0.1
    payload(accepted_at)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    expected = 2000 + 1200 * power._flow_signs[flow]
    assert hass.states.get(consumption.entity_id).state == str(expected)

    for index in range(1, 7):
        now[0] = accepted_at + timedelta(minutes=5 * index)
        if rejection == "outlier_ignored":
            value += 10
        elif rejection == "extreme_pending":
            # Keep successive extremes incomparable rather than confirming them.
            value += 1000 * 3**index
        payload(
            now[0],
            fields=[str(index)] if rejection == "source_changed_reseed" else None,
        )
        for entity_id in other_entities:
            hass.states.async_set(
                entity_id,
                1000,
                {"unit_of_measurement": "W", "sampled_at_utc": now[0].isoformat()},
            )
        coord.async_update_listeners()
        async_fire_time_changed(hass, now[0])
        await hass.async_block_till_done()
        assert power.extra_state_attributes["method"] == rejection
        assert power.extra_state_attributes["freshness_reference_utc"] == (
            accepted_at.isoformat()
        )
        if index < 6:
            assert hass.states.get(consumption.entity_id).state == str(expected)
            assert consumption.extra_state_attributes["using_cached"] is True
            assert consumption.extra_state_attributes["sampled_at_utc"] == (
                accepted_at.isoformat()
            )
        else:
            assert hass.states.get(power.entity_id).state == "unavailable"
            assert hass.states.get(consumption.entity_id).state == "unavailable"

    # A new valid delta recovers through the normal coordinator publication path.
    now[0] += timedelta(minutes=5)
    value += 0.1
    payload(now[0], fields=["6"] if rejection == "source_changed_reseed" else None)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(consumption.entity_id).state == str(expected)
    assert consumption.extra_state_attributes["using_cached"] is False
    assert coord.energy.consumption_power_diagnostics["power_balance_blocker"] is None
    await component.async_remove_entity(consumption.entity_id)
    await component.async_remove_entity(power.entity_id)


@pytest.mark.parametrize(
    ("attributes", "reason"),
    [
        ({"method": "seeded"}, "incomplete_source"),
        ({"method": "no_live_data"}, "incomplete_source"),
        ({"freshness_reference_utc": "bad-clock"}, "invalid_timestamp"),
    ],
)
async def test_balance_diagnostics_distinguish_blocked_source_from_bucket_history(
    hass, coordinator_factory, attributes, reason
):
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    source(hass, coord, "current_production_power", 1000)
    grid = source(hass, coord, "grid_power", 0)
    source(hass, coord, "battery_power", 0)
    hass.states.async_set(grid, 0, {"unit_of_measurement": "W", **attributes})
    assert not sensor.available
    diagnostics = coord.energy.consumption_power_diagnostics
    assert diagnostics["power_balance_blocker"] == {
        "source": "grid_power",
        "reason": reason,
    }
    rejection = diagnostics["last_rejection"]
    assert rejection["reason"] == "missing_consumption"
    hass.states.async_set(grid, 0, {"unit_of_measurement": "W"})
    assert sensor.native_value == 1000
    recovered = coord.energy.consumption_power_diagnostics
    assert recovered["power_balance_blocker"] is None
    assert recovered["last_rejection"] == rejection
    assert recovered["power_balance_source_freshness"]["grid_power"] == (
        hass.states.get(grid).last_updated.isoformat()
    )
    assert diagnostics["power_balance_blocker"]["reason"] == reason


@pytest.mark.parametrize("flag", ["using_stale", "using_cached"])
async def test_balance_reports_cached_upstream_input(hass, coordinator_factory, flag):
    coord = coordinator_factory()
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sensor.hass = hass
    production = source(hass, coord, "current_production_power", 1000)
    source(hass, coord, "grid_power", 0)
    source(hass, coord, "battery_power", 0)
    hass.states.async_set(production, 1000, {"unit_of_measurement": "W", flag: True})
    assert sensor.native_value == 1000
    assert sensor.extra_state_attributes["using_cached"] is True


@pytest.mark.parametrize("stored_reference", ["older", "future", "invalid"])
async def test_restored_flow_power_preserves_accepted_freshness(
    hass, coordinator_factory, monkeypatch, stored_reference
):
    from homeassistant.core import State

    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    for minutes, value in ((10, 1), (5, 1.1)):
        coord.energy.site_energy = {
            "grid_import": {
                "value_kwh": value,
                "last_report_date": now - timedelta(minutes=minutes),
                "fields_used": ["import"],
            }
        }
        sensor.native_value
    attributes = sensor.extra_state_attributes
    original_reference = now - timedelta(minutes=5)
    expected = original_reference
    if stored_reference == "older":
        expected = now - timedelta(minutes=20)
        attributes["freshness_reference_utc"] = expected.isoformat()
    elif stored_reference == "future":
        attributes["freshness_reference_utc"] = (now + timedelta(days=1)).isoformat()
    else:
        attributes["freshness_reference_utc"] = float("inf")
    restored = EnphaseGridPowerSensor(coord)
    restored.hass = hass
    restored.async_get_last_state = AsyncMock(
        return_value=State("sensor.restored_grid", "1200", attributes)
    )
    restored.async_get_last_extra_data = AsyncMock(
        return_value=sensor.extra_restore_state_data.legacy
    )
    # This parser test uses a manually attached entity; platform expiry is tested above.
    monkeypatch.setattr(restored, "_schedule_freshness_expiry", lambda: None)
    await restored.async_added_to_hass()
    assert restored.extra_state_attributes["freshness_reference_utc"] == (
        expected.isoformat()
    )
    assert restored.native_value == 1200
    await restored.async_will_remove_from_hass()


@pytest.mark.parametrize("kind", ["grid", "battery"])
@pytest.mark.parametrize("restore", [False, True])
async def test_core_outage_deadline_does_not_become_accepted_source_clock(
    hass, coordinator_factory, monkeypatch, kind, restore
):
    from homeassistant.core import State

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    accepted_at = now[0]
    coord = coordinator_factory()
    coord.last_success_utc = accepted_at - timedelta(minutes=29)
    coord.last_update_success = False
    sensor = (EnphaseGridPowerSensor if kind == "grid" else EnphaseBatteryPowerSensor)(
        coord
    )
    flow = next(iter(sensor._flow_signs))
    for minutes, value in ((5, 1), (0, 1.1)):
        coord.energy.site_energy = {
            flow: {
                "value_kwh": value,
                "last_report_date": accepted_at - timedelta(minutes=minutes),
                "fields_used": [flow],
            }
        }
        _ = sensor.native_value
    expected = 1200 * sensor._flow_signs[flow]
    assert sensor.native_value == expected
    assert sensor.extra_state_attributes["freshness_reference_utc"] == (
        accepted_at.isoformat()
    )
    if restore:
        previous = sensor
        sensor = type(previous)(coord)
        sensor.hass = hass
        sensor.async_get_last_state = AsyncMock(
            return_value=State(
                "sensor.core_outage_power",
                str(expected),
                previous.extra_state_attributes,
            )
        )
        sensor.async_get_last_extra_data = AsyncMock(
            return_value=previous.extra_restore_state_data
        )
        monkeypatch.setattr(sensor, "_schedule_freshness_expiry", lambda: None)
        await sensor.async_added_to_hass()
    now[0] = accepted_at + timedelta(minutes=2)
    assert not sensor.available  # The existing core-outage bound still applies.
    coord.last_update_success = True
    coord.last_success_utc = now[0]
    assert sensor.available
    assert sensor.native_value == expected
    assert sensor.extra_state_attributes["freshness_reference_utc"] == (
        accepted_at.isoformat()
    )
    now[0] = accepted_at + timedelta(minutes=30)
    assert not sensor.available  # Core recovery cannot renew the source clock.
    if restore:
        await sensor.async_will_remove_from_hass()


@pytest.mark.parametrize("timestamp", [1e20, 1e100])
async def test_out_of_range_restored_power_clock_cannot_break_setup(
    hass, coordinator_factory, monkeypatch, timestamp
):
    from homeassistant.core import State
    from custom_components.enphase_ev.sensor_site_energy import (
        _SiteLifetimePowerRestoreData,
    )

    coord = coordinator_factory()
    coord.energy.site_energy = {}
    sensor = EnphaseGridPowerSensor(coord)
    sensor.hass = hass
    sensor.async_get_last_state = AsyncMock(
        return_value=State(
            "sensor.invalid_restored_power",
            "1200",
            {
                "last_flow_kwh": {"grid_import": 1.1},
                "last_sample_ts": timestamp,
                "last_energy_ts": timestamp,
                "method": "lifetime_energy_window",
            },
        )
    )
    baseline = (dt_util.utcnow() - timedelta(minutes=5)).timestamp()
    sensor.async_get_last_extra_data = AsyncMock(
        return_value=_SiteLifetimePowerRestoreData(
            previous_live_flow_kwh={"grid_import": 1},
            previous_live_sample_ts=baseline,
            previous_live_energy_ts=baseline,
            last_live_interval_minutes=5,
            last_live_flow_sources={"grid_import": ("import",)},
            previous_live_flow_sources={"grid_import": ("import",)},
        )
    )
    monkeypatch.setattr(sensor, "_schedule_freshness_expiry", lambda: None)
    await sensor.async_added_to_hass()
    assert not sensor.available
    assert sensor._last_sample_ts is None
    assert sensor._live_flow_sample_count == 0
    await sensor.async_will_remove_from_hass()


@pytest.mark.parametrize("kind", ["grid", "battery"])
@pytest.mark.parametrize(
    "rejection", ["outlier_ignored", "extreme_pending", "source_changed_reseed"]
)
async def test_initial_power_rejection_recovers_through_publication(
    hass, config_entry, coordinator_factory, monkeypatch, kind, rejection
):
    """Availability must process recovery even without a previously accepted power."""
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    power = (EnphaseGridPowerSensor if kind == "grid" else EnphaseBatteryPowerSensor)(
        coord
    )
    flow = next(iter(power._flow_signs))
    value = 1.0
    fields = [flow]

    def publish_payload():
        coord.energy.site_energy = {
            flow: {
                "value_kwh": value,
                "last_report_date": now[0],
                "fields_used": fields,
                "interval_minutes": 5,
            }
        }
        coord.async_update_listeners()

    power.entity_id = "sensor.initially_rejected_" + kind
    power.async_get_last_state = AsyncMock(return_value=None)
    power.async_get_last_extra_data = AsyncMock(return_value=None)
    publish_payload()
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([power])
    now[0] += timedelta(minutes=5)
    if rejection == "source_changed_reseed":
        fields = ["replacement_channel"]
    else:
        value += 1000 if rejection == "extreme_pending" else 10
    publish_payload()
    await hass.async_block_till_done()
    # Once unavailable, HA no longer reads native_value while writing state.
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(power.entity_id).state == "unavailable"
    assert power._last_method == rejection
    assert power._accepted_power_freshness is None
    now[0] += timedelta(minutes=5)
    value += 0.1
    publish_payload()
    await hass.async_block_till_done()
    assert hass.states.get(power.entity_id).state == str(1200 * power._flow_signs[flow])
    assert power.extra_state_attributes["freshness_reference_utc"] == now[0].isoformat()
    assert power._cancel_freshness_expiry is not None
    await component.async_remove_entity(power.entity_id)


@pytest.mark.parametrize("kind", ["grid", "battery"])
@pytest.mark.parametrize("invalid_clock", ["stale", "future"])
async def test_invalid_source_clock_cannot_advance_power_baseline(
    hass, config_entry, coordinator_factory, monkeypatch, kind, invalid_clock
):
    """A rejected payload clock cannot change the next valid dE/dt window."""
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    accepted_at = now[0]
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    power = (EnphaseGridPowerSensor if kind == "grid" else EnphaseBatteryPowerSensor)(
        coord
    )
    flow = next(iter(power._flow_signs))

    def publish_payload(value, timestamp):
        coord.energy.site_energy = {
            flow: {
                "value_kwh": value,
                "last_report_date": timestamp,
                "fields_used": [flow],
                "interval_minutes": 5,
            }
        }
        coord.async_update_listeners()

    power.entity_id = "sensor.invalid_clock_" + kind
    power.async_get_last_state = AsyncMock(return_value=None)
    power.async_get_last_extra_data = AsyncMock(return_value=None)
    publish_payload(1, accepted_at - timedelta(minutes=5))
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([power])
    publish_payload(1.1, accepted_at)
    await hass.async_block_till_done()
    assert hass.states.get(power.entity_id).state == str(1200 * power._flow_signs[flow])

    now[0] += timedelta(minutes=5)
    # A lifetime guard can retain the cumulative value while the source clock
    # regresses. Neither that response nor a future-dated response is usable.
    timestamp = accepted_at + timedelta(minutes=-60 if invalid_clock == "stale" else 60)
    publish_payload(1.1, timestamp)
    await hass.async_block_till_done()
    assert hass.states.get(power.entity_id).state == "unavailable"

    # Recovery can return the last valid sample before the next source advance.
    now[0] += timedelta(minutes=1)
    publish_payload(1.1, accepted_at)
    await hass.async_block_till_done()
    assert hass.states.get(power.entity_id).state == str(1200 * power._flow_signs[flow])
    assert power.extra_state_attributes["freshness_reference_utc"] == (
        accepted_at.isoformat()
    )

    now[0] = accepted_at + timedelta(minutes=10)
    publish_payload(1.2, now[0])
    await hass.async_block_till_done()
    # 100 Wh over ten valid source minutes, not the rejected clock's window.
    assert hass.states.get(power.entity_id).state == str(600 * power._flow_signs[flow])
    assert power.extra_state_attributes["last_window_seconds"] == 600
    await component.async_remove_entity(power.entity_id)
