"""Storm Alert opt-in targets fresh, unexpired opted-out advisories."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, call

import aiohttp
import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util


@pytest.fixture
def storm_opt_in(coordinator_factory):
    coord = coordinator_factory()
    end = (dt_util.utcnow() + timedelta(hours=2)).timestamp() * 1000
    alert = {
        "id": "alert-a",
        "name": "Heat Advisory",
        "status": "opted-out",
        "endTime": end,
    }
    coord.client.storm_guard_alert = AsyncMock(return_value={"stormAlerts": [alert]})
    coord.client.opt_in_storm_alert = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    yield coord, alert
    coord.control_updates.cleanup()


async def test_opt_in_targets_only_fresh_eligible_alerts_and_confirms_ids(storm_opt_in):
    coord, alert = storm_opt_in
    alerts = [
        alert,
        dict(alert),
        {**alert, "id": "expired", "endTime": 1},
        {**alert, "id": "active", "status": "active"},
        {**alert, "id": "no-expiry", "endTime": None},
        {**alert, "id": "unknown", "status": "inactive"},
        {**alert, "id": None},
        {**alert, "id": "unnamed", "name": None},
    ]
    coord.client.storm_guard_alert.side_effect = [
        {"stormAlerts": alerts},
        {
            "stormAlerts": [
                {**alert, "status": "active"},
                {**alert, "id": "unnamed", "status": "active"},
            ]
        },
    ]
    await coord.async_opt_in_all_storm_alerts()
    assert coord.client.opt_in_storm_alert.await_args_list == [
        call(alert_id="alert-a", name="Heat Advisory"),
        call(alert_id="unnamed", name="Storm Alert"),
    ]
    assert (
        coord.control_updates.updates[("storm_alert_opt_in", None)].status
        == "confirmed"
    )


async def test_opt_in_does_not_confirm_from_unrelated_active_alert(storm_opt_in):
    coord, alert = storm_opt_in
    coord.client.storm_guard_alert.side_effect = [
        {"stormAlerts": [alert]},
        {"stormAlerts": [{**alert, "id": "another", "status": "active"}]},
    ]
    await coord.async_opt_in_all_storm_alerts()
    assert coord.control_updates.pending("storm_alert_opt_in")
    coord.client.storm_guard_alert.side_effect = None
    coord.client.storm_guard_alert.return_value = {
        "stormAlerts": [{**alert, "status": "active"}] * 2
    }
    await coord.async_refresh_storm_alert(force=True)
    assert (
        coord.control_updates.updates[("storm_alert_opt_in", None)].status
        == "confirmed"
    )


@pytest.mark.parametrize("payload", [{}, {"stormAlerts": None}, {"stormAlerts": []}])
async def test_opt_in_empty_or_invalid_payload_never_writes(storm_opt_in, payload):
    coord, _alert = storm_opt_in
    coord.client.storm_guard_alert.return_value = payload
    if isinstance(payload.get("stormAlerts"), list):
        await coord.async_opt_in_all_storm_alerts()
    else:
        with pytest.raises(ServiceValidationError):
            await coord.async_opt_in_all_storm_alerts()
    coord.client.opt_in_storm_alert.assert_not_awaited()


@pytest.mark.parametrize("value", [None, 0, float("inf"), float("nan"), "bad"])
def test_opt_in_requires_known_future_expiry(storm_opt_in, value):
    coord, alert = storm_opt_in
    assert not coord.battery_runtime.storm_alert_can_opt_in({**alert, "endTime": value})


async def test_failed_initial_read_never_uses_cached_alert(storm_opt_in):
    coord, alert = storm_opt_in
    coord.battery_runtime.parse_storm_alert({"stormAlerts": [alert]})
    coord.client.storm_guard_alert.side_effect = aiohttp.ClientError()
    with pytest.raises(aiohttp.ClientError):
        await coord.async_opt_in_all_storm_alerts()
    coord.client.opt_in_storm_alert.assert_not_awaited()


async def test_opt_in_partial_failure_is_reported_after_refresh(storm_opt_in):
    coord, alert = storm_opt_in
    coord.client.storm_guard_alert.return_value = {
        "stormAlerts": [alert, {**alert, "id": "b"}]
    }
    coord.client.opt_in_storm_alert.side_effect = [
        TimeoutError(),
        {"message": "success"},
    ]
    with pytest.raises(ServiceValidationError, match="opt-in failed"):
        await coord.async_opt_in_all_storm_alerts()
    assert coord.client.opt_in_storm_alert.await_count == 2
    assert (
        coord.control_updates.updates[("storm_alert_opt_in", None)].status == "failed"
    )


async def test_opt_in_and_opt_out_share_conflict_guard(storm_opt_in):
    coord, _alert = storm_opt_in
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_write(**kwargs):
        entered.set()
        await release.wait()

    coord.client.opt_in_storm_alert.side_effect = delayed_write
    task = asyncio.create_task(coord.async_opt_in_all_storm_alerts())
    await entered.wait()
    try:
        with pytest.raises(ServiceValidationError):
            await coord.async_opt_out_all_storm_alerts()
    finally:
        release.set()
        await task


@pytest.mark.parametrize("stage", ["before", "read", "write"])
async def test_retirement_stops_opt_in_followups(storm_opt_in, stage):
    coord, alert = storm_opt_in
    if stage == "before":
        coord.mark_runtime_stopped()
    elif stage == "read":

        async def retire_read():
            coord.mark_runtime_stopped()
            return {"stormAlerts": [alert]}

        coord.client.storm_guard_alert.side_effect = retire_read
    else:
        coord.client.storm_guard_alert.return_value = {
            "stormAlerts": [alert, {**alert, "id": "b"}]
        }

        async def retire_write(**kwargs):
            coord.mark_runtime_stopped()

        coord.client.opt_in_storm_alert.side_effect = retire_write
    await coord.async_opt_in_all_storm_alerts()
    assert coord.client.opt_in_storm_alert.await_count == (1 if stage == "write" else 0)
    coord.async_request_refresh.assert_not_awaited()


async def test_retirement_during_opt_in_confirmation_stops_followups(storm_opt_in):
    coord, alert = storm_opt_in
    reads = 0

    async def retire_after_write():
        nonlocal reads
        reads += 1
        if reads == 2:
            coord.mark_runtime_stopped()
        return {"stormAlerts": [alert]}

    coord.client.storm_guard_alert.side_effect = retire_after_write
    await coord.async_opt_in_all_storm_alerts()
    coord.client.opt_in_storm_alert.assert_awaited_once_with(
        alert_id="alert-a", name="Heat Advisory"
    )
    coord.async_request_refresh.assert_not_awaited()


async def test_opt_in_cannot_overlap_profile_write(storm_opt_in):
    coord, _alert = storm_opt_in
    async with coord.battery_runtime.battery_state._battery_profile_write_lock:
        with pytest.raises(ServiceValidationError):
            await coord.async_opt_in_all_storm_alerts()
    coord.client.opt_in_storm_alert.assert_not_awaited()


@pytest.mark.parametrize("tracking", [True, False])
async def test_opt_in_skips_alert_that_expires_while_batch_is_submitting(
    storm_opt_in, monkeypatch, tracking
):
    coord, alert = storm_opt_in
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    expiring = {
        **alert,
        "id": "expiring",
        "endTime": (now[0] + timedelta(seconds=30)).timestamp() * 1000,
    }
    coord.client.storm_guard_alert.side_effect = [
        {"stormAlerts": [alert, expiring]},
        {"stormAlerts": [{**alert, "status": "active"}]},
    ]

    async def slow_write(**kwargs):
        now[0] += timedelta(minutes=1)

    coord.client.opt_in_storm_alert.side_effect = slow_write
    with monkeypatch.context() as context:
        if not tracking:
            context.setattr(coord, "control_updates", None)
        await coord.async_opt_in_all_storm_alerts()
    coord.client.opt_in_storm_alert.assert_awaited_once_with(
        alert_id="alert-a", name="Heat Advisory"
    )
    if tracking:
        update = coord.control_updates.updates[("storm_alert_opt_in", None)]
        assert update.requested == {"opted_in_alerts": 1}
        assert update.status == "confirmed"


@pytest.mark.parametrize("retire", [False, True])
async def test_opt_in_without_control_tracking(storm_opt_in, monkeypatch, retire):
    coord, alert = storm_opt_in
    if retire:

        async def retire_write(**kwargs):
            coord.mark_runtime_stopped()

        coord.client.opt_in_storm_alert.side_effect = retire_write
    with monkeypatch.context() as context:
        context.setattr(coord, "control_updates", None)
        await coord.async_opt_in_all_storm_alerts()
    coord.client.opt_in_storm_alert.assert_awaited_once()
