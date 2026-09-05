"""Regression tests through Home Assistant's actual config-entry lifecycle."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
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
async def test_framework_reload_recreates_sessions_and_keeps_polling(
    hass, config_entry, lifecycle_cloud
):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    old = config_entry.runtime_data
    old_session = old.coordinator.client._cookie_header_session
    old.preserve_for_reload = True
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    current = config_entry.runtime_data
    assert current is not old
    assert current.coordinator is not old.coordinator
    assert old_session.closed
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
        sessions.append(coordinator.client._cookie_header_session)
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
    assert sessions[0].closed


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
