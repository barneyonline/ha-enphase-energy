"""Current production power uses its own bounded source freshness."""

import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity_component import EntityComponent
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.enphase_ev.current_power_runtime import CurrentPowerRuntime
from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseCurrentPowerConsumptionSensor,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["failure", "unchanged"])
async def test_current_power_expires_with_healthy_core_and_recovers(
    hass, coordinator_factory, config_entry, monkeypatch, response
):
    """Failed or repeated endpoint samples expire even while core remains healthy."""
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    coord.client.latest_power = AsyncMock(
        return_value={"value": 2500, "time": now[0].timestamp(), "units": "W"}
    )
    runtime = coord.current_power_runtime
    await runtime.async_refresh()
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    entity.hass = hass
    entity.entity_id = "sensor.current_production_power"
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([entity])
    entity.async_write_ha_state()
    assert hass.states.get(entity.entity_id).state == "2500"

    now[0] += timedelta(seconds=70)
    coord.last_success_utc = now[0]
    runtime._cache_until_mono = None
    if response == "failure":
        coord.client.latest_power.side_effect = TimeoutError()
    await runtime.async_refresh()
    # Equal data may suppress coordinator listeners: the entity's own timer
    # still publishes expiry, based on the original source timestamp.
    now[0] += timedelta(seconds=831)
    coord.last_success_utc = now[0]
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unavailable"
    assert entity.native_value is None
    assert entity._cancel_freshness_expiry is None

    coord.client.latest_power.side_effect = None
    coord.client.latest_power.return_value = {
        "value": 2400,
        "time": now[0].timestamp(),
    }
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    entity._handle_coordinator_update()
    assert hass.states.get(entity.entity_id).state == "2400"
    assert entity._cancel_freshness_expiry is not None
    await component.async_remove_entity(entity.entity_id)
    assert entity._cancel_freshness_expiry is None


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp", ["old", "missing", "future"])
async def test_source_clock_and_missing_timestamp_cannot_extend_failed_sample(
    coordinator_factory, monkeypatch, timestamp
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = timedelta(minutes=1)
    coord.last_success_utc = now[0]
    sample_time = {
        "old": (now[0] - timedelta(days=1)).timestamp(),
        "missing": None,
        "future": (now[0] + timedelta(days=1)).timestamp(),
    }[timestamp]
    coord.client.latest_power = AsyncMock(
        return_value={"value": 2500, "time": sample_time}
    )
    await coord.current_power_runtime.async_refresh()
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    assert entity.available is (timestamp != "old")
    now[0] += timedelta(minutes=15)
    coord.last_success_utc = now[0]
    assert not entity.available
    assert entity.native_value is None
    # Reading the retained runtime value repeatedly cannot renew the cache.
    now[0] += timedelta(minutes=1)
    coord.last_success_utc = now[0]
    assert not entity.available


@pytest.mark.asyncio
async def test_missing_source_timestamp_refresh_advances_timer_without_notification(
    hass, coordinator_factory, config_entry, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    coord.client.latest_power = AsyncMock(return_value={"value": 100})
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    await coord.current_power_runtime.async_refresh()
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    entity.hass = hass
    entity.entity_id = "sensor.current_production_power"
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([entity])
    now[0] += timedelta(seconds=100)
    coord.current_power_runtime._cache_until_mono = None
    await coord.current_power_runtime.async_refresh()
    now[0] += timedelta(seconds=801)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "100"
    assert entity._cancel_freshness_expiry is not None
    await component.async_remove_entity(entity.entity_id)


@pytest.mark.asyncio
async def test_runtime_uses_only_public_host_services(monkeypatch):
    """A minimal host can own the runtime without a coordinator instance."""
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    host = SimpleNamespace(
        site_id="site-test",
        client=SimpleNamespace(latest_power=AsyncMock(return_value={"value": 42})),
        endpoint_family_should_run=Mock(return_value=True),
        note_endpoint_family_success=Mock(),
        note_endpoint_family_failure=Mock(return_value=True),
    )
    runtime = CurrentPowerRuntime(host)
    assert runtime.refresh_due()
    await runtime.async_refresh()
    assert runtime.snapshot.w == 42
    assert runtime.received_utc == now
    host.note_endpoint_family_success.assert_called_once_with("current_power")
    runtime._cache_until_mono = None
    host.client.latest_power.side_effect = TimeoutError()
    await runtime.async_refresh()
    host.note_endpoint_family_failure.assert_called_once()
    assert runtime.received_utc == now
    runtime.clear()
    assert runtime.received_utc is None


@pytest.mark.asyncio
async def test_identical_missing_timestamp_sample_recovers_after_expiry(
    hass, coordinator_factory, config_entry, monkeypatch
):
    """Resuming a paused source publishes recovery even without a value change."""
    from custom_components.enphase_ev.integration_snapshot import CoordinatorData

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.always_update = False
    coord.last_success_utc = now[0]
    coord.client.latest_power = AsyncMock(return_value={"value": 100})
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    await coord.current_power_runtime.async_refresh()
    coord.async_set_updated_data(dict(coord.data))
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    entity.entity_id = "sensor.current_production_power"
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([entity])
    assert hass.states.get(entity.entity_id).state == "100"
    coord.async_set_updated_data(dict(coord.data))
    listener = Mock()
    remove_listener = coord.async_add_listener(listener)

    async def refreshed_data():
        return CoordinatorData(
            dict(coord.data), coord._build_integration_snapshot(coord.data)
        )

    monkeypatch.setattr(coord, "_async_update_data", refreshed_data)
    # Ordinary equal accepted samples should still be suppressed.
    now[0] += timedelta(seconds=60)
    coord.current_power_runtime._cache_until_mono = None
    await coord.current_power_runtime.async_refresh()
    await coord.async_refresh()
    listener.assert_not_called()
    # A skipped endpoint can expire without a failure changing aggregate health.
    now[0] += timedelta(minutes=16)
    coord.last_success_utc = now[0]
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unavailable"
    assert entity._cancel_freshness_expiry is None
    coord.current_power_runtime._cache_until_mono = None
    await coord.current_power_runtime.async_refresh()
    await coord.async_refresh()
    assert hass.states.get(entity.entity_id).state == "100"
    listener.assert_called_once()
    assert entity._cancel_freshness_expiry is not None
    remove_listener()
    await component.async_remove_entity(entity.entity_id)


@pytest.mark.asyncio
async def test_repeated_future_source_timestamp_keeps_original_receipt_bound(
    coordinator_factory, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.last_success_utc = now[0]
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    coord.client.latest_power = AsyncMock(
        return_value={"value": 2500, "time": (now[0] + timedelta(days=1)).timestamp()}
    )
    runtime = coord.current_power_runtime
    await runtime.async_refresh()
    received = runtime.received_utc
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    assert entity.available
    for minutes in (5, 11):
        now[0] += timedelta(minutes=minutes)
        coord.last_success_utc = now[0]
        runtime._cache_until_mono = None
        await runtime.async_refresh()
        assert runtime.received_utc == received
    assert not entity.available
    coord.client.latest_power.return_value["time"] = now[0].timestamp()
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert entity.available
    # Compatibility setters may seed a snapshot without a receipt timestamp.
    runtime.received_utc = None
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.received_utc == now[0]
