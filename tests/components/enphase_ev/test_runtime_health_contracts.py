"""Runtime health services preserve entry isolation and cooldown semantics."""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.endpoint_policies import (
    build_endpoint_family_policies,
)


def test_runtime_health_services_isolate_sites_and_allow_explicit_refresh(
    coordinator_factory,
):
    first = coordinator_factory()
    second = coordinator_factory()

    assert first.endpoint_family_should_run("current_power")
    assert first.note_endpoint_family_failure("current_power", TimeoutError())
    assert not first.endpoint_family_should_run("current_power")
    assert first.endpoint_family_should_run("current_power", force=True)
    assert second.endpoint_family_should_run("current_power")
    assert first.endpoint_family_last_success_utc("current_power") is None

    first.note_endpoint_family_success("current_power", success_ttl_s=0)
    assert first.endpoint_family_should_run("current_power")
    assert first.endpoint_family_last_success_utc("current_power") is not None
    assert second.endpoint_family_last_success_utc("current_power") is None

    first.note_endpoint_family_success("current_power")
    assert not first.endpoint_family_should_run("current_power")
    assert first.endpoint_family_should_run("current_power", force=True)
    assert not first.note_endpoint_family_failure("unknown", TimeoutError())
    assert first.endpoint_family_should_run("unknown")


def test_endpoint_policy_configuration_is_immutable_and_entry_local():
    first = build_endpoint_family_policies()
    second = build_endpoint_family_policies()
    assert first["current_power"].stale_after_s == 1200
    with pytest.raises(FrozenInstanceError):
        first["current_power"].stale_after_s = 0
    first.pop("current_power")
    assert "current_power" in second


async def test_reload_quiesce_invalidates_restart_before_waiting_for_refresh(
    coordinator_factory, monkeypatch
):
    """A queued command cannot fire while reload waits for a slow core refresh."""
    coord = coordinator_factory(serials=["EV1"])
    coord.data = {"EV1": {"plugged": True, "charging": False}}
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.start_charging = AsyncMock()
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    delayed = asyncio.Event()
    cancelled = asyncio.Event()

    async def suppress_cancellation(_seconds):
        delayed.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()

    monkeypatch.setattr(
        "custom_components.enphase_ev.evse_runtime.asyncio",
        SimpleNamespace(**(vars(asyncio) | {"sleep": suppress_cancellation})),
    )
    coord.schedule_amp_restart("EV1")
    restart = coord._amp_restart_tasks["EV1"]
    await asyncio.wait_for(delayed.wait(), 1)
    async with coord._refresh_lock:
        quiesce = asyncio.create_task(coord.async_quiesce_for_reload())
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(restart, 1)
        assert not quiesce.done()
        coord.client.start_charging.assert_not_awaited()
    assert await asyncio.wait_for(quiesce, 1)
    assert coord._amp_restart_tasks == {}
