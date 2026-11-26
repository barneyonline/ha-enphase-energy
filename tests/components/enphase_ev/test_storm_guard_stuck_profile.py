"""Coverage of uncertain observations and normal work after a Storm Guard opt-out."""

import asyncio
from datetime import timedelta
import time
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.repairs import async_create_fix_flow
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData

_PROFILE = {
    "profile": "backup_only",
    "siteProfileStatus": "PROFILE_SWITCH_PENDING",
    "systemTask": True,
    "requestedConfig": {},
    "requestedConfigMqtt": {},
}
_ISSUE_KEY = "storm_guard_stuck_profile"


@pytest.fixture
def clock():
    with freeze_time("2026-11-25 12:00:00", real_asyncio=True) as freezer:
        yield freezer


@pytest.fixture
def advisory(clock):
    return {
        "id": "storm-a",
        "status": "opted-out",
        "endTime": (dt_util.utcnow() + timedelta(hours=8)).timestamp() * 1000,
    }


@pytest.fixture
async def coordinator(hass, config_entry, advisory):
    coord = EnphaseCoordinator(hass, dict(config_entry.data), config_entry=config_entry)
    coord.client.storm_guard_profile = AsyncMock(return_value={"data": dict(_PROFILE)})
    coord.client.battery_status = AsyncMock(
        return_value={
            "storages": [{"id": "battery-a", "battery_mode": "Self-Consumption"}]
        }
    )
    coord.client.storm_guard_alert = AsyncMock(
        return_value={"stormAlerts": [advisory], "criticalAlertActive": False}
    )
    coord.client.set_battery_profile = AsyncMock()
    coord.client.set_battery_settings = AsyncMock()
    yield coord
    coord.mark_runtime_stopped()


async def refresh(coord, *, skip=None, refresh_telemetry=True):
    if refresh_telemetry:
        for storage in coord.client.battery_status.return_value["storages"]:
            storage["last_report"] = dt_util.utcnow().timestamp()
    runtime = coord.battery_runtime
    for family, poll in (
        ("alert", runtime.async_refresh_storm_alert),
        ("profile", runtime.async_refresh_storm_guard_profile),
        ("live", runtime.async_refresh_battery_status),
    ):
        if family != skip:
            await poll(force=True)
    coord.sync_battery_profile_pending_issue()


def issue(coord):
    return ir.async_get(coord.hass).async_get_issue(
        DOMAIN, f"{_ISSUE_KEY}_{coord.config_entry.entry_id}"
    )


async def poll_for_half_hour(coord, clock, **kwargs):
    for _ in range(6):
        clock.tick(timedelta(minutes=5))
        await refresh(coord, **kwargs)


async def test_repair_leaves_profile_and_reserve_write_guard_in_place(
    coordinator, clock
):
    coord = coordinator
    await refresh(coord)
    assert issue(coord) is None
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is not None
    assert coord.battery_system_task is True
    assert not coord.battery_profile_selection_available
    assert not coord.battery_reserve_editable
    with pytest.raises(
        ServiceValidationError, match="Battery profile updates are unavailable"
    ):
        await coord.battery_runtime.async_apply_battery_profile(
            profile="self-consumption", reserve=20
        )
    with pytest.raises(
        ServiceValidationError, match="Battery profile updates are unavailable"
    ):
        await coord.battery_runtime.async_apply_battery_reserve_only(
            profile="self-consumption", reserve=20
        )
    coord.client.set_battery_profile.assert_not_awaited()
    coord.client.set_battery_settings.assert_not_awaited()
    assert coord.battery_runtime.storm_guard_stuck_profile.diagnostics() == {
        "profile_matches": True,
        "live_self_consumption": True,
        "fresh_opt_out": True,
        "repair_reported": True,
    }


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "critical",
        "active",
        "conflicting",
        "missing-list",
        "invalid-list",
        "invalid-item",
        "invalid-status",
        "missing-id",
        "expired",
    ],
)
async def test_only_a_current_explicit_opt_out_qualifies(
    coordinator, clock, advisory, change
):
    coord = coordinator
    response = coord.client.storm_guard_alert.return_value
    if change == "empty":
        response["stormAlerts"] = []
    elif change == "critical":
        response["criticalAlertActive"] = True
    elif change == "active":
        response["stormAlerts"].append({"status": "active"})
    elif change == "conflicting":
        advisory["active"] = True
    elif change == "missing-list":
        response.clear()
    elif change == "invalid-list":
        response["stormAlerts"] = {}
    elif change == "invalid-item":
        response["stormAlerts"].append(None)
    elif change == "invalid-status":
        response["stormAlerts"].append({"active": False})
    elif change == "missing-id":
        advisory.pop("id")
    else:
        advisory["endTime"] = dt_util.utcnow().timestamp() * 1000
    await refresh(coord)
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is None


@pytest.mark.parametrize(
    "change",
    [
        {"requestedConfig": {"profile": "self-consumption"}},
        {"requestedConfigMqtt": {"pendingGateways": ["gateway-a"]}},
        {"requestedConfigMqtt": None},
        {"siteProfileStatus": None},
        {"siteProfileStatus": "OTHER"},
        {"siteProfileStatus": "READY"},
        {"isBatteryChangePending": True},
        {"isBatteryChangePending": "unknown"},
        {"drEventActive": True},
        {"drEventActive": None},
        {"systemTask": None},
        {"profile": "ai_optimisation"},
        {"data": None},
    ],
)
async def test_fresh_pending_work_or_incomplete_profile_does_not_warn(
    coordinator, clock, change
):
    coord = coordinator
    profile = coord.client.storm_guard_profile.return_value
    if "data" in change:
        profile.update(change)
    else:
        profile["data"].update(change)
    await refresh(coord)
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is None


@pytest.mark.parametrize("value", [None, [], {"data": []}, {"data": {}}])
async def test_unknown_settings_cannot_erase_fresh_pending_evidence(
    coordinator, clock, value
):
    coord = coordinator
    tracker = coord.battery_runtime.storm_guard_stuck_profile
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"isBatteryChangePending": True}}
    )
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    clock.tick(timedelta(minutes=1))
    tracker.note_settings(value, time.monotonic())
    await refresh(coord)
    assert tracker._pending_work(time.monotonic())
    for _ in range(6):
        clock.tick(timedelta(minutes=5))
        await coord.battery_runtime.async_refresh_battery_settings(force=True)
        await refresh(coord)
    assert issue(coord) is None

    clock.tick(timedelta(minutes=11))
    await refresh(coord)
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is not None


@pytest.mark.parametrize("queue", ["requestedConfig", "requestedConfigMqtt"])
async def test_sibling_queue_breaks_dwell_even_if_next_read_is_clear(
    coordinator, clock, queue
):
    coord = coordinator
    runtime = coord.battery_runtime
    tracker = runtime.storm_guard_stuck_profile
    await refresh(coord)
    for _ in range(5):
        clock.tick(timedelta(minutes=5))
        await refresh(coord)

    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {queue: {"profile": "self-consumption"}}}
    )
    await runtime.async_refresh_battery_settings(force=True)
    assert tracker._pending_work(time.monotonic())
    clock.tick(timedelta(minutes=1))
    coord.client.battery_settings_details.return_value = {
        "data": {"isBatteryChangePending": False}
    }
    await runtime.async_refresh_battery_settings(force=True)
    tracker.note_settings({"isBatteryChangePending": True}, time.monotonic() - 1)
    assert not tracker._pending_work(time.monotonic())

    clock.tick(timedelta(minutes=4))
    await refresh(coord)
    assert issue(coord) is None
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is not None


@pytest.mark.parametrize("family", ["alert", "profile", "live"])
@pytest.mark.parametrize("missing", ["stale", "error"])
async def test_missing_reads_restart_dwell(coordinator, clock, family, missing):
    coord = coordinator
    await refresh(coord)
    fetcher = {
        "alert": coord.client.storm_guard_alert,
        "profile": coord.client.storm_guard_profile,
        "live": coord.client.battery_status,
    }[family]
    if missing == "error":
        fetcher.side_effect = TimeoutError
    await poll_for_half_hour(coord, clock, skip=family if missing == "stale" else None)
    assert issue(coord) is None
    fetcher.side_effect = None
    await refresh(coord)
    assert issue(coord) is None
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is not None


@pytest.mark.parametrize("reported", ["missing", "stale", "future", "invalid_storage"])
async def test_unreliable_device_telemetry_restarts_dwell(coordinator, clock, reported):
    coord = coordinator
    await refresh(coord)
    storages = coord.client.battery_status.return_value["storages"]
    storage = storages[0]
    if reported == "missing":
        del storage["last_report"]
    elif reported == "future":
        storage["last_report"] = (dt_util.utcnow() + timedelta(hours=1)).timestamp()
    elif reported == "invalid_storage":
        storages.append(None)
    await poll_for_half_hour(coord, clock, refresh_telemetry=False)
    assert issue(coord) is None
    assert (
        coord.battery_runtime.storm_guard_stuck_profile.diagnostics()[
            "live_self_consumption"
        ]
        is None
    )
    if reported == "invalid_storage":
        storages.pop()
    await refresh(coord)
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is not None


async def test_changed_advisory_needs_new_battery_observations(
    coordinator, clock, advisory
):
    coord = coordinator
    await refresh(coord)
    clock.tick(timedelta(minutes=5))
    await refresh(coord)
    advisory["id"] = "storm-b"
    clock.tick(timedelta(minutes=5))
    await refresh(coord, skip="profile")
    await refresh(coord, skip="alert")
    clock.tick(timedelta(minutes=21))
    await refresh(coord)
    assert issue(coord) is None
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is not None


async def test_late_advisory_read_does_not_replace_newer_result(coordinator, clock):
    coord = coordinator
    tracker = coord.battery_runtime.storm_guard_stuck_profile
    await refresh(coord)
    clock.tick(timedelta(minutes=5))
    await refresh(coord)
    tracker.note_alert(
        {"stormAlerts": [], "criticalAlertActive": False},
        time.monotonic() - 1,
    )
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is not None


@pytest.mark.parametrize(
    "mode", [None, "Full Backup", "Savings", "conflicting", "absent"]
)
async def test_live_change_alone_does_not_confirm_recovery(coordinator, clock, mode):
    coord = coordinator
    await refresh(coord)
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is not None
    live = [{"id": "battery-a", "battery_mode": mode}]
    if mode == "conflicting":
        live = [
            {"id": "battery-a", "battery_mode": "Self-Consumption"},
            {"id": "battery-b", "battery_mode": "Full Backup"},
        ]
    if mode == "absent":
        live = []
    coord.client.battery_status.return_value = {"storages": live}
    await refresh(coord)
    assert issue(coord) is not None


@pytest.mark.parametrize(
    "data",
    [
        {
            "profile": "self-consumption",
            "systemTask": False,
            "siteProfileStatus": "READY",
        },
        {"profile": "self-consumption", "systemTask": False},
    ],
)
async def test_warning_clears_when_both_fresh_profiles_confirm_recovery(
    coordinator, clock, data
):
    coord = coordinator
    await refresh(coord)
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is not None

    coord.client.storm_guard_profile.return_value = {"data": data}
    clock.tick(timedelta(minutes=1))
    await refresh(coord, skip="live")
    assert issue(coord) is not None
    await refresh(coord, skip="profile")
    assert issue(coord) is None


@pytest.mark.parametrize(
    "change",
    [
        {"systemTask": True},
        {"profile": "backup_only"},
        {"siteProfileStatus": "PROFILE_SWITCH_PENDING"},
        {"isBatteryChangePending": True},
        {"drEventActive": True},
        {"requestedConfig": {"profile": "self-consumption"}},
        {"requestedConfigMqtt": {"pendingGateways": ["gateway-a"]}},
    ],
)
async def test_warning_retained_for_busy_disagreement_and_pending_work(
    coordinator, clock, change
):
    coord = coordinator
    await refresh(coord)
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is not None

    profile = {
        "profile": "self-consumption",
        "systemTask": False,
        "siteProfileStatus": "READY",
    }
    profile.update(change)
    coord.client.storm_guard_profile.return_value = {"data": profile}
    await refresh(coord)
    assert issue(coord) is not None


@pytest.mark.parametrize("contradiction", ["active_storm", "sibling_queue"])
async def test_manual_check_cannot_confirm_stuck_during_normal_work(
    coordinator, reported_issue, contradiction
):
    coord = coordinator
    runtime = coord.battery_runtime
    if contradiction == "active_storm":
        coord.client.storm_guard_alert.return_value = {
            "stormAlerts": [{"id": "storm-b", "status": "active"}],
            "criticalAlertActive": False,
        }
        await runtime.async_refresh_storm_alert(force=True)
    else:
        coord.client.battery_settings_details = AsyncMock(
            return_value={"data": {"requestedConfig": {"profile": "self-consumption"}}}
        )
        await runtime.async_refresh_battery_settings(force=True)
    assert not await runtime.storm_guard_stuck_profile.async_check()
    assert issue(coord) is not None


@pytest.mark.parametrize(
    "reason", ["guard", "local-profile", "local-settings", "opt-in", "opt-out"]
)
async def test_warning_waits_while_local_state_is_settling(
    coordinator, clock, monkeypatch, reason
):
    coord = coordinator
    await refresh(coord)
    if reason == "guard":
        monkeypatch.setattr(
            type(coord), "battery_system_task", property(lambda _: False)
        )
    elif reason == "local-profile":
        monkeypatch.setattr(
            type(coord), "battery_profile_pending", property(lambda _: True)
        )
    elif reason == "local-settings":
        monkeypatch.setattr(
            type(coord), "battery_settings_write_pending", property(lambda _: True)
        )
    else:
        pending = f"storm_alert_{reason.replace('-', '_')}"
        monkeypatch.setattr(
            coord.control_updates, "pending", lambda kind, *_, **__: kind == pending
        )

    await poll_for_half_hour(coord, clock)
    assert issue(coord) is None


async def test_unloaded_runtime_cannot_create_warning(coordinator, clock):
    coord = coordinator
    await refresh(coord)
    coord.mark_runtime_stopped()
    await poll_for_half_hour(coord, clock)
    assert issue(coord) is None


@pytest.fixture
async def reported_issue(coordinator, clock, hass, config_entry):
    """Open Repairs against a loaded entry with a retained warning."""
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coordinator)
    config_entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    await refresh(coordinator)
    await poll_for_half_hour(coordinator, clock)
    assert issue(coordinator) is not None
    hass.config.components.add(DOMAIN)
    assert await async_setup_component(hass, "repairs", {})
    yield repairs_flow_manager(hass)
    config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)


async def test_check_never_uses_cached_profile_when_no_new_read(
    coordinator, reported_issue, monkeypatch
):
    coord = coordinator
    monkeypatch.setattr(
        coord.battery_runtime, "async_refresh_storm_guard_profile", AsyncMock()
    )
    coord.client.battery_status.reset_mock()
    assert not await coord.battery_runtime.storm_guard_stuck_profile.async_check()
    coord.client.battery_status.assert_not_awaited()
    assert issue(coord) is not None


async def test_check_never_uses_profile_that_aged_out_during_live_read(
    coordinator, reported_issue, clock
):
    coord = coordinator

    async def late_battery_status():
        clock.tick(timedelta(minutes=11))
        return {
            "storages": [
                {
                    "id": "battery-a",
                    "battery_mode": "Self-Consumption",
                    "last_report": dt_util.utcnow().timestamp(),
                }
            ]
        }

    coord.client.battery_status.side_effect = late_battery_status
    assert not await coord.battery_runtime.storm_guard_stuck_profile.async_check()
    assert issue(coord) is not None


@pytest.mark.parametrize(
    ("report_offset_minutes", "partial_row"),
    [(-1, None), (0, None), (-1, "missing_timestamp"), (-1, "invalid_row")],
)
async def test_check_rejects_unordered_conflicting_reports_from_one_battery(
    coordinator, reported_issue, clock, report_offset_minutes, partial_row
):
    coord = coordinator
    client = coord.client
    client.remember_battery_profile_write_context({"data": _PROFILE})
    cached_base = client._battery_config_write_bases["profile"]
    clock.tick(timedelta(minutes=2))
    now = dt_util.utcnow()
    first = {
        "serial_number": "battery-a",
        "battery_mode": "Full Backup",
        "last_report": now.timestamp(),
    }
    second = {
        "serial_number": "battery-b",
        "battery_mode": "Full Backup",
        "last_report": (now - timedelta(minutes=2)).timestamp(),
    }
    initial_second = second
    if partial_row == "missing_timestamp":
        initial_second = {"serial_number": "battery-b", "battery_mode": "Full Backup"}
    elif partial_row == "invalid_row":
        initial_second = None
    client.battery_status.return_value = {"storages": [first, initial_second]}
    await refresh(coord, refresh_telemetry=False)

    client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption", "systemTask": False}
    }
    first.update(
        battery_mode="Self-Consumption",
        last_report=(now + timedelta(minutes=report_offset_minutes)).timestamp(),
    )
    second.update(battery_mode="Self-Consumption", last_report=now.timestamp())
    client.battery_status.return_value = {"storages": [second, first]}

    manager = reported_issue
    flow = await manager.async_init(DOMAIN, context={"issue_id": issue(coord).issue_id})
    for _ in range(2):
        flow = await manager.async_configure(
            flow["flow_id"], {"next_step_id": "check_again"}
        )
        assert flow["type"] == FlowResultType.MENU
        assert flow["step_id"] == "check_failed"
        assert issue(coord) is not None
        assert coord.battery_system_task is True
        assert client._battery_config_write_bases["profile"] is cached_base

    clock.tick(timedelta(minutes=2))
    first["last_report"] = (now + timedelta(minutes=1)).timestamp()
    flow = await manager.async_configure(
        flow["flow_id"], {"next_step_id": "check_again"}
    )
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    assert issue(coord) is None
    assert coord.battery_system_task is False
    assert client._battery_config_write_bases["profile"]["systemTask"] is False
    client.set_battery_profile.assert_not_awaited()
    client.set_battery_settings.assert_not_awaited()


@pytest.mark.parametrize(
    "incomplete", ["included_count", "excluded_count", "duplicate"]
)
async def test_check_rejects_incomplete_fleet_counts(
    coordinator, reported_issue, clock, incomplete
):
    coord = coordinator
    clock.tick(timedelta(minutes=1))
    first = {
        "serial_number": "battery-a",
        "battery_mode": "Self-Consumption",
        "last_report": dt_util.utcnow().timestamp(),
        "excluded": False,
    }
    second = {
        **first,
        "serial_number": "battery-b",
        "excluded": True,
    }
    counts = {"included_count": 1, "excluded_count": 0}
    storages = [first]
    if incomplete == "duplicate":
        counts["included_count"] = 2
        storages.append(dict(first))
    else:
        counts[incomplete] += 1
    client = coord.client
    client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption", "systemTask": False}
    }
    client.battery_status.return_value = {"storages": storages, **counts}
    manager = reported_issue
    flow = await manager.async_init(DOMAIN, context={"issue_id": issue(coord).issue_id})
    flow = await manager.async_configure(
        flow["flow_id"], {"next_step_id": "check_again"}
    )
    assert flow["type"] == FlowResultType.MENU
    assert flow["step_id"] == "check_failed"
    assert issue(coord) is not None
    assert coord.battery_system_task is True
    assert not coord.battery_profile_selection_available
    assert not coord.battery_reserve_editable

    client.battery_status.return_value = {
        "storages": [first, second],
        "included_count": 1,
        "excluded_count": 1,
    }
    flow = await manager.async_configure(
        flow["flow_id"], {"next_step_id": "check_again"}
    )
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    assert issue(coord) is None
    assert coord.battery_system_task is False
    client.set_battery_profile.assert_not_awaited()
    client.set_battery_settings.assert_not_awaited()


async def test_second_fix_flow_does_not_start_duplicate_reads(
    coordinator, reported_issue
):
    coord = coordinator
    issue_id = issue(coord).issue_id
    tracker = coord.battery_runtime.storm_guard_stuck_profile
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def blocked_profile(**kwargs):
        entered.set()
        await finish.wait()
        return {"data": _PROFILE}

    coord.client.storm_guard_profile.side_effect = blocked_profile
    coord.client.storm_guard_profile.reset_mock()
    check = asyncio.create_task(tracker.async_check())
    try:
        await entered.wait()
        flow = await reported_issue.async_init(DOMAIN, context={"issue_id": issue_id})
        flow = await reported_issue.async_configure(
            flow["flow_id"], {"next_step_id": "check_again"}
        )
        assert flow["step_id"] == "check_failed"
        coord.client.storm_guard_profile.assert_awaited_once()
    finally:
        finish.set()
        assert await check


@pytest.mark.parametrize("change", ["unloaded", "replacement", "resolved"])
async def test_fix_flow_checks_loaded_owner_and_current_warning(
    coordinator, reported_issue, config_entry, hass, change
):
    coord = coordinator
    issue_id = issue(coord).issue_id
    manager = reported_issue
    flow = await manager.async_init(DOMAIN, context={"issue_id": issue_id})
    if change == "unloaded":
        coord.mark_runtime_stopped()
    elif change == "replacement":
        config_entry.runtime_data = EnphaseRuntimeData(
            coordinator=EnphaseCoordinator(
                hass, dict(config_entry.data), config_entry=config_entry
            )
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    coord.client.storm_guard_profile.reset_mock()
    flow = await manager.async_configure(
        flow["flow_id"], {"next_step_id": "check_again"}
    )
    if change == "resolved":
        assert flow["type"] == FlowResultType.CREATE_ENTRY
    else:
        assert flow["type"] == FlowResultType.ABORT
        assert flow["reason"] == "entry_unavailable"
        assert issue(coord) is not None
    coord.client.storm_guard_profile.assert_not_awaited()


async def test_wrong_stored_entry_cannot_open_fix_flow(hass):
    flow = await async_create_fix_flow(
        hass, f"{_ISSUE_KEY}_original", {"entry_id": "different"}
    )
    flow.hass = hass
    assert (await flow.async_step_init())["reason"] == "entry_unavailable"


@pytest.mark.parametrize("pause", ["auth", "rate_limit", "expired_cooldown", "retired"])
async def test_manual_readback_admission_respects_backoff(coordinator, clock, pause):
    coord = coordinator
    if pause == "auth":
        coord._auth_blocked_until_utc = dt_util.utcnow() + timedelta(minutes=2)
    elif pause == "rate_limit":
        coord._backoff_until = time.monotonic() + 120
    elif pause == "expired_cooldown":
        coord.note_endpoint_family_failure("storm_guard", TimeoutError())
        clock.tick(timedelta(minutes=6))
    else:
        coord.mark_runtime_stopped()

    assert coord.endpoint_family_can_refresh_readback("storm_guard") is (
        pause == "expired_cooldown"
    )


@pytest.mark.parametrize("retire", [True, False])
async def test_fix_flow_checks_retirement_during_profile_read(
    coordinator, reported_issue, retire
):
    coord = coordinator
    issue_id = issue(coord).issue_id
    manager = reported_issue
    flow = await manager.async_init(DOMAIN, context={"issue_id": issue_id})

    async def read_profile(**kwargs):
        if retire:
            coord.mark_runtime_stopped()
            return {"data": _PROFILE}
        raise asyncio.CancelledError

    coord.client.storm_guard_profile.side_effect = read_profile
    coord.client.battery_status.reset_mock()
    action = manager.async_configure(flow["flow_id"], {"next_step_id": "check_again"})
    if retire:
        result = await action
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "entry_unavailable"
    else:
        with pytest.raises(asyncio.CancelledError):
            await action
    assert issue(coord) is not None
    coord.client.battery_status.assert_not_awaited()


async def test_endpoint_cooldown_during_check_stops_next_read(
    coordinator, reported_issue
):
    coord = coordinator
    tracker = coord.battery_runtime.storm_guard_stuck_profile

    async def read_profile(**kwargs):
        coord.note_endpoint_family_failure("battery_status", TimeoutError())
        return {"data": _PROFILE}

    coord.client.storm_guard_profile.side_effect = read_profile
    coord.client.battery_status.reset_mock()
    assert not await tracker.async_check()
    coord.client.battery_status.assert_not_awaited()
    coord.client.storm_guard_profile.reset_mock()
    assert not await tracker.async_check()
    coord.client.storm_guard_profile.assert_not_awaited()
    assert issue(coord) is not None


async def test_inflight_check_keeps_warning_until_both_reads_confirm_recovery(
    coordinator, reported_issue
):
    coord = coordinator
    tracker = coord.battery_runtime.storm_guard_stuck_profile
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption", "systemTask": False}
    }

    async def invalid_live():
        coord.sync_battery_profile_pending_issue()
        assert issue(coord) is not None
        return {}

    coord.client.battery_status.side_effect = invalid_live
    assert not await tracker.async_check()
    coord.sync_battery_profile_pending_issue()
    assert issue(coord) is not None


@pytest.mark.parametrize(
    ("completes_during_check", "queued"), [(True, True), (False, True), (True, False)]
)
async def test_repair_retains_guard_until_settings_and_live_evidence_allow_recovery(
    coordinator, reported_issue, clock, completes_during_check, queued
):
    coord = coordinator
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = True
    coord.client.set_battery_settings_compat = AsyncMock()
    issue_id = issue(coord).issue_id
    flow = await reported_issue.async_init(DOMAIN, context={"issue_id": issue_id})
    settings_entered = asyncio.Event()
    finish_settings = asyncio.Event()

    async def read_settings():
        settings_entered.set()
        await finish_settings.wait()
        data = {
            "systemTask": True,
            "isBatteryChangePending": queued,
        }
        if queued:
            data["requestedConfigMqtt"] = {"pendingGateways": ["gateway-a"]}
        return {"data": data}

    coord.client.battery_settings_details = AsyncMock(side_effect=read_settings)
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption", "systemTask": False}
    }
    settings = asyncio.create_task(runtime.async_refresh_battery_settings(force=True))

    async def current_battery_status():
        assert coord.battery_system_task is True
        if completes_during_check:
            finish_settings.set()
            await settings
        return {
            "storages": [
                {
                    "id": "battery-a",
                    "battery_mode": "Self-Consumption",
                    "last_report": dt_util.utcnow().timestamp(),
                }
            ]
        }

    coord.client.battery_status.side_effect = current_battery_status
    try:
        await settings_entered.wait()
        clock.tick(timedelta(seconds=1))
        flow = await reported_issue.async_configure(
            flow["flow_id"], {"next_step_id": "check_again"}
        )
        assert settings.done() is completes_during_check
        finish_settings.set()
        await settings
        coord.sync_battery_profile_pending_issue()
        if queued:
            assert flow["type"] == FlowResultType.MENU
            assert flow["step_id"] == "check_failed"
            assert issue(coord) is not None
            assert coord.battery_system_task is True
            assert not coord.battery_profile_selection_available
            assert not coord.battery_reserve_editable
            with pytest.raises(
                ServiceValidationError, match="Battery profile updates are unavailable"
            ):
                await runtime.async_apply_battery_profile(
                    profile="self-consumption", reserve=20
                )
            with pytest.raises(
                ServiceValidationError, match="Battery profile updates are unavailable"
            ):
                await runtime.async_apply_battery_reserve_only(
                    profile="self-consumption", reserve=20
                )
        else:
            assert flow["type"] == FlowResultType.CREATE_ENTRY
            assert issue(coord) is None
            assert coord.battery_system_task is False
        coord.client.set_battery_profile.assert_not_awaited()
        coord.client.set_battery_settings.assert_not_awaited()
        coord.client.set_battery_settings_compat.assert_not_awaited()
    finally:
        finish_settings.set()
        await settings


async def test_newer_busy_settings_during_live_read_prevents_idle_repair(
    coordinator, reported_issue, clock
):
    coord = coordinator
    runtime = coord.battery_runtime
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption", "systemTask": False}
    }
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"systemTask": True, "isBatteryChangePending": False}}
    )

    async def current_battery_status():
        clock.tick(timedelta(seconds=1))
        assert await runtime.async_refresh_battery_settings(force=True)
        return {
            "storages": [
                {
                    "id": "battery-a",
                    "battery_mode": "Self-Consumption",
                    "last_report": dt_util.utcnow().timestamp(),
                }
            ]
        }

    coord.client.battery_status.side_effect = current_battery_status
    assert not await runtime.storm_guard_stuck_profile.async_check()
    assert issue(coord) is not None
    assert coord.battery_system_task is True
    assert not coord.battery_profile_selection_available
    assert not coord.battery_reserve_editable


async def test_older_idle_settings_cannot_hide_newer_busy_settings_during_repair(
    coordinator, reported_issue, clock
):
    coord = coordinator
    runtime = coord.battery_runtime
    issue_id = issue(coord).issue_id
    flow = await reported_issue.async_init(DOMAIN, context={"issue_id": issue_id})
    older_settings_entered = asyncio.Event()
    release_older_settings = asyncio.Event()

    async def read_settings():
        if older_settings_entered.is_set():
            return {"data": {"systemTask": True, "isBatteryChangePending": False}}
        older_settings_entered.set()
        await release_older_settings.wait()
        return {"data": {"systemTask": False, "isBatteryChangePending": False}}

    async def current_battery_status():
        clock.tick(timedelta(seconds=1))
        assert await runtime.async_refresh_battery_settings(force=True)
        release_older_settings.set()
        assert await older_settings
        return {
            "storages": [
                {
                    "id": "battery-a",
                    "battery_mode": "Self-Consumption",
                    "last_report": dt_util.utcnow().timestamp(),
                }
            ]
        }

    coord.client.battery_settings_details = AsyncMock(side_effect=read_settings)
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption", "systemTask": False}
    }
    coord.client.battery_status.side_effect = current_battery_status
    older_settings = asyncio.create_task(runtime.async_refresh_battery_settings())
    try:
        await older_settings_entered.wait()
        clock.tick(timedelta(seconds=1))
        flow = await reported_issue.async_configure(
            flow["flow_id"], {"next_step_id": "check_again"}
        )
    finally:
        release_older_settings.set()
        await older_settings

    assert flow["type"] == FlowResultType.MENU
    assert flow["step_id"] == "check_failed"
    assert issue(coord) is not None
    assert coord.battery_system_task is True
    assert not coord.battery_profile_selection_available
    assert not coord.battery_reserve_editable


@pytest.mark.parametrize("newer_system_task", [True, None, False])
async def test_later_settings_can_block_recovery_from_an_older_scheduled_profile(
    coordinator, reported_issue, clock, newer_system_task
):
    coord = coordinator
    runtime = coord.battery_runtime
    profile_entered = asyncio.Event()
    release_profile = asyncio.Event()

    async def read_profile(**kwargs):
        profile_entered.set()
        await release_profile.wait()
        return {"data": {"profile": "self-consumption", "systemTask": False}}

    coord.client.storm_guard_profile.side_effect = read_profile
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"systemTask": newer_system_task}}
    )
    clock.tick(timedelta(seconds=1))
    profile = asyncio.create_task(runtime.async_refresh_storm_guard_profile(force=True))
    try:
        await profile_entered.wait()
        clock.tick(timedelta(seconds=1))
        assert await runtime.async_refresh_battery_settings(force=True)
    finally:
        release_profile.set()
        await profile

    await refresh(coord, skip="profile")
    # The ordinary Profile parser retains its existing behavior. The Repair
    # only clears when newer Settings also supports an idle task.
    assert coord.battery_system_task is False
    assert (issue(coord) is None) is (newer_system_task is False)


@pytest.mark.parametrize("completes_during_check", [False, True])
async def test_ev_preferences_settings_work_blocks_idle_repair(
    coordinator, reported_issue, clock, completes_during_check
):
    coord = coordinator
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = True
    coord.client.set_ev_battery_preference = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def read_settings():
        entered.set()
        await release.wait()
        return {"data": {"requestedConfigMqtt": {"pendingGateways": ["gateway-a"]}}}

    async def update_ev_preferences():
        with pytest.raises(ServiceValidationError, match="preferences are unavailable"):
            await runtime.ev_preferences.async_update(enabled=True)

    coord.client.battery_settings_details = AsyncMock(side_effect=read_settings)
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption", "systemTask": False}
    }
    update = asyncio.create_task(update_ev_preferences())

    async def current_battery_status():
        if completes_during_check:
            release.set()
            await update
        return {
            "storages": [
                {
                    "id": "battery-a",
                    "battery_mode": "Self-Consumption",
                    "last_report": dt_util.utcnow().timestamp(),
                }
            ]
        }

    coord.client.battery_status.side_effect = current_battery_status
    try:
        await entered.wait()
        clock.tick(timedelta(seconds=1))
        assert not await runtime.storm_guard_stuck_profile.async_check()
        assert update.done() is completes_during_check
        assert issue(coord) is not None
        assert coord.battery_system_task is True
    finally:
        release.set()
        await update
    coord.client.set_ev_battery_preference.assert_not_awaited()


async def test_fix_flow_can_be_opened_only_for_loaded_entry(
    coordinator, reported_issue
):
    coord = coordinator
    issue_id = issue(coord).issue_id
    coord.mark_runtime_stopped()
    result = await reported_issue.async_init(DOMAIN, context={"issue_id": issue_id})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "entry_unavailable"
    assert issue(coord) is not None
