"""Production Power shares the site lifetime-energy calculation contract."""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import State
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.restore_state import RestoredExtraData
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseBatteryPowerSensor,
    EnphaseCurrentPowerConsumptionSensor,
    EnphaseGridPowerSensor,
)


@pytest.fixture
def production(coordinator_factory, monkeypatch):
    now = [datetime(2026, 11, 4, 19, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    # Retain the inaccurate endpoint measurement to detect any fallback to it.
    coord.current_power_runtime.replace_snapshot(w=248, sample_utc=now[0])
    coord.current_power_runtime.received_utc = now[0]
    sensor = EnphaseCurrentPowerConsumptionSensor(coord)

    def sample(wh, *, seconds=0, source=None, interval=5):
        now[0] += timedelta(seconds=seconds)
        coord.last_success_utc = now[0]
        parsed = coord.energy._aggregate_site_energy(
            {
                "production": [wh],
                "last_report_date": (source or now[0]).timestamp(),
                "interval_minutes": interval,
                "start_date": "2026-01-01",
            }
        )
        assert parsed is not None
        coord.energy.site_energy, coord.energy._site_energy_meta = parsed

    return coord, sensor, now, sample


def test_production_uses_existing_solar_energy_flow(production):
    coord, sensor, now, sample = production
    sample(100_000)
    assert sensor.native_value is None
    sample(100_600, seconds=300)
    assert sensor.native_value == 7200
    attrs = sensor.extra_state_attributes
    assert attrs["source_flows"] == ["solar_production"]
    assert attrs["method"] == "lifetime_energy_window"
    assert attrs["last_window_seconds"] == 300
    assert attrs["sampled_at_utc"] == now[0].isoformat()
    assert sensor.unique_id.endswith("_current_production_power")
    assert coord.current_power_consumption_w == 248


def test_production_waits_for_energy_and_never_falls_back_to_latest_power(production):
    coord, sensor, now, sample = production
    assert sensor.native_value is None
    assert not sensor.available
    sample(100_000)
    assert sensor.native_value is None
    coord.energy.site_energy.clear()
    assert sensor.native_value is None


def test_same_timestamp_energy_correction_is_included_at_next_source_step(production):
    coord, sensor, now, sample = production
    sample(100_000)
    assert sensor.native_value is None
    sample(100_500, seconds=300)
    assert sensor.native_value == 6000
    source = now[0]
    sample(100_840, seconds=60, source=source)
    assert sensor.native_value == 6000
    sample(101_000, seconds=240)
    assert sensor.native_value == 6000
    assert sensor.extra_state_attributes["last_flow_kwh"] == {"solar_production": 101}


def test_reported_interval_bounds_short_report_windows(production):
    coord, sensor, now, sample = production
    sample(100_000, interval=15)
    assert sensor.native_value is None
    sample(100_600, seconds=300, interval=15)
    assert sensor.native_value == 2400
    assert sensor.extra_state_attributes["last_window_seconds"] == 900


def test_production_counter_reset_reseeds_without_power_spike(production):
    coord, sensor, now, sample = production
    sample(100_000)
    assert sensor.native_value is None
    sample(100_500, seconds=300)
    assert sensor.native_value == 6000
    # Use the shared normalized flow boundary to exercise its reset handling,
    # independently of energy acquisition's multi-observation reset guard.
    now[0] += timedelta(minutes=5)
    coord.last_success_utc = now[0]
    coord.energy.site_energy["solar_production"] = {
        "value_kwh": 1,
        "fields_used": ["production"],
        "last_report_date": now[0],
        "interval_minutes": 5,
    }
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["method"] == "lifetime_reset"
    now[0] += timedelta(minutes=5)
    coord.last_success_utc = now[0]
    coord.energy.site_energy["solar_production"].update(
        value_kwh=1.5, last_report_date=now[0]
    )
    assert sensor.native_value == 6000


@pytest.mark.parametrize("invalid", [float("nan"), float("inf")])
@pytest.mark.parametrize("warmed_up", [False, True])
def test_nonfinite_production_bucket_cannot_poison_power(
    production, invalid, warmed_up
):
    coord, sensor, now, sample = production
    if warmed_up:
        sample(100_000)
        assert sensor.native_value is None
        sample(100_500, seconds=300)
        assert sensor.native_value == 6000
    sample(invalid, seconds=60)
    assert sensor.native_value == (6000 if warmed_up else None)
    if not warmed_up:
        # A corrected response may carry the same source timestamp. The invalid
        # placeholder was not a live baseline and must not suppress this seed.
        sample(100_000)
        assert sensor.native_value is None
    sample(101_000 if warmed_up else 100_500, seconds=240 if warmed_up else 300)
    assert sensor.native_value == 6000


@pytest.mark.parametrize("interval", [float("nan"), float("inf")])
def test_nonfinite_interval_uses_existing_default(production, interval):
    coord, sensor, now, sample = production
    sample(100_000, interval=interval)
    assert sensor.native_value is None
    sample(100_600, seconds=300, interval=interval)
    assert sensor.native_value == 7200
    assert sensor.extra_state_attributes["last_window_seconds"] == 300
    assert coord.energy.site_energy["solar_production"].interval_minutes == 5


async def test_production_publishes_and_expires_like_grid_power(
    hass, config_entry, production
):
    coord, sensor, now, sample = production
    sample(100_000)
    assert sensor.native_value is None
    sample(100_500, seconds=300)
    sensor.entity_id = "sensor.energy_production"
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    assert float(hass.states.get(sensor.entity_id).state) == 6000
    accepted = now[0]
    for age in (1799, 1800):
        now[0] = accepted + timedelta(seconds=age)
        coord.last_success_utc = now[0]
        coord.current_power_runtime.received_utc = now[0]
        async_fire_time_changed(hass, now[0])
        await hass.async_block_till_done()
        state = hass.states.get(sensor.entity_id)
        assert (state.state == "unavailable") is (age == 1800)
    await component.async_remove_entity(sensor.entity_id)


@pytest.mark.parametrize(
    ("sensor_class", "bucket"),
    [
        (EnphaseCurrentPowerConsumptionSensor, "production"),
        (EnphaseGridPowerSensor, "import"),
        (EnphaseBatteryPowerSensor, "discharge"),
    ],
)
@pytest.mark.parametrize("warmed_up", [False, True])
async def test_regressed_source_clock_cannot_rebase_power(
    hass,
    config_entry,
    coordinator_factory,
    monkeypatch,
    sensor_class,
    bucket,
    warmed_up,
):
    """An older response must not zero watts or shorten the next valid window."""
    now = [datetime(2026, 11, 4, 19, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    sensor = sensor_class(coord)
    sensor.entity_id = "sensor.regressed_" + bucket

    def publish(wh, timestamp):
        parsed = coord.energy._aggregate_site_energy(
            {
                bucket: [wh],
                "last_report_date": timestamp.timestamp(),
                "interval_minutes": 5,
                "start_date": "2026-01-01",
            }
        )
        assert parsed is not None
        coord.energy.site_energy, coord.energy._site_energy_meta = parsed
        coord.last_success_utc = now[0]
        coord.async_update_listeners()

    publish(100_000, now[0])
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    try:
        if warmed_up:
            now[0] += timedelta(minutes=5)
            publish(100_500, now[0])
            await hass.async_block_till_done()
            assert float(hass.states.get(sensor.entity_id).state) == 6000
        original = hass.states.get(sensor.entity_id)
        accepted_at = now[0]
        now[0] += timedelta(minutes=1)
        # A stale payload can carry the guard's retained counter with an older
        # source time that is still inside the 30-minute availability window.
        publish(100_000, accepted_at - timedelta(minutes=1))
        await hass.async_block_till_done()
        current = hass.states.get(sensor.entity_id)
        assert current.state == original.state
        assert current.attributes["sampled_at_utc"] == accepted_at.isoformat()
        now[0] = accepted_at + timedelta(minutes=5)
        publish(101_000 if warmed_up else 100_500, now[0])
        await hass.async_block_till_done()
        current = hass.states.get(sensor.entity_id)
        assert float(current.state) == 6000
        assert current.attributes["last_window_seconds"] == 300
    finally:
        await component.async_remove_entity(sensor.entity_id)


@pytest.mark.parametrize("method", [[], {}])
async def test_upgrade_ignores_malformed_cached_method(hass, production, method):
    coord, sensor, now, sample = production
    sensor.hass = hass
    sensor.entity_id = "sensor.energy_production"
    sensor.async_get_last_state = AsyncMock(return_value=None)
    sensor.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData(
            {
                "power_history": {
                    "version": 1,
                    "samples": [
                        {
                            "watts": 248,
                            "sampled_ts": now[0].timestamp(),
                            "freshness_ts": now[0].timestamp(),
                            "attributes": {"method": method},
                        }
                    ],
                }
            }
        )
    )
    await sensor._async_restore_power()
    assert not sensor._power_history.samples
    assert sensor.native_value is None


@pytest.mark.parametrize("legacy", [False, True])
async def test_upgrade_discards_latest_power_cache_without_changing_entity(
    hass, config_entry, production, legacy
):
    coord, sensor, now, sample = production
    sensor.entity_id = "sensor.energy_production"
    sensor.async_get_last_state = AsyncMock(
        return_value=State(
            sensor.entity_id,
            "248",
            {
                "source": "app-api:get_latest_power",
                "sampled_at_utc": now[0].isoformat(),
            },
        )
    )
    raw = {"native_value": 248, "native_unit_of_measurement": "W"}
    if not legacy:
        raw["power_history"] = {
            "version": 1,
            "samples": [
                {
                    "watts": 248,
                    "sampled_ts": now[0].timestamp(),
                    "freshness_ts": now[0].timestamp(),
                    "received_ts": now[0].timestamp(),
                    "attributes": {"source": "app-api:get_latest_power"},
                }
            ],
        }
    sensor.async_get_last_extra_data = AsyncMock(return_value=RestoredExtraData(raw))
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    sample(100_000)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state in {"unknown", "unavailable"}
    sample(100_500, seconds=300)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert float(hass.states.get(sensor.entity_id).state) == 6000
    await component.async_remove_entity(sensor.entity_id)
