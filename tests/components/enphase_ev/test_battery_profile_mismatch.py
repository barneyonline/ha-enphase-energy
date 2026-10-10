"""Persistent source disagreements and explicit Repair recovery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.enphase_ev import battery_profile_mismatch as mismatch_mod
from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.repairs import async_create_fix_flow
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData


@pytest.fixture
async def mismatch_repair(hass, config_entry, monkeypatch):
    coord = EnphaseCoordinator(hass, dict(config_entry.data), config_entry=config_entry)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    config_entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    runtime = coord.battery_runtime
    clock = [1000.0]
    monkeypatch.setattr(
        mismatch_mod, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {
                "profile": "backup_only",
                "batteryBackupPercentage": 100,
                "previousBatteryBackupPercentage": {"self-consumption": 5},
            }
        }
    )
    coord.client.battery_status = AsyncMock(
        return_value={
            "storages": [{"id": "battery-a", "battery_mode": "Self-Consumption"}]
        }
    )
    coord.client.storm_guard_alert = AsyncMock(
        return_value={"stormAlerts": [], "criticalAlertActive": False}
    )
    coord.client.battery_site_settings = AsyncMock(
        return_value={"data": {"userDetails": {"isOwner": True}}}
    )
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    await runtime.async_refresh_storm_guard_profile(force=True)
    await runtime.async_refresh_battery_status(force=True)
    coord.diagnostics.sync_battery_profile_pending_issue()
    clock[0] += 601
    await runtime.async_refresh_storm_guard_profile(force=True)
    await runtime.async_refresh_battery_status(force=True)
    coord.diagnostics.sync_battery_profile_pending_issue()
    assert ir.async_get(hass).async_get_issue(DOMAIN, runtime.profile_mismatch.issue_id)
    hass.config.components.add(DOMAIN)
    assert await async_setup_component(hass, "repairs", {})
    yield coord, clock, repairs_flow_manager(hass)
    coord.mark_runtime_stopped()
    config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)


async def start_repair(coord, manager):
    return await manager.async_init(
        DOMAIN, data={"issue_id": coord.battery_runtime.profile_mismatch.issue_id}
    )


async def test_persistent_mismatch_has_fresh_scoped_context_and_check(
    mismatch_repair, hass
):
    coord, clock, manager = mismatch_repair
    mismatch = coord.battery_runtime.profile_mismatch
    issue = ir.async_get(hass).async_get_issue(DOMAIN, mismatch.issue_id)
    assert issue.is_fixable
    assert issue.data == {
        "entry_id": coord.config_entry.entry_id,
        "identity": mismatch.identity,
    }
    assert mismatch.diagnostics() == {
        "configured_profile": "backup_only",
        "live_profile": "self-consumption",
        "repair_reported": True,
    }
    result = await start_repair(coord, manager)
    assert result["menu_options"] == ["check_again", "retry_profile"]
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "check_again"}
    )
    assert result["step_id"] == "pending"
    coord.client.set_battery_profile.assert_not_awaited()
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption"}
    }
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "check_again"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert ir.async_get(hass).async_get_issue(DOMAIN, mismatch.issue_id) is None


async def test_explicit_retry_displays_values_and_writes_once(mismatch_repair):
    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "retry_profile"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["description_placeholders"] == {
        "profile": "Self-Consumption",
        "reserve": "5",
    }
    coord.client.set_battery_profile.assert_not_awaited()
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    coord.client.set_battery_profile.assert_awaited_once_with(
        profile="self-consumption",
        battery_backup_percentage=5,
        operation_mode_sub_type=None,
        devices=None,
    )
    assert coord.battery_profile_pending


@pytest.mark.parametrize("family", ["profile", "status"])
async def test_failed_fresh_read_prevents_retry(mismatch_repair, family):
    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "retry_profile"}
    )
    fetcher = (
        coord.client.storm_guard_profile
        if family == "profile"
        else coord.client.battery_status
    )
    fetcher.side_effect = aiohttp.ClientError()
    result = await manager.async_configure(result["flow_id"], {})
    assert result["step_id"] == "check_failed"
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.parametrize("alerts", [{"stormAlerts": [{"status": "active"}]}, {}])
async def test_active_or_unknown_alert_blocks_retry(mismatch_repair, alerts):
    coord, clock, manager = mismatch_repair
    coord.client.storm_guard_alert.return_value = alerts
    result = await start_repair(coord, manager)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "retry_profile"}
    )
    result = await manager.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cannot_retry"}
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.parametrize("stage", ["profile", "status", "alert", "permission"])
@pytest.mark.parametrize("change", ["retire", "replace", "resolve", "new_write"])
async def test_changes_across_awaits_never_retry_stale_context(
    mismatch_repair, stage, change
):
    coord, clock, manager = mismatch_repair
    runtime = coord.battery_runtime
    result = await start_repair(coord, manager)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "retry_profile"}
    )

    def change_context():
        if change == "retire":
            coord.mark_runtime_stopped()
        elif change == "replace":
            runtime.profile_mismatch.note_live("cost_savings")
            runtime.profile_mismatch.sync()
        elif change == "resolve":
            runtime.profile_mismatch.note_live("backup_only")
            runtime.profile_mismatch.sync()
        else:
            runtime.set_battery_pending(
                profile="cost_savings", reserve=20, sub_type=None
            )

    if stage == "permission":

        async def permission():
            change_context()

        runtime.async_assert_battery_profile_write_allowed = AsyncMock(
            side_effect=permission
        )
    else:

        async def fetch(*args, **kwargs):
            change_context()
            if stage == "profile":
                return {"data": {"profile": "backup_only"}}
            if stage == "status":
                # Preserve the newly observed state rather than undoing it.
                return {
                    "storages": [
                        {"id": "a", "battery_mode": runtime.profile_mismatch.live}
                    ]
                }
            return {"stormAlerts": []}

        setattr(
            coord.client,
            {
                "profile": "storm_guard_profile",
                "status": "battery_status",
                "alert": "storm_guard_alert",
            }[stage],
            AsyncMock(side_effect=fetch),
        )
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] in {FlowResultType.ABORT, FlowResultType.CREATE_ENTRY}
    coord.client.set_battery_profile.assert_not_awaited()


async def test_retry_rejects_changed_displayed_reserve(mismatch_repair):
    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "retry_profile"}
    )
    coord.battery_runtime.remember_battery_reserve("self-consumption", 25)
    result = await manager.async_configure(result["flow_id"], {})
    assert result["reason"] == "request_changed"
    coord.client.set_battery_profile.assert_not_awaited()


async def test_retry_failure_keeps_issue_and_allows_explicit_retry(mismatch_repair):
    coord, clock, manager = mismatch_repair
    coord.client.set_battery_profile.side_effect = TimeoutError()
    result = await start_repair(coord, manager)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "retry_profile"}
    )
    result = await manager.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cannot_retry"}
    assert not coord.battery_profile_pending
    coord.client.set_battery_profile.side_effect = None
    coord._battery_profile_last_write_mono = None
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert coord.client.set_battery_profile.await_count == 2


@pytest.mark.parametrize(
    "change", ["stale", "unknown", "match", "pending", "backend_pending", "no_entry"]
)
async def test_mismatch_is_cleared_without_trusting_missing_or_settling_state(
    mismatch_repair, change
):
    coord, clock, manager = mismatch_repair
    mismatch = coord.battery_runtime.profile_mismatch
    if change == "stale":
        clock[0] += 601
    elif change == "unknown":
        mismatch.note_live("new-mode")
    elif change == "match":
        mismatch.note_live("backup_only")
    elif change == "pending":
        coord.battery_runtime.set_battery_pending(
            profile="cost_savings", reserve=20, sub_type=None
        )
    elif change == "backend_pending":
        coord._battery_backend_profile_update_pending = True
    else:
        # Unreported entry-less coordinators cannot create cross-entry issues.
        mismatch.reset()
        coord.config_entry = None
    mismatch.sync()
    assert mismatch.identity is None
    assert not mismatch.diagnostics()["repair_reported"]


async def test_dwell_requires_repeated_reads_from_both_sources(mismatch_repair):
    coord, clock, manager = mismatch_repair
    mismatch = coord.battery_runtime.profile_mismatch
    mismatch.reset()
    mismatch.sync()
    clock[0] += 600
    mismatch.sync()
    assert not mismatch.diagnostics()["repair_reported"]
    mismatch.note_configured("backup_only")
    mismatch.note_live("self-consumption")
    mismatch.sync()
    assert mismatch.diagnostics()["repair_reported"]


async def test_live_confirmation_keeps_actual_config_observation(mismatch_repair):
    coord, clock, manager = mismatch_repair
    runtime = coord.battery_runtime
    runtime.set_battery_pending(
        profile="self-consumption",
        reserve=5,
        sub_type=None,
        require_exact_settings=False,
    )
    runtime.parse_battery_profile_payload({"data": {"profile": "backup_only"}})
    runtime.parse_battery_status_payload(
        {"storages": [{"id": "a", "battery_mode": "Self-Consumption"}]}
    )
    assert not coord.battery_profile_pending
    assert coord._battery_profile == "self-consumption"
    assert runtime.profile_mismatch.configured == "backup_only"
    assert runtime.profile_mismatch.live == "self-consumption"


async def test_invalid_and_conflicting_status_cannot_supply_retry_target(
    mismatch_repair,
):
    coord, clock, manager = mismatch_repair
    runtime = coord.battery_runtime
    runtime.parse_battery_status_payload(
        {
            "storages": [
                {"id": "a", "battery_mode": "Self-Consumption"},
                {"id": "b", "battery_mode": "Full Backup"},
            ]
        }
    )
    assert runtime.profile_mismatch.live is None
    runtime.parse_battery_status_payload(None)
    assert runtime.profile_mismatch.live is None


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"entry_id": 1, "identity": "x"},
        {"entry_id": "missing", "identity": 1},
    ],
)
async def test_invalid_mismatch_repair_context_cannot_write(hass, data):
    flow = await async_create_fix_flow(hass, "battery_profile_mismatch_missing", data)
    flow.hass = hass
    assert (await flow.async_step_init())["reason"] == "entry_unavailable"


async def test_unloaded_repair_entry_cannot_write(mismatch_repair, hass):
    coord, clock, manager = mismatch_repair
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, coord.battery_runtime.profile_mismatch.issue_id
    )
    flow = await async_create_fix_flow(
        hass, coord.battery_runtime.profile_mismatch.issue_id, issue.data
    )
    flow.hass = hass
    coord.config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)
    assert (await flow.async_step_init())["reason"] == "entry_unavailable"
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.parametrize("change", ["reserve", "alert", "unload", "reload"])
async def test_permission_read_revalidates_displayed_settings_and_entry(
    mismatch_repair, change
):
    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "retry_profile"}
    )

    async def change_permission():
        if change == "reserve":
            coord.battery_runtime.remember_battery_reserve("self-consumption", 25)
        elif change == "alert":
            coord._storm_alert_active = True
        elif change == "unload":
            coord.config_entry._async_set_state(
                coord.hass, ConfigEntryState.NOT_LOADED, None
            )
        else:
            coord.config_entry.runtime_data = EnphaseRuntimeData(
                coordinator=EnphaseCoordinator(
                    coord.hass,
                    dict(coord.config_entry.data),
                    config_entry=coord.config_entry,
                )
            )

    coord.battery_runtime.async_assert_battery_profile_write_allowed = AsyncMock(
        side_effect=change_permission
    )
    result = await manager.async_configure(result["flow_id"], {})
    if change in {"reserve", "alert"}:
        assert result["errors"] == {"base": "cannot_retry"}
    else:
        assert result["type"] == FlowResultType.ABORT
    coord.client.set_battery_profile.assert_not_awaited()


async def test_reserve_changed_by_fresh_check_requires_new_confirmation(
    mismatch_repair,
):
    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "retry_profile"}
    )
    coord.client.storm_guard_profile.return_value["data"][
        "previousBatteryBackupPercentage"
    ] = {"self-consumption": 25}
    result = await manager.async_configure(result["flow_id"], {})
    assert result["reason"] == "request_changed"
    coord.client.set_battery_profile.assert_not_awaited()


async def test_runtime_retry_rejects_unsupported_profile(mismatch_repair):
    from homeassistant.exceptions import ServiceValidationError

    coord, clock, manager = mismatch_repair
    coord._storm_alert_active = False
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_retry_mismatched_profile(
            profile_key="unsupported",
            reserve=5,
            sub_type=None,
            identity=coord.battery_runtime.profile_mismatch.identity,
        )
    coord.client.set_battery_profile.assert_not_awaited()


async def test_reload_during_profile_check_does_not_use_replacement(mismatch_repair):
    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)

    async def replace(*args, **kwargs):
        coord.config_entry.runtime_data = EnphaseRuntimeData(
            coordinator=EnphaseCoordinator(
                coord.hass,
                dict(coord.config_entry.data),
                config_entry=coord.config_entry,
            )
        )
        return {"data": {"profile": "backup_only"}}

    coord.client.storm_guard_profile.side_effect = replace
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "check_again"}
    )
    assert result["reason"] == "entry_unavailable"
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.parametrize("step", ["check_again", "retry_profile"])
async def test_stale_repair_menu_cannot_read_or_write_after_unload(
    mismatch_repair, step
):
    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)
    coord.mark_runtime_stopped()
    coord.client.storm_guard_profile.reset_mock()
    coord.client.battery_status.reset_mock()
    result = await manager.async_configure(result["flow_id"], {"next_step_id": step})
    assert result["reason"] == "entry_unavailable"
    coord.client.storm_guard_profile.assert_not_awaited()
    coord.client.battery_status.assert_not_awaited()
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.parametrize(
    "other", [{"id": "b", "battery_mode": "unknown"}, {"id": "b"}, None]
)
async def test_incomplete_battery_modes_cannot_authorize_retry(mismatch_repair, other):
    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "retry_profile"}
    )
    coord.client.battery_status.return_value["storages"].append(other)
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert coord.battery_runtime.profile_mismatch.live is None
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.parametrize("stage", ["before", "permission"])
async def test_retry_cannot_overlap_storm_alert_opt_in(mismatch_repair, stage):
    from datetime import timedelta
    from homeassistant.util import dt as dt_util

    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "retry_profile"}
    )
    coord.client.storm_guard_alert.return_value = {
        "stormAlerts": [
            {
                "id": "a",
                "name": "Heat Advisory",
                "status": "opted-out",
                "endTime": (dt_util.utcnow() + timedelta(hours=2)).timestamp() * 1000,
            }
        ]
    }
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_release(**kwargs):
        entered.set()
        await release.wait()

    coord.client.opt_in_storm_alert = AsyncMock(side_effect=wait_for_release)
    task = None

    async def start_opt_in():
        nonlocal task
        task = asyncio.create_task(coord.async_opt_in_all_storm_alerts())
        await entered.wait()

    if stage == "before":
        await start_opt_in()
    else:
        coord.battery_runtime.async_assert_battery_profile_write_allowed = AsyncMock(
            side_effect=start_opt_in
        )
    try:
        result = await manager.async_configure(result["flow_id"], {})
        assert result["errors"] == {"base": "cannot_retry"}
        coord.client.set_battery_profile.assert_not_awaited()
    finally:
        release.set()
        if task is not None:
            await task


@pytest.mark.parametrize("step", ["check_again", "retry_profile"])
@pytest.mark.parametrize("live_mode", ["Self-Consumption", "Full Backup"])
async def test_repair_refreshes_both_sources_before_resolving(
    mismatch_repair, step, live_mode
):
    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)
    if step == "retry_profile":
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption"}
    }
    coord.client.battery_status.return_value = {
        "storages": [{"id": "a", "battery_mode": live_mode}]
    }
    coord.client.battery_status.reset_mock()
    result = await manager.async_configure(
        result["flow_id"], {} if step == "retry_profile" else {"next_step_id": step}
    )
    coord.client.battery_status.assert_awaited_once_with()
    if live_mode == "Full Backup":
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "request_changed"
        assert coord.battery_runtime.profile_mismatch.live == "backup_only"
    else:
        assert result["type"] == FlowResultType.CREATE_ENTRY
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.parametrize("step", ["check_again", "retry_profile"])
@pytest.mark.parametrize("failed_source", ["profile", "status"])
async def test_repair_does_not_resolve_from_agreement_with_failed_source(
    mismatch_repair, step, failed_source
):
    coord, clock, manager = mismatch_repair
    result = await start_repair(coord, manager)
    if step == "retry_profile":
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    if failed_source == "status":
        coord.client.storm_guard_profile.return_value = {
            "data": {"profile": "self-consumption"}
        }
        coord.client.battery_status.side_effect = aiohttp.ClientError()
    else:
        coord.client.storm_guard_profile.side_effect = aiohttp.ClientError()
        coord.client.battery_status.return_value = {
            "storages": [{"id": "a", "battery_mode": "Full Backup"}]
        }
    result = await manager.async_configure(
        result["flow_id"], {} if step == "retry_profile" else {"next_step_id": step}
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "check_failed"
    coord.client.set_battery_profile.assert_not_awaited()
