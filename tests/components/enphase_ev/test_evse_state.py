"""Verify runtime-owned charger state and cancellation at the request boundary."""

import asyncio
import inspect

import pytest

from custom_components.enphase_ev.evse_state import EVSEState


def test_runtime_owns_state_and_publishes_detached_controls(coordinator_factory):
    coord = coordinator_factory()
    runtime = coord.evse_runtime
    assert coord.evse_state is runtime.state
    runtime.set_desired_charging("one", True)
    runtime.state._pending_charging["one"] = (True, 1.0)
    runtime.state._charge_mode_cache["one"] = ("MANUAL_CHARGING", 1.0)
    runtime.state._green_battery_cache["one"] = (True, True, 1.0)
    runtime.state._auth_settings_cache["one"] = (True, False, True, True, 1.0)
    first = runtime.snapshot
    runtime.state._charge_mode_cache["one"] = ("MANUAL_CHARGING", 2.0)
    assert runtime.snapshot == first
    runtime.set_desired_charging("one", False)
    assert first.desired_charging["one"] is True
    assert runtime.snapshot.desired_charging["one"] is False
    assert runtime.snapshot != first
    with pytest.raises(TypeError):
        first.desired_charging["one"] = False
    assert coord._desired_charging is runtime.state._desired_charging


def test_prune_removes_inactive_serials_from_every_cache():
    state = EVSEState()
    # Pruning must cover derived power/transition histories as well as commands.
    for name in state.__slots__:
        cache = getattr(state, name)
        cache.update({"active": None, "removed": None})
    state.prune({"active"})
    assert all(set(getattr(state, name)) == {"active"} for name in state.__slots__)


async def test_cancelled_lookup_closes_requests_waiting_for_capacity(
    coordinator_factory,
):
    runtime = coordinator_factory().evse_runtime
    runtime._lookup_semaphore = asyncio.Semaphore(1)
    entered = asyncio.Event()

    async def request():
        entered.set()
        await asyncio.Event().wait()

    first, queued = request(), request()
    task = asyncio.create_task(runtime._run_lookup_tasks({"one": first, "two": queued}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inspect.getcoroutinestate(first) == inspect.CORO_CLOSED
    assert inspect.getcoroutinestate(queued) == inspect.CORO_CLOSED


async def test_lookup_can_share_future_results(coordinator_factory):
    future = asyncio.get_running_loop().create_future()
    future.set_result("value")
    assert await coordinator_factory().evse_runtime._run_lookup_tasks(
        {"one": future}
    ) == {"one": "value"}
