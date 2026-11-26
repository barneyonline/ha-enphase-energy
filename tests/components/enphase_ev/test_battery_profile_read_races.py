"""Battery reads must not apply snapshots across an accepted profile write."""

import asyncio
import time
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientResponseError, RequestInfo
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from yarl import URL

from custom_components.enphase_ev.api_client.common import (
    check_enlighten_read_admission,
)

_PROFILE_SOURCES = [
    ("storm_guard", "storm_guard_profile", "async_refresh_storm_guard_profile"),
    ("battery_status", "battery_status", "async_refresh_battery_status"),
]


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


async def test_storm_guard_repair_defers_to_inflight_poll(coordinator_factory):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    entered = asyncio.Event()
    release = asyncio.Event()

    async def read(**kwargs):
        entered.set()
        await release.wait()
        return {"data": {"profile": "backup_only", "systemTask": True}}

    coord.client.storm_guard_profile = AsyncMock(side_effect=read)
    poll = asyncio.create_task(runtime.async_refresh_storm_guard_profile())
    await entered.wait()
    try:
        assert not await runtime.storm_guard_stuck_profile.async_check()
        coord.client.storm_guard_profile.assert_awaited_once()
    finally:
        release.set()
        assert await poll
    assert coord.battery_system_task is True


@pytest.mark.parametrize("family,endpoint,refresh", _PROFILE_SOURCES)
async def test_poll_cannot_erase_new_repair_retry_after(
    coordinator_factory, family, endpoint, refresh
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    repair_entered = asyncio.Event()
    allow_response = asyncio.Event()
    url = URL("https://example.com")
    rate_limited = ClientResponseError(
        RequestInfo(url, "GET", {}, url),
        (),
        status=429,
        headers={"Retry-After": "1800"},
    )

    async def read(**kwargs):
        repair_entered.set()
        await allow_response.wait()
        raise rate_limited

    fetch = AsyncMock(side_effect=read)
    setattr(coord.client, endpoint, fetch)
    read_source = getattr(runtime, refresh)
    repair = asyncio.create_task(read_source(repair=True))
    await repair_entered.wait()
    poll = asyncio.create_task(read_source())
    try:
        await asyncio.sleep(0)
        fetch.assert_awaited_once()
    finally:
        allow_response.set()
        results = await asyncio.gather(repair, poll)

    assert results == [False, False]
    health = coord._endpoint_family_state(family)
    assert health.last_status == 429
    assert health.next_retry_mono - time.monotonic() > 1700
    assert not await read_source(repair=True)
    fetch.assert_awaited_once()


@pytest.mark.parametrize("family,endpoint,refresh", _PROFILE_SOURCES)
async def test_repair_backoff_rechecked_at_transport_admission(
    coordinator_factory, family, endpoint, refresh
):
    coord = coordinator_factory()
    sent = Mock()

    async def queued_read(**kwargs):
        coord._backoff_until = time.monotonic() + 120
        check_enlighten_read_admission()
        sent()
        return {}

    setattr(coord.client, endpoint, queued_read)
    assert not await getattr(coord.battery_runtime, refresh)(repair=True)
    sent.assert_not_called()
    assert coord._endpoint_family_state(family).request_count == 0


async def test_repair_rejects_background_profile_result_during_live_read(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    tracker = runtime.storm_guard_stuck_profile
    sync = Mock()
    monkeypatch.setattr(tracker, "sync", sync)
    live_started = asyncio.Event()
    live_release = asyncio.Event()
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {
                "profile": "backup_only",
                "systemTask": True,
                "siteProfileStatus": "PROFILE_SWITCH_PENDING",
                "requestedConfig": {},
                "requestedConfigMqtt": {},
            }
        }
    )

    async def live_read():
        live_started.set()
        await live_release.wait()
        return {
            "storages": [
                {
                    "id": "battery-a",
                    "battery_mode": "Self-Consumption",
                    "last_report": dt_util.utcnow().timestamp(),
                }
            ]
        }

    coord.client.battery_status = AsyncMock(side_effect=live_read)
    check = asyncio.create_task(tracker.async_check())
    try:
        await asyncio.wait_for(live_started.wait(), timeout=5)
        assert await runtime.async_refresh_storm_guard_profile(force=True)
    finally:
        live_release.set()
        assert not await check
    assert coord.client.storm_guard_profile.await_count == 2
    coord.client.battery_status.assert_awaited_once()
    sync.assert_not_called()


@pytest.mark.parametrize("reported_success", [False, True])
async def test_repair_requires_its_own_applied_read(
    coordinator_factory, monkeypatch, reported_success
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    tracker = runtime.storm_guard_stuck_profile
    ordinary_refresh = runtime.async_refresh_storm_guard_profile
    coord.client.storm_guard_profile = AsyncMock(
        return_value={"data": {"systemTask": False}}
    )
    coord.client.battery_status = AsyncMock()

    async def refresh(*, repair):
        assert repair
        if not reported_success:
            # A background poll succeeded, while the repair could not read.
            assert await asyncio.create_task(ordinary_refresh())
        return reported_success

    monkeypatch.setattr(runtime, "async_refresh_storm_guard_profile", refresh)
    assert not await tracker.async_check()
    coord.client.battery_status.assert_not_awaited()


async def test_settings_started_before_check_cannot_unlock_newer_busy_profile(
    coordinator_factory,
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    settings_started = asyncio.Event()
    allow_settings = asyncio.Event()

    async def read_settings():
        settings_started.set()
        await allow_settings.wait()
        return {"data": {"systemTask": False}}

    coord.client.battery_settings_details = AsyncMock(side_effect=read_settings)
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {
                "profile": "backup_only",
                "systemTask": True,
                "siteProfileStatus": "PROFILE_SWITCH_PENDING",
                "requestedConfig": {},
                "requestedConfigMqtt": {},
            }
        }
    )
    settings = asyncio.create_task(runtime.async_refresh_battery_settings())
    await settings_started.wait()
    try:
        assert await runtime.async_refresh_storm_guard_profile(repair=True)
    finally:
        allow_settings.set()
        result = await settings

    assert not result
    assert coord.battery_system_task is True
    assert not coord.battery_profile_selection_available
    assert not coord.battery_reserve_editable

    # A new settings response keeps its usual authority.
    assert await runtime.async_refresh_battery_settings()
    assert coord.battery_system_task is False


@pytest.mark.parametrize("permission_refresh", [False, True])
async def test_older_site_settings_cannot_replace_busy_but_can_confirm_permissions(
    coordinator_factory, permission_refresh
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = None
    coord._battery_user_is_installer = None
    entered = asyncio.Event()
    released = asyncio.Event()

    async def site_settings():
        entered.set()
        await released.wait()
        return {"data": {"userDetails": {"isOwner": True}, "systemTask": False}}

    coord.client.battery_site_settings = AsyncMock(side_effect=site_settings)
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {
                "profile": "backup_only",
                "systemTask": True,
                "siteProfileStatus": "PROFILE_SWITCH_PENDING",
                "requestedConfig": {},
                "requestedConfigMqtt": {},
            }
        }
    )
    read = (
        runtime.async_ensure_battery_write_access_confirmed
        if permission_refresh
        else runtime.async_refresh_battery_site_settings
    )
    task = asyncio.create_task(read())
    await entered.wait()
    try:
        assert await runtime.async_refresh_storm_guard_profile(repair=True)
    finally:
        released.set()
        await task

    assert coord.battery_write_access_confirmed
    assert coord.battery_system_task is True
    assert not coord.battery_profile_selection_available


@pytest.mark.parametrize(
    "source", ["settings", "site_settings", "permissions", "ev_preferences"]
)
async def test_older_repair_profile_cannot_replace_newer_busy_settings(
    coordinator_factory, source
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = None
    coord._battery_user_is_installer = None
    entered = asyncio.Event()
    released = asyncio.Event()

    async def profile(**kwargs):
        entered.set()
        await released.wait()
        return {
            "data": {
                "profile": "self-consumption",
                "siteProfileStatus": "READY",
                "systemTask": False,
            }
        }

    coord.client.storm_guard_profile = AsyncMock(side_effect=profile)
    coord.client.battery_status = AsyncMock()
    settings = {"data": {"userDetails": {"isOwner": True}, "systemTask": True}}
    coord.client.battery_settings_details = AsyncMock(return_value=settings)
    coord.client.battery_site_settings = AsyncMock(return_value=settings)

    async def read_ev_preferences():
        coord._battery_user_is_owner = True
        with pytest.raises(ServiceValidationError, match="preferences are unavailable"):
            await runtime.ev_preferences.async_update(enabled=True)

    refresh = {
        "settings": runtime.async_refresh_battery_settings,
        "site_settings": runtime.async_refresh_battery_site_settings,
        "permissions": runtime.async_ensure_battery_write_access_confirmed,
        "ev_preferences": read_ev_preferences,
    }[source]
    repair = asyncio.create_task(runtime.storm_guard_stuck_profile.async_check())
    await entered.wait()
    try:
        await refresh()
        assert coord.battery_system_task is True
    finally:
        released.set()
        refreshed = await repair

    assert not refreshed
    assert coord.battery_system_task is True
    assert not coord.battery_profile_selection_available
    assert not coord.battery_reserve_editable
    coord.client.battery_status.assert_not_awaited()


@pytest.mark.parametrize(
    "incomplete",
    [
        {"systemTask": False},
        {
            "profile": "self-consumption",
            "systemTask": False,
            "isBatteryChangePending": True,
        },
    ],
)
async def test_incomplete_repair_read_cannot_unlock_battery_controls(
    coordinator_factory, incomplete
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_system_task = True
    coord.client.storm_guard_profile = AsyncMock(return_value={"data": incomplete})
    coord.client.battery_status = AsyncMock()

    assert not await runtime.storm_guard_stuck_profile.async_check()
    assert coord.battery_system_task is True
    assert not coord.battery_profile_selection_available
    coord.client.battery_status.assert_not_awaited()

    assert await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_system_task is False


async def test_idle_repair_profile_without_paired_check_preserves_write_context(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    client = coord.client
    coord._battery_user_is_owner = True
    busy = {"profile": "backup_only", "systemTask": True}
    busy_read = {"data": busy | {"supportsMqtt": True}}
    idle = {
        "profile": "self-consumption",
        "siteProfileStatus": "READY",
        "systemTask": False,
        "supportsMqtt": False,
    }
    monkeypatch.setattr(
        client,
        "_battery_config_request",
        AsyncMock(side_effect=[busy_read, {"data": idle}]),
    )

    assert await runtime.async_refresh_storm_guard_profile()
    assert await runtime.async_refresh_storm_guard_profile(repair=True)

    assert coord.battery_profile == "self-consumption"
    assert coord.battery_system_task is True
    assert not coord.battery_profile_selection_available
    assert not coord.battery_reserve_editable
    assert client._battery_config_write_bases["profile"] == busy
    assert client._battery_config_supports_mqtt is True
    assert runtime._repair_profile_stage is None
