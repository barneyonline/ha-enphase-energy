"""Persistent UTC telemetry cooldown and inventory-visible polling status."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.const import OPT_MICROINVERTER_POWER_ENABLED
from custom_components.enphase_ev.inverter_telemetry_cooldown import (
    FAMILY,
    _store,
    async_restore_inverter_telemetry_cooldown,
    async_save_inverter_telemetry_cooldown,
    inverter_telemetry_status_attributes,
)
from custom_components.enphase_ev.sensor_inverter import (
    EnphaseMicroinverterConnectivityStatusSensor,
)


async def test_saved_cooldown_restores_before_poll_across_new_coordinator(
    coordinator_factory,
):
    coord = coordinator_factory()
    health = coord._endpoint_family_state(FAMILY)
    health.last_status = 429
    health.cooldown_active = True
    health.next_retry_utc = dt_util.utcnow() + timedelta(hours=1)
    deadline = health.next_retry_utc
    await async_save_inverter_telemetry_cooldown(coord)
    restored = coordinator_factory()
    await async_restore_inverter_telemetry_cooldown(restored)
    state = restored._endpoint_family_state(FAMILY)
    assert state.next_retry_utc == deadline
    assert state.last_status == 429
    assert restored._endpoint_family_wait_active(FAMILY)
    assert not restored._endpoint_family_should_run(FAMILY)
    await async_restore_inverter_telemetry_cooldown(restored)
    assert state.next_retry_utc == deadline
    assert _store(coord) is _store(coord)


@pytest.mark.parametrize(
    "saved",
    [
        None,
        {},
        {"next_retry_utc": 1},
        {"next_retry_utc": "nonsense"},
        {"next_retry_utc": "2026-01-01T00:00:00"},
        {"next_retry_utc": "2020-01-01T00:00:00+00:00"},
    ],
)
async def test_invalid_or_expired_saved_cooldown_is_ignored(coordinator_factory, saved):
    coord = coordinator_factory()
    coord.__dict__["_inverter_telemetry_cooldown_store"] = SimpleNamespace(
        async_load=AsyncMock(return_value=saved)
    )
    await async_restore_inverter_telemetry_cooldown(coord)
    assert not coord._endpoint_family_wait_active(FAMILY)


async def test_restore_does_not_shorten_current_cooldown(coordinator_factory):
    coord = coordinator_factory()
    deadline = dt_util.utcnow() + timedelta(hours=2)
    coord._endpoint_family_state(FAMILY).next_retry_utc = deadline
    coord.__dict__["_inverter_telemetry_cooldown_store"] = SimpleNamespace(
        async_load=AsyncMock(
            return_value={"next_retry_utc": (deadline - timedelta(hours=1)).isoformat()}
        )
    )
    await async_restore_inverter_telemetry_cooldown(coord)
    assert coord._endpoint_family_state(FAMILY).next_retry_utc == deadline


async def test_storage_failures_do_not_break_other_endpoints(coordinator_factory):
    coord = coordinator_factory()
    coord.__dict__["_inverter_telemetry_cooldown_store"] = SimpleNamespace(
        async_load=AsyncMock(side_effect=OSError()),
        async_save=AsyncMock(side_effect=OSError()),
    )
    await async_restore_inverter_telemetry_cooldown(coord)
    await async_save_inverter_telemetry_cooldown(coord)
    health = coord._endpoint_family_state(FAMILY)
    health.last_status = 429
    health.cooldown_active = True
    health.next_retry_utc = dt_util.utcnow() + timedelta(hours=1)
    await async_save_inverter_telemetry_cooldown(coord)
    assert health.cooldown_active


def test_polling_status_available_before_power_entities_exist(coordinator_factory):
    coord = coordinator_factory()
    coord.config_entry = SimpleNamespace(
        options={OPT_MICROINVERTER_POWER_ENABLED: True}
    )
    sensor = EnphaseMicroinverterConnectivityStatusSensor(coord)
    assert sensor.extra_state_attributes["power_telemetry_status"] == "pending"
    health = coord._endpoint_family_state(FAMILY)
    health.last_status = 429
    health.cooldown_active = True
    health.next_retry_utc = dt_util.utcnow() + timedelta(hours=1)
    attrs = sensor.extra_state_attributes
    assert attrs["power_telemetry_status"] == "rate_limited"
    assert attrs["power_telemetry_next_retry"] == health.next_retry_utc.isoformat()
    health.next_retry_utc = dt_util.utcnow() - timedelta(seconds=1)
    health.last_success_utc = dt_util.utcnow()
    # Successful diagnostic reads cannot imply that power data is available.
    coord._inverter_parameter_telemetry = {"INV-1": {"temperature": 20}}
    assert (
        inverter_telemetry_status_attributes(coord)["power_telemetry_status"]
        == "pending"
    )
    import time

    coord._inverter_parameter_telemetry = {"INV-1": {"power": 100}}
    coord._inverter_parameter_success_mono = {
        "INV-1": {"power": time.monotonic() - 3600}
    }
    assert (
        inverter_telemetry_status_attributes(coord)["power_telemetry_status"]
        == "pending"
    )
    coord._inverter_parameter_success_mono = {"INV-1": {"power": time.monotonic()}}
    assert (
        inverter_telemetry_status_attributes(coord)["power_telemetry_status"] == "ready"
    )
    coord.config_entry.options = {}
    assert (
        inverter_telemetry_status_attributes(coord)["power_telemetry_status"] == "ready"
    )
    coord._inverter_parameter_telemetry = {}
    assert (
        inverter_telemetry_status_attributes(coord)["power_telemetry_status"]
        == "pending"
    )


async def test_concurrent_restoration_waits_for_durable_deadline(coordinator_factory):
    import asyncio

    coord = coordinator_factory()
    reading = asyncio.Event()
    release = asyncio.Event()
    deadline = dt_util.utcnow() + timedelta(hours=1)

    async def load():
        reading.set()
        await release.wait()
        return {"next_retry_utc": deadline.isoformat()}

    store = SimpleNamespace(async_load=AsyncMock(side_effect=load))
    coord.__dict__["_inverter_telemetry_cooldown_store"] = store
    first = asyncio.create_task(async_restore_inverter_telemetry_cooldown(coord))
    await reading.wait()
    second = asyncio.create_task(async_restore_inverter_telemetry_cooldown(coord))
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    store.async_load.assert_awaited_once()
    assert coord._endpoint_family_wait_active(FAMILY)
