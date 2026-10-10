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
@pytest.mark.parametrize("value,units", [(0, "W"), (1, "W"), (-1, "W"), (0.001, "kW")])
@pytest.mark.parametrize(
    "failure", ["network", "invalid", "unit", "extreme", "backoff"]
)
async def test_healthy_idle_sample_stays_available_overnight_then_expires(
    hass, coordinator_factory, config_entry, monkeypatch, value, units, failure
):
    """Repeated near-zero measurements renew only their own successful receipt."""
    now = [dt_util.utcnow()]
    sampled_at = now[0] - timedelta(minutes=5)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.last_success_utc = now[0]
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    coord.client.latest_power = AsyncMock(
        return_value={"value": value, "units": units, "time": sampled_at.timestamp()}
    )
    runtime = coord.current_power_runtime
    await runtime.async_refresh()
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    entity.entity_id = "sensor.current_production_power"
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([entity])
    initial_state = hass.states.get(entity.entity_id).state
    assert initial_state != "unavailable"

    # Exercise the expiry timer without coordinator notifications for equal data.
    for _ in range(24):
        now[0] += timedelta(minutes=15)
        coord.last_success_utc = now[0]
        runtime._cache_until_mono = None
        await runtime.async_refresh()
        async_fire_time_changed(hass, now[0])
        await hass.async_block_till_done()
        assert entity.available
        assert hass.states.get(entity.entity_id).state == initial_state
        assert runtime.received_utc == now[0]
        assert entity.extra_state_attributes["sampled_at_utc"] == sampled_at.isoformat()

    if failure == "network":
        coord.client.latest_power.side_effect = TimeoutError()
    elif failure == "backoff":
        monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: False)
    else:
        coord.client.latest_power.return_value = {
            "invalid": {"value": "invalid"},
            "unit": {"value": 0, "units": "unknown"},
            "extreme": {"value": 12_000_000, "time": sampled_at.timestamp()},
        }[failure]
    now[0] += timedelta(minutes=1)
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    now[0] += timedelta(minutes=19)
    coord.last_success_utc = now[0]
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unavailable"
    assert entity.native_value is None
    assert entity._cancel_freshness_expiry is None

    # Identical data must publish recovery and restart the timer after expiry.
    previous_revision = runtime.snapshot.freshness_revision
    coord.client.latest_power.side_effect = None
    coord.client.latest_power.return_value = {
        "value": value,
        "units": units,
        "time": sampled_at.timestamp(),
    }
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.snapshot.freshness_revision == previous_revision + 1
    entity._handle_coordinator_update()
    assert hass.states.get(entity.entity_id).state == initial_state
    assert entity._cancel_freshness_expiry is not None
    await component.async_remove_entity(entity.entity_id)


@pytest.mark.asyncio
async def test_quarter_hour_samples_allow_delivery_delay(
    coordinator_factory, monkeypatch
):
    """A 15-minute sample cadence plus cloud delivery delay does not cause blips."""
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.last_success_utc = now[0]
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    coord.client.latest_power = AsyncMock(
        return_value={"value": 2500, "time": now[0].timestamp()}
    )
    runtime = coord.current_power_runtime
    await runtime.async_refresh()
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    for _ in range(8):
        sampled_at = now[0] + timedelta(minutes=15)
        now[0] = sampled_at + timedelta(seconds=79)
        coord.last_success_utc = now[0]
        runtime._cache_until_mono = None
        await runtime.async_refresh()
        assert entity.available
        coord.client.latest_power.return_value["time"] = sampled_at.timestamp()
        runtime._cache_until_mono = None
        await runtime.async_refresh()
        assert entity.available
    now[0] = sampled_at + timedelta(minutes=20)
    coord.last_success_utc = now[0]
    assert not entity.available


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1.001, -1.001, 2500])
async def test_non_idle_old_sample_still_expires(
    coordinator_factory, monkeypatch, value
):
    now = [dt_util.utcnow()]
    sampled_at = now[0] - timedelta(minutes=21)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.last_success_utc = now[0]
    coord.client.latest_power = AsyncMock(
        return_value={"value": value, "time": sampled_at.timestamp()}
    )
    await coord.current_power_runtime.async_refresh()
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    assert not entity.available
    assert entity.native_value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 2500])
async def test_same_timestamp_value_corrections_are_retained(
    coordinator_factory, monkeypatch, caplog, value
):
    """Without an interval contract, preserve returned watts and log safe evidence."""
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.last_success_utc = now[0]
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    payload = {
        "value": value,
        "time": now[0].timestamp(),
        "units": "W",
        "precision": 0,
        "token": "private-token",
        "site_id": "private-site",
    }
    coord.client.latest_power = AsyncMock(return_value=payload)
    runtime = coord.current_power_runtime
    with caplog.at_level(
        logging.DEBUG, logger="custom_components.enphase_ev.current_power_runtime"
    ):
        await runtime.async_refresh()
        now[0] += timedelta(minutes=5)
        payload["value"] = 3971
        runtime._cache_until_mono = None
        await runtime.async_refresh()
    assert runtime.snapshot.w == 3971
    assert runtime.snapshot.sample_utc.timestamp() == payload["time"]
    assert "value_w=3971.0" in caplog.text
    assert "received_at_utc=" in caplog.text
    assert "private-token" not in caplog.text
    assert "private-site" not in caplog.text


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
    now[0] += timedelta(seconds=1131)
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
    now[0] += timedelta(minutes=20)
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
    now[0] += timedelta(seconds=1101)
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
@pytest.mark.parametrize("value", [0, 100])
async def test_identical_sample_recovers_after_expiry(
    hass, coordinator_factory, config_entry, monkeypatch, value
):
    """Resuming a paused source publishes recovery even without a value change."""
    from custom_components.enphase_ev.integration_snapshot import CoordinatorData

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.update_interval = None
    coord.always_update = False
    coord.last_success_utc = now[0]
    coord.client.latest_power = AsyncMock(
        return_value={
            "value": value,
            "time": now[0].timestamp() if value == 0 else None,
        }
    )
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    await coord.current_power_runtime.async_refresh()
    coord.async_set_updated_data(dict(coord.data))
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    entity.entity_id = "sensor.current_production_power"
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([entity])
    assert hass.states.get(entity.entity_id).state == str(value)
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
    now[0] += timedelta(minutes=21)
    coord.last_success_utc = now[0]
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unavailable"
    assert entity._cancel_freshness_expiry is None
    coord.current_power_runtime._cache_until_mono = None
    await coord.current_power_runtime.async_refresh()
    await coord.async_refresh()
    assert hass.states.get(entity.entity_id).state == str(value)
    listener.assert_called_once()
    assert entity._cancel_freshness_expiry is not None
    remove_listener()
    await component.async_remove_entity(entity.entity_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 2500])
async def test_repeated_future_source_timestamp_keeps_original_receipt_bound(
    coordinator_factory, monkeypatch, value
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.last_success_utc = now[0]
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    coord.client.latest_power = AsyncMock(
        return_value={"value": value, "time": (now[0] + timedelta(days=1)).timestamp()}
    )
    runtime = coord.current_power_runtime
    await runtime.async_refresh()
    received = runtime.received_utc
    entity = EnphaseCurrentPowerConsumptionSensor(coord)
    assert entity.available
    for minutes in (5, 16):
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
