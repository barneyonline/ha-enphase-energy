"""Regression tests through Home Assistant's actual config-entry lifecycle."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.enphase_ev import async_unload_entry
from custom_components.enphase_ev.const import CONF_SERIALS, DOMAIN
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


@pytest.fixture
def lifecycle_cloud(hass, monkeypatch):
    """Mock cloud I/O while preserving framework callbacks and session ownership."""
    monkeypatch.setattr(
        "custom_components.enphase_ev.async_create_clientsession",
        async_create_clientsession,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.async_get_clientsession",
        async_get_clientsession,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.refresh_runner.RefreshRunner.async_start_startup_power",
        AsyncMock(),
    )
    monkeypatch.setattr(EnphaseCoordinator, "async_start_startup_warmup", AsyncMock())
    monkeypatch.setattr(
        "custom_components.enphase_ev.schedule_sync.ScheduleSync.async_start",
        AsyncMock(),
    )
    update = AsyncMock(
        return_value={RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "status": "available"}}
    )
    monkeypatch.setattr(EnphaseCoordinator, "_async_update_data", update)

    async def forward(entry, platforms):
        # Real CoordinatorEntity listeners use the same registration mechanism.
        entry.async_on_unload(
            entry.runtime_data.coordinator.async_add_listener(lambda: None)
        )

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_unload", AsyncMock(return_value=True)
    )
    return update


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["platform", "schedule", "unload_owner", "teardown_owner"]
)
async def test_framework_unload_cancellation_reaches_not_loaded(
    hass, config_entry, lifecycle_cloud, monkeypatch, phase
):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    coord = config_entry.runtime_data.coordinator
    client = coord.client
    lifetime_session = client._guarded_read_session
    cookie_session = client._cookie_header_session
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    if phase in {"platform", "unload_owner"}:

        async def unload_platform(_entry, platform):
            if platform == "sensor":
                await pause()
            return True

        monkeypatch.setattr(
            hass.config_entries, "async_forward_entry_unload", unload_platform
        )
    else:
        monkeypatch.setattr(coord.schedule_sync, "async_stop", pause)

    unload = asyncio.create_task(
        hass.config_entries.async_unload(config_entry.entry_id)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert config_entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS
        if phase in {"unload_owner", "teardown_owner"}:
            config_entry.runtime_data.unload_task.cancel()
        else:
            unload.cancel()
            await asyncio.sleep(0)
            assert not unload.done()
            unload.cancel()
    finally:
        release.set()
        (result,) = await asyncio.gather(unload, return_exceptions=True)
        await hass.async_block_till_done()
        if config_entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS:
            await coord.async_close()

    if phase == "unload_owner":
        assert result is False
        assert config_entry.state is ConfigEntryState.FAILED_UNLOAD
        assert coord.runtime_active
        await coord.async_close()
        return

    assert result is True
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert lifetime_session.closed and cookie_session.closed
    assert not client._s.closed
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize("step", ["weather", "snapshot", "coordinator"])
async def test_framework_unload_finishes_after_cleanup_error(
    hass, config_entry, lifecycle_cloud, monkeypatch, caplog, step
):
    from custom_components.enphase_ev import _RUNTIME_HANDOFF_KEY

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    runtime_data = config_entry.runtime_data
    coord = runtime_data.coordinator
    client = coord.client
    sessions = (client._guarded_read_session, client._cookie_header_session)
    runtime_data.preserve_for_reload = True
    monkeypatch.setattr(coord, "async_quiesce_for_reload", AsyncMock(return_value=True))
    capture = Mock(return_value=object())
    monkeypatch.setattr(coord, "capture_reload_snapshot", capture)
    cleanup_error = RuntimeError("fixture cleanup failure")
    weather = None
    if step == "weather":
        entered = asyncio.Event()

        async def discover_weather():
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                raise cleanup_error from None

        weather = Mock()
        runtime_data.weather_coordinator = weather
        runtime_data.weather_discovery_task = asyncio.create_task(discover_weather())
        await asyncio.wait_for(entered.wait(), timeout=1)
    elif step == "snapshot":
        capture.side_effect = cleanup_error
    else:
        original_cleanup = coord.async_cleanup_runtime_state

        async def cleanup_runtime():
            await original_cleanup()
            raise cleanup_error

        monkeypatch.setattr(coord, "async_cleanup_runtime_state", cleanup_runtime)

    try:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        assert config_entry.state is ConfigEntryState.NOT_LOADED
        assert getattr(config_entry, "runtime_data", None) is None
        assert not runtime_data.preserve_for_reload
        assert not coord.runtime_active
        assert all(session.closed for session in sessions)
        assert not client._s.closed
        assert config_entry.entry_id not in hass.data.get(_RUNTIME_HANDOFF_KEY, {})
        assert "fixture cleanup failure" in caplog.text
        if weather is not None:
            weather.mark_stopped.assert_called_once()
            assert runtime_data.weather_coordinator is None
            capture.assert_not_called()
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        assert await hass.config_entries.async_unload(config_entry.entry_id)
    finally:
        await coord.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry", "replace_runtime"),
    [(False, False), (True, False), (False, True)],
    ids=["cancelled", "retried", "replaced"],
)
async def test_cancelled_unload_finishes_runtime_teardown_in_order(
    hass, config_entry, lifecycle_cloud, monkeypatch, retry, replace_runtime
):
    from custom_components.enphase_ev import _RUNTIME_HANDOFF_KEY
    from custom_components.enphase_ev.api import guarded_read_scope

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    coord = config_entry.runtime_data.coordinator
    if replace_runtime:
        config_entry.runtime_data.preserve_for_reload = True
        monkeypatch.setattr(
            coord, "async_quiesce_for_reload", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            coord, "capture_reload_snapshot", Mock(return_value=object())
        )
    client = coord.client
    lifetime_session = client._guarded_read_session
    cookie_session = client._cookie_header_session
    shared_session = client._s
    request_entered, schedule_entered, intent_entered = (
        asyncio.Event() for _ in range(3)
    )
    release_schedule, release_intent = asyncio.Event(), asyncio.Event()
    requests_unwound = asyncio.Event()

    async def lifetime_request():
        with guarded_read_scope(lambda: coord.runtime_active):
            async with client._request_session() as session:
                assert session is lifetime_session
                request_entered.set()
                try:
                    await asyncio.Future()
                finally:
                    requests_unwound.set()

    async def stop_schedule():
        schedule_entered.set()
        await release_schedule.wait()

    async def wait_for_intent():
        intent_entered.set()
        await release_intent.wait()

    schedule_stop = AsyncMock(side_effect=stop_schedule)
    intent_drain = AsyncMock(side_effect=wait_for_intent)
    monkeypatch.setattr(coord.schedule_sync, "async_stop", schedule_stop)
    monkeypatch.setattr(
        coord.export_limit_runtime, "async_wait_for_intent_transactions", intent_drain
    )
    request = asyncio.create_task(lifetime_request())
    await asyncio.wait_for(request_entered.wait(), timeout=1)

    unload = asyncio.create_task(async_unload_entry(hass, config_entry))
    retry_task = None
    try:
        await asyncio.wait_for(schedule_entered.wait(), timeout=1)
        assert not coord.runtime_active
        platform_calls = hass.config_entries.async_forward_entry_unload.await_count
        unload.cancel()
        await asyncio.sleep(0)
        assert not unload.done()
        if retry:
            retry_task = asyncio.create_task(async_unload_entry(hass, config_entry))
            await asyncio.sleep(0)
            assert not retry_task.done()
        release_schedule.set()
        await asyncio.wait_for(intent_entered.wait(), timeout=1)
        assert not requests_unwound.is_set()
        assert not lifetime_session.closed and not cookie_session.closed
        replacement = None
        if replace_runtime:
            replacement = EnphaseRuntimeData(
                coordinator=EnphaseCoordinator(hass, config_entry.data)
            )
            config_entry.runtime_data = replacement
        release_intent.set()
        await asyncio.wait_for(requests_unwound.wait(), timeout=1)
        assert await unload
        if retry_task is not None:
            assert await retry_task
        schedule_stop.assert_awaited_once()
        intent_drain.assert_awaited_once()
        assert (
            hass.config_entries.async_forward_entry_unload.await_count == platform_calls
        )
        assert lifetime_session.closed and cookie_session.closed
        assert not shared_session.closed
        assert config_entry.runtime_data is replacement
        assert config_entry.entry_id not in hass.data.get(_RUNTIME_HANDOFF_KEY, {})
    finally:
        release_schedule.set()
        release_intent.set()
        await asyncio.gather(unload, return_exceptions=True)
        if retry_task is not None:
            await asyncio.gather(retry_task, return_exceptions=True)
        await client.async_close()
        await asyncio.gather(request, return_exceptions=True)


async def test_framework_unload_allows_stored_login_cancellation_to_finish(
    hass, config_entry, lifecycle_cloud, monkeypatch
):
    from custom_components.enphase_ev import auth_refresh_runtime

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    coord = config_entry.runtime_data.coordinator
    login_entered, login_unwinding, cleanup_entered, release_login = (
        asyncio.Event() for _ in range(4)
    )
    login_unwound = asyncio.Event()
    cleanup_state = coord.async_cleanup_runtime_state

    async def authenticate(*_args):
        login_entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            login_unwinding.set()
            await release_login.wait()
            login_unwound.set()
            raise

    async def observe_cleanup():
        cleanup_entered.set()
        await cleanup_state()

    monkeypatch.setattr(auth_refresh_runtime, "async_authenticate", authenticate)
    monkeypatch.setattr(coord.schedule_sync, "async_stop", login_unwinding.wait)
    monkeypatch.setattr(coord, "async_cleanup_runtime_state", observe_cleanup)

    manual = asyncio.create_task(coord.async_try_reauth_now())
    await asyncio.wait_for(login_entered.wait(), timeout=1)
    login = coord._auth_refresh_task
    unload = asyncio.create_task(
        hass.config_entries.async_unload(config_entry.entry_id)
    )
    try:
        await asyncio.wait_for(cleanup_entered.wait(), timeout=1)
        assert login.cancelling() == 1
        assert not login.done()
        assert not unload.done()
    finally:
        release_login.set()
        assert await unload
        result = await manual

    assert login_unwound.is_set()
    assert not result.success and result.reason == "reauth_failed"


@pytest.mark.asyncio
async def test_framework_reload_recreates_sessions_and_keeps_polling(
    hass, config_entry, lifecycle_cloud
):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    old = config_entry.runtime_data
    old_session = old.coordinator.client._cookie_header_session
    old_lifetime_session = old.coordinator.client._guarded_read_session
    shared_session = old.coordinator.client._s
    assert old_lifetime_session.cookie_jar is shared_session.cookie_jar
    assert old_lifetime_session.connector is shared_session.connector
    old.preserve_for_reload = True
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    current = config_entry.runtime_data
    assert current is not old
    assert current.coordinator is not old.coordinator
    assert old_session.closed
    assert old_lifetime_session.closed
    assert not shared_session.closed
    assert not current.coordinator.client._guarded_read_session.closed
    assert not current.coordinator.client._cookie_header_session.closed
    assert current.coordinator.data[RANDOM_SERIAL]["status"] == "available"
    assert current.coordinator.setup_phase_timings["first_refresh_s"] == 0
    before = lifecycle_cloud.await_count
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=300))
    await hass.async_block_till_done()
    assert lifecycle_cloud.await_count > before
    # Cookie-authenticated requests receive a usable new stateless session.
    async with current.coordinator.client._request_session(
        cookie_header_only=True
    ) as session:
        assert session is not old_session
        assert not session.closed
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert hass.services.has_service(DOMAIN, "force_refresh")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "request_grid_toggle_otp",
            {"config_entry_id": config_entry.entry_id},
            blocking=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("active_serials", [["replacement-charger"], []])
async def test_reload_does_not_rediscover_retired_configured_charger(
    hass, config_entry, lifecycle_cloud, monkeypatch, active_serials
):
    """Platform setup must see live discovery instead of historical entry serials."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    old = config_entry.runtime_data
    old.coordinator._devices_inventory_ready = True
    old.coordinator._status_charger_data_authoritative = True
    old.coordinator._status_charger_data_serials = active_serials
    # Include stale telemetry to ensure it cannot add the retired serial back.
    old.coordinator.data = {
        serial: {"sn": serial, "status": "available"}
        for serial in [RANDOM_SERIAL, *active_serials]
    }
    assert config_entry.data[CONF_SERIALS] == [RANDOM_SERIAL]
    assert old.coordinator.iter_serials() == active_serials
    forwarded = []

    async def forward(entry, platforms):
        coord = entry.runtime_data.coordinator
        forwarded.append(coord.iter_serials())
        assert set(coord.data) == set(active_serials)
        assert coord._configured_serials == {RANDOM_SERIAL}
        assert not coord._devices_inventory_ready

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    lifecycle_cloud.return_value = {
        serial: {"sn": serial, "status": "available"} for serial in active_serials
    }
    old.preserve_for_reload = True
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert forwarded == [active_serials]
    assert config_entry.runtime_data.coordinator.iter_serials() == active_serials
    assert await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.asyncio
async def test_framework_failed_setup_rolls_back_then_retries(
    hass, config_entry, lifecycle_cloud, monkeypatch
):
    failure = AsyncMock(side_effect=ConfigEntryNotReady("offline"))
    monkeypatch.setattr(EnphaseCoordinator, "async_bootstrap_first_refresh", failure)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert getattr(config_entry, "runtime_data", None) is None
    monkeypatch.setattr(
        EnphaseCoordinator,
        "async_bootstrap_first_refresh",
        EnphaseCoordinator.async_config_entry_first_refresh,
    )
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_framework_setup_failure_cancels_integration_owned_tasks(
    hass, config_entry, lifecycle_cloud, monkeypatch, cancelled
):
    import asyncio

    stopped = asyncio.Event()
    started = asyncio.Event()
    sessions = []
    failure_ready = asyncio.Event()

    async def background():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def broken_forward(entry, platforms):
        coordinator = entry.runtime_data.coordinator
        sessions.extend(
            [
                coordinator.client._cookie_header_session,
                coordinator.client._guarded_read_session,
            ]
        )
        task = hass.async_create_background_task(background(), "enphase_setup_probe")
        coordinator.track_entry_background_task(task)
        await started.wait()
        if cancelled:
            failure_ready.set()
            await asyncio.Event().wait()
        raise RuntimeError("platform forwarding failed")

    monkeypatch.setattr(
        hass.config_entries, "async_forward_entry_setups", broken_forward
    )
    if cancelled:
        setup_task = asyncio.create_task(
            hass.config_entries.async_setup(config_entry.entry_id)
        )
        await failure_ready.wait()
        setup_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await setup_task
    else:
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert stopped.is_set()
    assert config_entry.runtime_data is None
    assert all(session.closed for session in sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("user_change", ["options", "data", "none"])
async def test_internal_persistence_does_not_swallow_queued_user_update(
    hass, config_entry, lifecycle_cloud, user_change
):
    """Exercise callbacks queued by HA before an overlapping user edit."""
    from custom_components.enphase_ev.const import OPT_VPP_EVENTS_ENABLED

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    runtime = config_entry.runtime_data
    coordinator = runtime.coordinator
    before = dict(config_entry.data)
    assert coordinator._async_update_config_entry_data_internal(
        {**before, "internal_cooldown": "2026-09-05"}, reason="test"
    )
    # Both writes occur before HA runs either update listener.
    if user_change == "options":
        hass.config_entries.async_update_entry(
            config_entry,
            options={**config_entry.options, OPT_VPP_EVENTS_ENABLED: True},
        )
    elif user_change == "data":
        hass.config_entries.async_update_entry(
            config_entry, data={**config_entry.data, "username": "new@example.test"}
        )
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    if user_change == "none":
        assert config_entry.runtime_data is runtime
        assert runtime.applied_data["internal_cooldown"] == "2026-09-05"
    else:
        assert config_entry.runtime_data is not runtime
        if user_change == "options":
            assert config_entry.runtime_data.applied_options[OPT_VPP_EVENTS_ENABLED]
        else:
            assert (
                config_entry.runtime_data.applied_data["username"] == "new@example.test"
            )
    assert await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, RuntimeError("persistence failed")])
async def test_failed_internal_persistence_clears_pending_delta(
    hass, config_entry, lifecycle_cloud, monkeypatch, result
):
    from unittest.mock import Mock

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    runtime = config_entry.runtime_data
    with monkeypatch.context() as patch:
        update = (
            Mock(side_effect=result)
            if isinstance(result, Exception)
            else Mock(return_value=result)
        )
        patch.setattr(hass.config_entries, "async_update_entry", update)
        if isinstance(result, Exception):
            with pytest.raises(RuntimeError, match="persistence failed"):
                runtime.coordinator._async_update_config_entry_data_internal(
                    {**config_entry.data, "cooldown": True}, reason="test"
                )
        else:
            assert not runtime.coordinator._async_update_config_entry_data_internal(
                {**config_entry.data, "cooldown": True}, reason="test"
            )
    assert runtime.internal_data_updates == []
    assert runtime.reload_suppression_count == 0
    assert await hass.config_entries.async_unload(config_entry.entry_id)
