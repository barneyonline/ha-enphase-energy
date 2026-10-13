"""Production does not publish premature samples or renew accepted freshness."""

import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorExtraStoredData
from homeassistant.core import State
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseCurrentPowerConsumptionSensor,
)


@pytest.mark.parametrize(
    "received_time,sample_time,value,later_value",
    [
        ("11:36:38", "11:40:00", 17, 329),
        ("12:22:04", "12:25:00", 53, 561),
        ("14:37:07", "14:40:00", 11, 1386),
    ],
)
async def test_reported_future_samples_preserve_previous_valid_reading(
    coordinator_factory,
    monkeypatch,
    caplog,
    received_time,
    sample_time,
    value,
    later_value,
):
    zone = ZoneInfo("America/Los_Angeles")
    received = datetime.fromisoformat("2026-10-11T" + received_time).replace(
        tzinfo=zone
    )
    sampled = datetime.fromisoformat("2026-10-11T" + sample_time).replace(tzinfo=zone)
    now = [received.astimezone(ZoneInfo("UTC")) - timedelta(minutes=1)]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    coord.client.latest_power = AsyncMock(
        return_value={
            "value": 2500,
            "time": now[0].timestamp(),
            "units": "W",
            "precision": 0,
        }
    )
    runtime = coord.current_power_runtime
    await runtime.async_refresh()
    accepted = runtime.snapshot
    accepted_receipt = runtime.received_utc
    now[0] = received.astimezone(ZoneInfo("UTC"))
    coord.client.latest_power.return_value = {
        "value": value,
        "time": sampled.timestamp(),
        "units": "W",
        "precision": 0,
        "token": "private-token",
        "site_id": "private-site",
    }
    runtime._cache_until_mono = None
    with caplog.at_level(
        logging.DEBUG, logger="custom_components.enphase_ev.current_power_runtime"
    ):
        await runtime.async_refresh()
    assert runtime.snapshot == accepted
    assert runtime.received_utc == accepted_receipt
    diagnostics = runtime.diagnostics()
    assert diagnostics["validation_state"] == "future_timestamp"
    assert diagnostics["validation_reason"] == "sample_timestamp_in_future"
    assert diagnostics["last_observed_value"] == value
    assert diagnostics["using_stale"] is True
    assert "validation_state=future_timestamp" in caplog.text
    assert "private-token" not in caplog.text
    assert "private-site" not in caplog.text
    assert coord._endpoint_family_state("current_power").consecutive_failures == 0
    # Re-fetch the revised value at the same source timestamp once time advances.
    now[0] = sampled.astimezone(ZoneInfo("UTC")) + timedelta(seconds=80)
    coord.client.latest_power.return_value["value"] = later_value
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.snapshot.w == later_value
    assert runtime.snapshot.sample_utc.timestamp() == sampled.timestamp()
    assert runtime.received_utc == now[0]
    assert runtime.diagnostics()["validation_state"] == "accepted"


@pytest.mark.parametrize(
    "offset,accepted", [(-60, True), (0, True), (60, True), (61, False), (210, False)]
)
async def test_future_clock_tolerance_without_previous_reading(
    coordinator_factory, monkeypatch, offset, accepted
):
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    coord = coordinator_factory()
    coord.client.latest_power = AsyncMock(
        return_value={
            "value": 2500,
            "time": (now + timedelta(seconds=offset)).timestamp(),
        }
    )
    await coord.current_power_runtime.async_refresh()
    assert (coord.current_power_runtime.snapshot.w is not None) is accepted
    assert coord.current_power_runtime.received_utc == (now if accepted else None)


@pytest.mark.parametrize("sample_offset,cached_offset", [(210, 0), (-120, -300)])
async def test_premature_restored_sample_is_not_published(
    hass, config_entry, coordinator_factory, monkeypatch, sample_offset, cached_offset
):
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    entity.entity_id = "sensor.restored_premature_production"
    entity.async_get_last_sensor_data = AsyncMock(
        return_value=SensorExtraStoredData(17, "W")
    )
    entity.async_get_last_state = AsyncMock(
        return_value=State(
            entity.entity_id,
            "17",
            {
                "sampled_at_utc": (now + timedelta(seconds=sample_offset)).isoformat(),
                "cached_at_utc": (now + timedelta(seconds=cached_offset)).isoformat(),
            },
        )
    )
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([entity])
    assert hass.states.get(entity.entity_id).state == "unavailable"
    assert entity.native_value is None
    assert entity._cancel_freshness_expiry is None
    await component.async_remove_entity(entity.entity_id)


async def test_future_polls_cannot_renew_power_expiry(
    hass, config_entry, coordinator_factory, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    coord.client.latest_power = AsyncMock(
        return_value={"value": 2500, "time": now[0].timestamp()}
    )
    runtime = coord.current_power_runtime
    await runtime.async_refresh()
    accepted_at = now[0]
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    entity.entity_id = "sensor.future_poll_production"
    entity.async_get_last_sensor_data = AsyncMock(return_value=None)
    entity.async_get_last_state = AsyncMock(return_value=None)
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([entity])
    for age in (60, 1799, 1800):
        now[0] = accepted_at + timedelta(seconds=age)
        coord.last_success_utc = now[0]
        coord.client.latest_power.return_value = {
            "value": 0,
            "time": (now[0] + timedelta(seconds=210)).timestamp(),
        }
        runtime._cache_until_mono = None
        await runtime.async_refresh()
        async_fire_time_changed(hass, now[0])
        await hass.async_block_till_done()
        assert hass.states.get(entity.entity_id).state == (
            "2500" if age < 1800 else "unavailable"
        )
        assert runtime.received_utc == accepted_at
    await component.async_remove_entity(entity.entity_id)


async def test_future_sample_cannot_change_extreme_confirmation(
    coordinator_factory, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    runtime = coord.current_power_runtime
    coord.client.latest_power = AsyncMock(
        return_value={"value": 12_000_000, "time": now[0].timestamp()}
    )
    await runtime.async_refresh()
    assert runtime.diagnostics()["pending_extreme_count"] == 1
    now[0] += timedelta(seconds=60)
    coord.client.latest_power.return_value = {
        "value": 13_000_000,
        "time": (now[0] + timedelta(seconds=210)).timestamp(),
    }
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.snapshot.w is None
    assert runtime.diagnostics()["validation_state"] == "future_timestamp"
    assert runtime.diagnostics()["pending_extreme_count"] == 1
    # A normal premature reading must not clear the pending confirmation either.
    coord.client.latest_power.return_value["value"] = 17
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.diagnostics()["pending_extreme_count"] == 1
    # A genuinely newer, nonfuture sample can confirm the original extreme.
    coord.client.latest_power.return_value = {
        "value": 13_000_000,
        "time": now[0].timestamp(),
    }
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.snapshot.w == 13_000_000
    assert runtime.diagnostics()["validation_state"] == "confirmed_extreme"
