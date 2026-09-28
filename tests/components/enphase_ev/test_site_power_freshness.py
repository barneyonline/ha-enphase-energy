"""Source-bound site power and unchanged family recovery regressions."""

import logging
from datetime import timedelta
from unittest.mock import Mock
import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from custom_components.enphase_ev.sensor_base import EnphaseSiteSensorEntity
from custom_components.enphase_ev.integration_snapshot import CoordinatorData


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["battery_status", "heatpump_power"])
@pytest.mark.parametrize("first_success", [False, True])
async def test_family_equal_data_recovers_after_expiry(
    hass, coordinator_factory, config_entry, monkeypatch, family, first_success
):
    """Freshness recovery is semantic; routine equal samples stay suppressed."""
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor_base.inventory_type_available",
        lambda *_args: True,
    )
    coord = coordinator_factory()
    coord.update_interval = None
    coord.always_update = False
    coord.last_success_utc = now[0]

    def succeed():
        if family == "battery_status":
            coord.note_endpoint_family_success(family)
        else:
            coord.heatpump_state._heatpump_power_last_success_utc = now[0]

    if not first_success:
        succeed()
    coord.async_set_updated_data(dict(coord.data))
    entity = EnphaseSiteSensorEntity(
        coord,
        "family_power",
        "Family",
        "encharge" if family == "battery_status" else "heatpump",
    )
    entity._attr_device_class = SensorDeviceClass.POWER
    entity._attr_native_value = 75
    entity._attr_native_unit_of_measurement = "W"
    entity.entity_id = "sensor.family_power"
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([entity])
    coord.async_set_updated_data(dict(coord.data))
    assert hass.states.get(entity.entity_id).state == "75"

    async def update():
        return CoordinatorData(
            dict(coord.data), coord._build_integration_snapshot(coord.data)
        )

    monkeypatch.setattr(coord, "_async_update_data", update)
    listener = Mock()
    remove_listener = coord.async_add_listener(listener)
    now[0] += timedelta(seconds=60)
    if not first_success:
        succeed()
    await coord.async_refresh()
    listener.assert_not_called()
    now[0] += timedelta(minutes=61)
    coord.last_success_utc = now[0]
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unavailable"
    succeed()
    await coord.async_refresh()
    assert entity.available
    assert hass.states.get(entity.entity_id).state == "75"
    listener.assert_called_once()
    # Capturing the same success again cannot increment the recovery revision.
    await coord.async_refresh()
    listener.assert_called_once()
    assert entity._cancel_freshness_expiry is not None
    remove_listener()
    await component.async_remove_entity(entity.entity_id)
    assert entity._cancel_freshness_expiry is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grid", "battery"])
async def test_lifetime_power_expires_and_recovers_with_healthy_core(
    hass, coordinator_factory, config_entry, monkeypatch, kind
):
    from custom_components.enphase_ev.sensor_site_energy import (
        EnphaseGridPowerSensor,
        EnphaseBatteryPowerSensor,
    )
    from custom_components.enphase_ev.energy import SiteEnergyFlow

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    cls, key = (
        (EnphaseGridPowerSensor, "grid_import")
        if kind == "grid"
        else (EnphaseBatteryPowerSensor, "battery_discharge")
    )
    entity = cls(coord)
    entity.entity_id = "sensor.review_" + kind

    def flow(value):
        return SiteEnergyFlow(
            value_kwh=value,
            bucket_count=1,
            fields_used=["import"],
            start_date=now[0].date().isoformat(),
            last_report_date=now[0],
            update_pending=False,
            source_unit="Wh",
            last_reset_at=None,
            interval_minutes=5,
        )

    coord.energy.site_energy = {key: flow(1)}
    assert entity.native_value is None
    now[0] += timedelta(minutes=5)
    coord.last_success_utc = now[0]
    coord.energy.site_energy[key] = flow(1.5)
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([entity])
    assert hass.states.get(entity.entity_id).state == "6000"
    now[0] += timedelta(days=1)
    coord.last_success_utc = now[0]
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    coord.async_set_updated_data(dict(coord.data))
    assert hass.states.get(entity.entity_id).state == "unavailable"
    assert not entity.available
    assert entity._cancel_freshness_expiry is None
    now[0] += timedelta(minutes=5)
    coord.energy.site_energy[key] = flow(2)
    coord.async_set_updated_data(dict(coord.data))
    assert entity.available
    assert hass.states.get(entity.entity_id).state != "unavailable"
    assert entity._cancel_freshness_expiry is not None
    await component.async_remove_entity(entity.entity_id)


@pytest.mark.parametrize("clock", [None, "future", "infinite"])
def test_lifetime_power_missing_or_invalid_clock_is_bounded(
    coordinator_factory, monkeypatch, clock
):
    from custom_components.enphase_ev.sensor_site_energy import EnphaseGridPowerSensor

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.last_success_utc = now[0]
    source = {
        None: None,
        "future": now[0] + timedelta(days=1),
        "infinite": float("inf"),
    }[clock]
    coord.energy.site_energy = {
        "grid_import": {"value_kwh": 1, "last_report_date": source}
    }
    entity = EnphaseGridPowerSensor(coord)
    assert entity.available is (clock is None)
    now[0] += timedelta(minutes=16)
    coord.last_success_utc = now[0]
    assert not entity.available
    # Restarted/restored history cannot renew the fallback grace period.
    restored = EnphaseGridPowerSensor(coord)
    restored._last_sample_ts = (now[0] - timedelta(hours=1)).timestamp()
    assert not restored.available


def test_lifetime_power_oldest_source_metadata_and_core_deadlines(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor_site_energy import EnphaseGridPowerSensor

    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    coord = coordinator_factory()
    coord.last_success_utc = now
    coord.energy.site_energy = {
        "grid_import": {"value_kwh": 1, "last_report_date": now},
        "grid_export": {
            "value_kwh": 1,
            "last_report_date": now - timedelta(minutes=16),
        },
    }
    entity = EnphaseGridPowerSensor(coord)
    assert not entity.available
    # A zero supported channel still belongs to the timestamped source family.
    coord.energy.site_energy = {}
    coord.energy._site_energy_meta = {
        "bucket_lengths": {"import": 1},
        "last_report_date": now - timedelta(minutes=16),
    }
    assert not entity.available
    coord.energy._site_energy_meta["last_report_date"] = now
    assert entity.available
    # The source deadline cannot extend the existing core-outage grace period.
    coord.last_update_success = False
    coord.last_success_utc = now - timedelta(minutes=14)
    assert entity._freshness_deadline() == now + timedelta(minutes=1)


@pytest.mark.asyncio
async def test_lifetime_power_restore_cannot_restart_missing_clock_grace(
    hass, coordinator_factory, config_entry, monkeypatch
):
    from homeassistant.core import State
    from unittest.mock import AsyncMock
    from custom_components.enphase_ev.sensor_site_energy import EnphaseGridPowerSensor

    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now
    coord.energy.site_energy = {"grid_import": {"value_kwh": 1.5}}
    entity = EnphaseGridPowerSensor(coord)
    entity.entity_id = "sensor.restored_grid_power"
    monkeypatch.setattr(
        entity,
        "async_get_last_state",
        AsyncMock(
            return_value=State(
                entity.entity_id,
                "6000",
                {
                    "last_flow_kwh": {"grid_import": 1.5},
                    "last_sample_ts": (now - timedelta(hours=1)).timestamp(),
                    "last_energy_ts": (now - timedelta(hours=1)).timestamp(),
                    "method": "lifetime_energy_window",
                },
            )
        ),
    )
    monkeypatch.setattr(
        entity, "async_get_last_extra_data", AsyncMock(return_value=None)
    )
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([entity])
    assert hass.states.get(entity.entity_id).state == "unavailable"
    assert entity._cancel_freshness_expiry is None
    await component.async_remove_entity(entity.entity_id)
