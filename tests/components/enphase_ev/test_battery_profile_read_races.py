"""Battery reads must not apply snapshots across an accepted profile write."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["profile", "settings", "status"])
@pytest.mark.parametrize("confirm_live", [False, True])
async def test_read_started_before_write_cannot_replace_or_confirm_new_intent(
    coordinator_factory, monkeypatch, family, confirm_live
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = True
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = Mock()
    runtime.parse_battery_profile_payload(
        {"data": {"profile": "backup_only", "batteryBackupPercentage": 100}}
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    # Without live confirmation, an old snapshot happens to match the new
    # target. With confirmation, the old snapshot conflicts with confirmed state.
    old_profile = "backup_only" if confirm_live else "self-consumption"
    old_label = "Full Backup" if confirm_live else "Self-Consumption"
    old_payload = (
        {"storages": [{"id": "123", "battery_mode": old_label}]}
        if family == "status"
        else {
            "data": {
                "profile": old_profile,
                "batteryBackupPercentage": 100 if confirm_live else 5,
                "isBatteryChangePending": False,
            }
        }
    )

    async def delayed_read(*args, **kwargs):
        # The server snapshot exists before the local write is even started.
        entered.set()
        await release.wait()
        return old_payload

    fetch_name, refresh_name = {
        "profile": ("storm_guard_profile", "async_refresh_storm_guard_profile"),
        "settings": ("battery_settings_details", "async_refresh_battery_settings"),
        "status": ("battery_status", "async_refresh_battery_status"),
    }[family]
    monkeypatch.setattr(coord.client, fetch_name, delayed_read)
    read_task = asyncio.create_task(getattr(runtime, refresh_name)(force=True))
    await entered.wait()
    try:
        await runtime.async_apply_battery_profile(
            profile="self-consumption", reserve=5, require_exact_pending_match=False
        )
        if confirm_live:
            runtime.parse_battery_status_payload(
                {"storages": [{"id": "123", "battery_mode": "Self-Consumption"}]}
            )
            assert not coord.battery_profile_pending
        release.set()
        await read_task
        if confirm_live:
            assert coord.battery_selected_profile == "self-consumption"
            assert coord.battery_live_profile == "self-consumption"
            assert coord.battery_effective_backup_percentage == 5
        else:
            assert coord.battery_profile_pending
        coord.client.set_battery_profile.assert_awaited_once()
    finally:
        release.set()
        await read_task


@pytest.mark.asyncio
async def test_prewrite_settings_cannot_relock_newer_reserve_readback(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    entered = asyncio.Event()
    release = asyncio.Event()

    async def old_settings():
        entered.set()
        await release.wait()
        return {"data": {"rbdControl": {"show": True, "locked": True}}}

    monkeypatch.setattr(coord.client, "battery_settings_details", old_settings)
    task = asyncio.create_task(runtime.async_refresh_battery_settings(force=True))
    await entered.wait()
    try:
        runtime.set_battery_pending(
            profile="self-consumption", reserve=5, sub_type=None
        )
        runtime.parse_battery_profile_payload(
            {"data": {"profile": "self-consumption", "batteryBackupPercentage": 5}}
        )
        runtime.parse_battery_settings_payload(
            {"data": {"rbdControl": {"show": True, "locked": False}}}
        )
        assert coord.battery_reserve_editable
        release.set()
        assert await task is False
        assert coord.battery_reserve_editable
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("write_fails", [False, True])
async def test_read_started_during_write_waits_for_postwrite_confirmation(
    coordinator_factory, monkeypatch, write_fails
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = True
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = Mock()
    write_entered = asyncio.Event()
    write_release = asyncio.Event()
    read_entered = asyncio.Event()
    read_release = asyncio.Event()

    async def write(**kwargs):
        write_entered.set()
        await write_release.wait()
        if write_fails:
            raise TimeoutError
        return {"message": "success"}

    async def read(**kwargs):
        read_entered.set()
        await read_release.wait()
        return {"data": {"profile": "self-consumption", "batteryBackupPercentage": 5}}

    coord.client.set_battery_profile = AsyncMock(side_effect=write)
    monkeypatch.setattr(coord.client, "storm_guard_profile", read)
    write_task = asyncio.create_task(
        runtime.async_apply_battery_profile(
            profile="self-consumption", reserve=5, require_exact_pending_match=False
        )
    )
    await write_entered.wait()
    read_task = asyncio.create_task(
        runtime.async_refresh_storm_guard_profile(force=True)
    )
    await read_entered.wait()
    try:
        write_release.set()
        if write_fails:
            with pytest.raises(TimeoutError):
                await write_task
        else:
            await write_task
        read_release.set()
        await read_task
        if write_fails:
            # Failed writes cannot suppress a valid concurrent read indefinitely.
            assert coord.battery_profile == "self-consumption"
            assert not coord.battery_profile_pending
        else:
            assert coord.battery_profile_pending
            # A new read after acceptance confirms normally, without a retry PUT.
            await runtime.async_refresh_storm_guard_profile(force=True)
            assert not coord.battery_profile_pending
            assert coord.battery_profile == "self-consumption"
        coord.client.set_battery_profile.assert_awaited_once()
    finally:
        write_release.set()
        read_release.set()
        await asyncio.gather(write_task, read_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_postwrite_read_failure_preserves_intent_and_new_read_can_revert(
    coordinator_factory,
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    runtime.set_battery_pending(
        profile="self-consumption",
        reserve=5,
        sub_type=None,
        require_exact_settings=False,
    )
    coord.client.storm_guard_profile = AsyncMock(side_effect=TimeoutError)
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {"profile": "self-consumption", "batteryBackupPercentage": 5}
        }
    )
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    # Preserve #694's configured-profile contract for genuinely newer reads.
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "backup_only", "batteryBackupPercentage": 100}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_selected_profile == "backup_only"
