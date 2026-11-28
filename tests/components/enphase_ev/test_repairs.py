"""Exercise battery profile recovery through Home Assistant's Repair manager."""

import asyncio
from datetime import timedelta
import time
from unittest.mock import ANY, AsyncMock

import aiohttp
import pytest
from freezegun import freeze_time
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.enphase_ev.api import (
    EnphaseEVClient,
    EnphaseLoginWallUnauthorized,
    Unauthorized,
)
from custom_components.enphase_ev.const import DOMAIN, ISSUE_BATTERY_PROFILE_PENDING
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.number import BatteryReserveNumber
from custom_components.enphase_ev.repairs import async_create_fix_flow
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.switch import SavingsUseBatteryAfterPeakSwitch

RESERVE_BOUNDS = {
    "batteryBackupPercentageMin": 10,
    "batteryBackupPercentageMax": 90,
}


@pytest.fixture
async def battery_repair_manager(hass, config_entry):
    coord = EnphaseCoordinator(hass, dict(config_entry.data), config_entry=config_entry)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    config_entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    hass.config.components.add(DOMAIN)
    assert await async_setup_component(hass, "repairs", {})
    yield coord, repairs_flow_manager(hass)
    coord.control_updates.cleanup()
    config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)


@pytest.fixture
def profile_repair(battery_repair_manager):
    coord, manager = battery_repair_manager
    coord.battery_runtime.set_battery_pending(
        profile="cost_savings", reserve=20, sub_type=None
    )
    coord._battery_pending_requested_at = dt_util.utcnow() - timedelta(
        minutes=11
    )  # noqa: SLF001
    coord._battery_pending_requested_mono = time.monotonic() - 660  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(
        return_value={"data": {"userDetails": {"isOwner": True}}}
    )
    coord.client.cancel_battery_profile_update = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.battery_runtime.async_refresh_battery_settings = AsyncMock(return_value=True)
    coord.battery_runtime.async_refresh_storm_guard_profile = AsyncMock()
    coord.diagnostics.sync_battery_profile_pending_issue()
    issue_id = coord.diagnostics._repair_issue_id(
        ISSUE_BATTERY_PROFILE_PENDING
    )  # noqa: SLF001
    return coord, issue_id, manager


async def start(manager, issue_id):
    return await manager.async_init(DOMAIN, context={"issue_id": issue_id})


@pytest.fixture
async def reserve_repair(battery_repair_manager):
    coord, manager = battery_repair_manager
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})
    coord.async_request_refresh = AsyncMock()
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {"profile": "self-consumption", "batteryBackupPercentage": 20}
        }
    )
    await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": RESERVE_BOUNDS}
    )
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    number = BatteryReserveNumber(coord)
    assert number.native_value == 20
    return coord, manager, number


async def open_overdue_reserve_repair(hass, coord, manager, clock):
    clock.tick(timedelta(seconds=601))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    coord.diagnostics.sync_battery_profile_pending_issue()
    issue_id = coord.diagnostics._repair_issue_id(
        ISSUE_BATTERY_PROFILE_PENDING
    )  # noqa: SLF001
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    return issue_id, await start(manager, issue_id)


async def test_overdue_issue_has_scoped_fixable_diagnostic_context(
    hass, profile_repair
):
    coord, issue_id, manager = profile_repair
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue_id != ISSUE_BATTERY_PROFILE_PENDING
    assert issue.is_fixable is True
    assert issue.data["entry_id"] == coord.config_entry.entry_id
    assert issue.data["requested_at"] == coord.battery_pending_requested_at.isoformat()
    assert "site_metrics" in issue.data
    assert issue.translation_placeholders["pending_timeout_minutes"] == "10"
    result = await start(manager, issue_id)
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["check_again", "cancel_change"]
    assert result["description_placeholders"]["site_id"] == coord.site_id
    assert result["description_placeholders"]["pending_timeout_minutes"] == "10"
    coord.client.cancel_battery_profile_update.assert_not_awaited()


async def test_check_again_reads_fresh_and_keeps_unresolved_issue(hass, profile_repair):
    coord, issue_id, manager = profile_repair
    result = await start(manager, issue_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "check_again"}
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "pending"
    coord.battery_runtime.async_refresh_battery_settings.assert_awaited_once_with(
        force=True
    )
    coord.battery_runtime.async_refresh_storm_guard_profile.assert_awaited_once_with(
        force=True
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert coord.battery_profile_pending
    coord.client.cancel_battery_profile_update.assert_not_awaited()


@pytest.mark.parametrize(
    "reserve_feedback",
    [
        {},
        {"batteryBackupPercentage": None},
        {"batteryBackupPercentage": "unknown"},
        {"batteryBackupPercentage": -1},
        {"batteryBackupPercentage": 101},
        {"batteryBackupPercentage": 5},
        {"batteryBackupPercentage": 95},
    ],
    ids=[
        "omitted",
        "null",
        "invalid",
        "below_range",
        "above_range",
        "below_site_minimum",
        "above_site_maximum",
    ],
)
@pytest.mark.parametrize("backend_pending", [False, True])
async def test_reserve_repair_waits_for_explicit_profile_reserve(
    hass, reserve_repair, reserve_feedback, backend_pending
):
    coord, manager, number = reserve_repair
    profile = {"profile": "self-consumption"}
    with freeze_time(real_asyncio=True) as clock:
        await number.async_set_native_value(30)
        coord.client.storm_guard_profile.return_value = {
            "data": {
                **profile,
                **reserve_feedback,
                "isBatteryChangePending": backend_pending,
            }
        }
        await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)
        assert coord.battery_profile_pending
        assert number.native_value == 20
        assert number.extra_state_attributes["requested_reserve_percentage"] == 30
        assert number.extra_state_attributes["control_update_status"] == "pending"
        issue_id, result = await open_overdue_reserve_repair(
            hass, coord, manager, clock
        )
        assert number.extra_state_attributes["control_update_status"] == "unconfirmed"

        coord.client.battery_settings_details = AsyncMock(
            return_value={
                "data": {**profile, **RESERVE_BOUNDS, "batteryBackupPercentage": 30}
            }
        )
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "check_again"}
        )
        assert result["step_id"] == "pending"
        assert coord.battery_profile_pending
        assert number.native_value == 20
        assert number.extra_state_attributes["control_update_status"] == "unconfirmed"

        coord.client.storm_guard_profile.return_value = {
            "data": {
                **profile,
                "batteryBackupPercentage": "30",
                "isBatteryChangePending": backend_pending,
            }
        }
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "check_again"}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert not coord.battery_profile_pending
    assert number.native_value == 30
    assert number.extra_state_attributes["backend_pending"] is backend_pending
    assert number.extra_state_attributes["pending"] is backend_pending
    coord.client.set_battery_settings_compat.assert_awaited_once_with(
        {"batteryBackupPercentage": 30},
        merged_payload=True,
        strip_devices=True,
        on_accepted_profile=ANY,
    )


async def test_prewrite_profile_response_cannot_confirm_reserve(reserve_repair):
    coord, _manager, number = reserve_repair
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_profile(*, locale):
        started.set()
        await release.wait()
        return {"data": {"profile": "self-consumption", "batteryBackupPercentage": 30}}

    coord.client.storm_guard_profile.side_effect = delayed_profile
    read = asyncio.create_task(
        coord.battery_runtime.async_refresh_storm_guard_profile(force=True)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await number.async_set_native_value(30)
    finally:
        release.set()
        applied = await read
    assert applied is False
    assert coord.battery_profile_pending
    assert number.native_value == 20
    assert number.extra_state_attributes["requested_reserve_percentage"] == 30


@pytest.mark.parametrize(
    "data",
    [
        {"profile": "self-consumption", "batteryBackupPercentage": None},
        {"profile": "self-consumption", "batteryBackupPercentage": 30},
        {"batteryBackupPercentage": 70},
    ],
    ids=["null", "matching_target", "missing_profile"],
)
async def test_profile_read_before_reserve_acknowledgement(reserve_repair, data):
    coord, _manager, number = reserve_repair
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_write(*_args, **_kwargs):
        started.set()
        await release.wait()
        return {}

    coord.client.set_battery_settings_compat.side_effect = delayed_write
    write = asyncio.create_task(number.async_set_native_value(30))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        coord.client.storm_guard_profile.return_value = {"data": data}
        await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)
        assert number.native_value == 20
    finally:
        release.set()
        await write
    assert coord.battery_profile_pending
    assert number.extra_state_attributes["control_update_status"] == "pending"


@pytest.mark.parametrize("preflight_reserve", [20, 95])
async def test_reserve_confirmation_uses_profile_at_write_dispatch(
    reserve_repair, monkeypatch, preflight_reserve
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    started = asyncio.Event()
    release = asyncio.Event()

    async def validate_access():
        started.set()
        await release.wait()

    monkeypatch.setattr(
        runtime, "async_assert_battery_profile_write_allowed", validate_access
    )
    write = asyncio.create_task(number.async_set_native_value(30))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        coord.client.storm_guard_profile.return_value = {
            "data": {
                "profile": "cost_savings",
                "batteryBackupPercentage": preflight_reserve,
                "operationModeSubType": "",
                **RESERVE_BOUNDS,
            }
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert number.native_value == 20
    finally:
        release.set()
        await write

    coord.client.storm_guard_profile.return_value["data"][
        "batteryBackupPercentage"
    ] = 30
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30
    assert number.extra_state_attributes["control_update_status"] == "confirmed"


@pytest.mark.parametrize(
    ("settings_base", "authoritative_profile"),
    [
        ({}, {"profile": "cost_savings", "operationModeSubType": "prioritize-energy"}),
        (
            {"profile": {}},
            {"profile": "cost_savings", "operationModeSubType": "prioritize-energy"},
        ),
        ({"profile": "self-consumption"}, {"profile": "self-consumption"}),
        (
            {"profile": "cost_savings", "operationModeSubType": ""},
            {"profile": "cost_savings", "operationModeSubType": ""},
        ),
        (
            {"profile": "cost_savings", "operationModeSubType": "regional-saving"},
            {"profile": "cost_savings", "operationModeSubType": "regional-saving"},
        ),
        (
            {"profile": "cost_savings", "operationModeSubType": " \t"},
            {"profile": "cost_savings", "operationModeSubType": "prioritize-energy"},
        ),
        (
            {"profile": "ai_optimisation"},
            {"profile": "ai_optimisation", "operationModeSubType": "prioritize-energy"},
        ),
    ],
    ids=[
        "no_wire_profile",
        "malformed_wire_profile",
        "different_profile",
        "explicit_savings_off",
        "regional_savings_subtype",
        "malformed_wire_subtype",
        "ai_default",
    ],
)
async def test_reserve_confirmation_matches_profile_sent_by_compatibility_client(
    reserve_repair, settings_base, authoritative_profile
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    coord.client.storm_guard_profile.return_value = {
        "data": {
            "profile": "cost_savings",
            "operationModeSubType": "prioritize-energy",
            "batteryBackupPercentage": 20,
        }
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    coord.client._remember_battery_config_write_base(  # noqa: SLF001
        "battery_settings", {"data": settings_base}
    )
    coord.client.set_battery_settings_compat = (
        EnphaseEVClient.set_battery_settings_compat.__get__(coord.client)
    )
    coord.client._json = AsyncMock(return_value={})  # noqa: SLF001
    await number.async_set_native_value(30)

    args, kwargs = coord.client._json.await_args  # noqa: SLF001
    assert args[0] == "PUT"
    assert kwargs["json"] == {**settings_base, "batteryBackupPercentage": 30}
    if "operationModeSubType" in settings_base:
        for incompatible in ({}, "another-regional-saving", " \t"):
            coord.client.storm_guard_profile.return_value = {
                "data": {
                    **authoritative_profile,
                    "batteryBackupPercentage": 30,
                    "operationModeSubType": incompatible,
                }
            }
            await runtime.async_refresh_storm_guard_profile(force=True)
            assert coord.battery_profile_pending
            assert number.native_value == 20
    coord.client.storm_guard_profile.return_value = {
        "data": {**authoritative_profile, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30
    assert number.extra_state_attributes["control_update_status"] == "confirmed"


async def test_reserve_accepts_explicit_savings_subtype_when_put_omits_it(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    coord.client._remember_battery_config_write_base(  # noqa: SLF001
        "battery_settings", {"data": {"profile": "cost_savings"}}
    )
    coord.client.set_battery_settings_compat = (
        EnphaseEVClient.set_battery_settings_compat.__get__(coord.client)
    )
    coord.client._json = AsyncMock(return_value={})  # noqa: SLF001

    await number.async_set_native_value(30)
    assert coord.client._json.await_args.kwargs["json"] == {  # noqa: SLF001
        "profile": "cost_savings",
        "batteryBackupPercentage": 30,
    }
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "cost_savings", "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    assert number.native_value == 20

    coord.client.storm_guard_profile.return_value["data"][
        "operationModeSubType"
    ] = "prioritize-energy"
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert coord.savings_use_battery_after_peak
    assert number.native_value == 30


@pytest.mark.parametrize("known_subtype", [True, False])
async def test_reserve_matches_observed_regional_ai_subtype(
    reserve_repair, known_subtype
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    profile = {"profile": "ai_optimisation", "operationModeSubType": "regional-ai"}
    initial = profile if known_subtype else {"profile": "ai_optimisation"}
    coord.client.storm_guard_profile.return_value = {
        "data": {**initial, "batteryBackupPercentage": 20}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    await number.async_set_native_value(30)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


async def test_matching_settings_limits_follow_the_accepted_wire_profile(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    savings = {"profile": "cost_savings", "operationModeSubType": ""}
    coord.client._remember_battery_config_write_base(  # noqa: SLF001
        "battery_settings", {"data": savings}
    )
    coord.client.set_battery_settings_compat = (
        EnphaseEVClient.set_battery_settings_compat.__get__(coord.client)
    )
    coord.client._json = AsyncMock(return_value={})  # noqa: SLF001
    await number.async_set_native_value(30)
    coord.client.battery_settings_details.return_value = {
        "data": {**savings, "batteryBackupPercentageMax": 25}
    }
    await runtime.async_refresh_battery_settings(force=True)
    coord.client.storm_guard_profile.return_value = {
        "data": {**savings, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    assert number.native_value == 20
    coord.client.storm_guard_profile.return_value["data"][
        "batteryBackupPercentageMax"
    ] = 90
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


async def test_old_settings_limits_do_not_follow_a_configured_mode_change(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    coord.client.battery_settings_details.return_value = {
        "data": {"profile": "self-consumption", "batteryBackupPercentageMax": 25}
    }
    await runtime.async_refresh_battery_settings(force=True)
    savings = {"profile": "cost_savings", "operationModeSubType": ""}
    coord.client.storm_guard_profile.return_value = {
        "data": {**savings, "batteryBackupPercentage": 20}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    coord.client.battery_settings_details.return_value = {"data": savings}
    await runtime.async_refresh_battery_settings(force=True)

    await number.async_set_native_value(30)
    coord.client.storm_guard_profile.return_value = {
        "data": {**savings, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


async def test_new_site_floor_invalidates_contradictory_scoped_fallback(reserve_repair):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    profile = {"profile": "self-consumption"}
    coord.client.battery_settings_details.return_value = {
        "data": {**profile, "batteryBackupPercentageMax": 25}
    }
    await runtime.async_refresh_battery_settings(force=True)
    coord.client.battery_settings_details.return_value = {
        "data": {"batteryBackupPercentageMin": 30}
    }
    await runtime.async_refresh_battery_settings(force=True)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 35}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert number.native_value == 35


@pytest.mark.parametrize("control", ["system_profile", "reserve"])
async def test_full_backup_profile_readback_uses_fixed_reserve(reserve_repair, control):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    if control == "system_profile":
        runtime.parse_battery_site_settings_payload({"data": {"showFullBackup": True}})
        coord.client.set_battery_profile = AsyncMock(return_value={})
        await runtime.async_set_system_profile("backup_only")
    else:
        coord.client.battery_settings_details.return_value = {
            "data": {"batteryBackupPercentageMax": 100}
        }
        await runtime.async_refresh_battery_settings(force=True)

        async def accept_full_backup(*_args, on_accepted_profile, **_kwargs):
            on_accepted_profile("backup_only", None)
            return {}

        coord.client.set_battery_settings_compat.side_effect = accept_full_backup
        await number.async_set_native_value(100)

    coord.client.storm_guard_profile.return_value = {
        "data": {
            "profile": "backup_only",
            "batteryBackupPercentage": 100,
            "batteryBackupPercentageMax": 90,
        }
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 100
    if control == "reserve":
        assert number.extra_state_attributes["control_update_status"] == "confirmed"


async def test_malformed_settings_subtype_cannot_supply_savings_evidence(
    battery_repair_manager,
):
    coord, _manager = battery_repair_manager
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.client._json = AsyncMock(return_value={})  # noqa: SLF001
    settings = {"profile": "cost_savings", "operationModeSubType": {}}
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {**settings, "batteryBackupPercentage": 20}}
    )
    await runtime.async_refresh_battery_settings(force=True)
    coord.client._remember_battery_config_write_base(  # noqa: SLF001
        "battery_settings", {"data": settings}
    )
    number = BatteryReserveNumber(coord)
    await number.async_set_native_value(30)

    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {"profile": "cost_savings", "batteryBackupPercentage": 30}
        }
    )
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    assert number.native_value == 20
    coord.client.storm_guard_profile.return_value["data"]["operationModeSubType"] = ""
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


async def test_partial_profile_uses_valid_same_profile_settings_subtype(reserve_repair):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    profile = {"profile": "cost_savings"}
    coord.client.storm_guard_profile.return_value = {"data": profile}
    await runtime.async_refresh_storm_guard_profile(force=True)
    coord.client.battery_settings_details.return_value = {
        "data": {**profile, "operationModeSubType": "prioritize-energy"}
    }
    await runtime.async_refresh_battery_settings(force=True)
    coord.client.battery_settings_details.return_value = {"data": profile}
    await runtime.async_refresh_battery_settings(force=True)

    with freeze_time(real_asyncio=True) as clock:
        await number.async_set_native_value(30)
        coord.client.storm_guard_profile.return_value = {
            "data": {**profile, "batteryBackupPercentage": 30}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert not coord.battery_profile_pending
        assert number.native_value == 30

        coord.client.battery_settings_details.return_value = {
            "data": {"operationModeSubType": ""}
        }
        await runtime.async_refresh_battery_settings(force=True)
        clock.tick(timedelta(seconds=3))
        await number.async_set_native_value(40)
        coord.client.storm_guard_profile.return_value = {
            "data": {**profile, "batteryBackupPercentage": 40}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert coord.battery_profile_pending
        assert number.native_value == 30
        coord.client.storm_guard_profile.return_value["data"][
            "operationModeSubType"
        ] = ""
        await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 40


@pytest.mark.parametrize("source", ["profile", "settings", None])
async def test_reserve_does_not_use_settings_subtype_from_previous_configuration(
    battery_repair_manager, source
):
    coord, _manager = battery_repair_manager
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})
    savings = {"profile": "cost_savings", "batteryBackupPercentage": 20}
    if source is not None:
        coord.client.battery_settings_details = AsyncMock(
            return_value={
                "data": {**savings, "operationModeSubType": "prioritize-energy"}
            }
        )
        await runtime.async_refresh_battery_settings(force=True)
        self_consumption = {"data": {"profile": "self-consumption"}}
        if source == "profile":
            coord.client.storm_guard_profile = AsyncMock(return_value=self_consumption)
            await runtime.async_refresh_storm_guard_profile(force=True)
        else:
            coord.client.battery_settings_details.return_value = self_consumption
            await runtime.async_refresh_battery_settings(force=True)
    coord.client.storm_guard_profile = AsyncMock(return_value={"data": savings})
    await runtime.async_refresh_storm_guard_profile(force=True)

    number = BatteryReserveNumber(coord)
    await number.async_set_native_value(30)
    coord.client.storm_guard_profile.return_value = {
        "data": {**savings, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    assert number.native_value == 20

    coord.client.storm_guard_profile.return_value["data"]["operationModeSubType"] = ""
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


async def test_settings_profile_changes_discard_prior_profile_subtype(reserve_repair):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    savings = {"profile": "cost_savings", "batteryBackupPercentage": 20}
    coord.client.storm_guard_profile.return_value = {
        "data": {**savings, "operationModeSubType": "prioritize-energy"}
    }
    with freeze_time(real_asyncio=True) as clock:
        await runtime.async_refresh_storm_guard_profile(force=True)
        clock.tick(timedelta(minutes=10))
        coord.client.battery_settings_details.return_value = {
            "data": {"profile": "self-consumption"}
        }
        await runtime.async_refresh_battery_settings(force=True)
        assert coord.battery_profile == "self-consumption"
        coord.client.battery_settings_details.return_value = {"data": savings}
        await runtime.async_refresh_battery_settings(force=True)
        assert coord.battery_profile == "cost_savings"
        await number.async_set_native_value(30)

        coord.client.storm_guard_profile.return_value = {
            "data": {**savings, "batteryBackupPercentage": 30}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert coord.battery_profile_pending
        assert number.native_value == 20

        coord.client.storm_guard_profile.return_value["data"][
            "operationModeSubType"
        ] = ""
        await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


@pytest.mark.parametrize("partial_profile", [False, True])
async def test_recent_settings_subtype_supersedes_expired_profile_evidence(
    reserve_repair, partial_profile
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    savings = {"profile": "cost_savings"}
    coord.client.storm_guard_profile.return_value = {
        "data": {
            **savings,
            "batteryBackupPercentage": 20,
            "operationModeSubType": "prioritize-energy",
        }
    }
    with freeze_time(real_asyncio=True) as clock:
        await runtime.async_refresh_storm_guard_profile(force=True)
        clock.tick(timedelta(seconds=601))
        if partial_profile:
            coord.client.storm_guard_profile.return_value = {
                "data": {**savings, "operationModeSubType": None}
            }
            await runtime.async_refresh_storm_guard_profile(force=True)
        coord.client.battery_settings_details.return_value = {
            "data": {**savings, "operationModeSubType": ""}
        }
        await runtime.async_refresh_battery_settings(force=True)
        coord.client.battery_settings_details.return_value = {"data": savings}
        await runtime.async_refresh_battery_settings(force=True)
        assert not SavingsUseBatteryAfterPeakSwitch(coord).is_on

        await number.async_set_native_value(30)
        coord.client.storm_guard_profile.return_value = {
            "data": {
                **savings,
                "batteryBackupPercentage": 30,
                "operationModeSubType": "",
            }
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


async def test_malformed_settings_bound_does_not_replace_reported_reserve(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    coord.client.battery_settings_details.return_value = {
        "data": {
            "profile": "self-consumption",
            "batteryBackupPercentage": 25,
            "batteryBackupPercentageMax": -1,
        }
    }
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert coord.battery_effective_backup_percentage == number.native_value == 25


async def test_partial_settings_reserve_respects_site_floor(battery_repair_manager):
    coord, _manager = battery_repair_manager
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {"profile": "self-consumption", "batteryBackupPercentage": 20}
        }
    )
    await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"batteryBackupPercentage": 0, "veryLowSocMin": 10}}
    )
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    number = BatteryReserveNumber(coord)
    assert number.native_min_value == 10
    assert coord.battery_effective_backup_percentage == number.native_value == 20


@pytest.mark.parametrize("confirm_first", [False, True])
@pytest.mark.parametrize("reported_floor", [5, -1, 101])
async def test_profile_readback_uses_updated_site_floor(
    battery_repair_manager, confirm_first, reported_floor
):
    coord, _manager = battery_repair_manager
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"veryLowSocMin": 10}}
    )
    await runtime.async_refresh_battery_settings(force=True)
    profile = {"profile": "self-consumption"}
    coord.client.storm_guard_profile = AsyncMock(
        return_value={"data": {**profile, "batteryBackupPercentage": 20}}
    )
    await runtime.async_refresh_storm_guard_profile(force=True)
    number = BatteryReserveNumber(coord)
    await number.async_set_native_value(30)

    if confirm_first:
        coord.client.storm_guard_profile.return_value = {
            "data": {**profile, "batteryBackupPercentage": 30}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert not coord.battery_profile_pending

    coord.client.battery_settings_details.return_value = {
        "data": {"veryLowSocMin": reported_floor}
    }
    await runtime.async_refresh_battery_settings(force=True)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 5}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    expected = 5 if reported_floor == 5 else (30 if confirm_first else 20)
    assert number.native_value == expected
    assert coord.battery_profile_pending is not confirm_first
    coord.client.storm_guard_profile.return_value["data"][
        "batteryBackupPercentage"
    ] = 30
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


@pytest.mark.parametrize("source", ["profile", "settings"])
async def test_malformed_configured_profile_during_reserve_access_check(
    reserve_repair, source
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = None  # noqa: SLF001

    async def read_access():
        malformed = {"data": {"profile": {}, "batteryBackupPercentage": 70}}
        if source == "profile":
            coord.client.storm_guard_profile.return_value = malformed
            await runtime.async_refresh_storm_guard_profile(force=True)
        else:
            coord.client.battery_settings_details.return_value = malformed
            await runtime.async_refresh_battery_settings(force=True)
        assert number.native_value == 20
        return {"data": {"userDetails": {"isOwner": True}}}

    coord.client.battery_site_settings = AsyncMock(side_effect=read_access)
    await number.async_set_native_value(30)
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption", "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


async def test_invalid_settings_limit_during_access_check_does_not_block_confirmation(
    battery_repair_manager,
):
    coord, _manager = battery_repair_manager
    runtime = coord.battery_runtime
    coord.async_request_refresh = AsyncMock()
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})
    profile = {"profile": "self-consumption"}
    coord.client.storm_guard_profile = AsyncMock(
        return_value={"data": {**profile, "batteryBackupPercentage": 20}}
    )
    await runtime.async_refresh_storm_guard_profile(force=True)

    async def read_access():
        coord.client.battery_settings_details = AsyncMock(
            return_value={"data": {"batteryBackupPercentageMax": -1}}
        )
        await runtime.async_refresh_battery_settings(force=True)
        return {"data": {"userDetails": {"isOwner": True}}}

    coord.client.battery_site_settings = AsyncMock(side_effect=read_access)
    number = BatteryReserveNumber(coord)
    await number.async_set_native_value(30)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30
    coord.client.set_battery_settings_compat.assert_awaited_once_with(
        {"batteryBackupPercentage": 30},
        merged_payload=True,
        strip_devices=True,
        on_accepted_profile=ANY,
    )


@pytest.mark.parametrize(
    ("profile", "initial_reserve", "profile_reserve", "expected"),
    [("backup_only", 100, None, 100), ("self-consumption", 20, 5, 20)],
)
async def test_settings_maximum_preserves_profile_reserve_floor(
    battery_repair_manager, profile, initial_reserve, profile_reserve, expected
):
    coord, _manager = battery_repair_manager
    runtime = coord.battery_runtime
    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": {
                "profile": profile,
                "batteryBackupPercentage": initial_reserve,
                "batteryBackupPercentageMax": 90,
                "veryLowSocMin": 10,
            }
        }
    )
    await runtime.async_refresh_battery_settings(force=True)
    assert coord.battery_effective_backup_percentage == initial_reserve
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {"profile": profile, "batteryBackupPercentage": profile_reserve}
        }
    )
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_effective_backup_percentage == expected


@pytest.mark.parametrize("source", ["profile", "settings", "settings_floor"])
async def test_impossible_reserve_bounds_are_not_reused(battery_repair_manager, source):
    coord, _manager = battery_repair_manager
    runtime = coord.battery_runtime
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"veryLowSocMin": 5, "batteryBackupPercentageMax": 90}}
    )
    await runtime.async_refresh_battery_settings(force=True)
    profile = {"profile": "self-consumption", "batteryBackupPercentage": 20}
    coord.client.storm_guard_profile = AsyncMock(return_value={"data": profile})
    await runtime.async_refresh_storm_guard_profile(force=True)
    impossible = {"data": {**profile, "batteryBackupPercentageMax": 4}}
    if source == "profile":
        coord.client.storm_guard_profile.return_value = impossible
        await runtime.async_refresh_storm_guard_profile(force=True)
    else:
        coord.client.battery_settings_details.return_value = (
            {"data": {"veryLowSocMin": 91}}
            if source == "settings_floor"
            else impossible
        )
        await runtime.async_refresh_battery_settings(force=True)

    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 25}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert BatteryReserveNumber(coord).native_value == 25


async def test_partial_settings_preserves_configured_full_backup_reserve(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    runtime.parse_battery_site_settings_payload({"data": {"showFullBackup": True}})
    coord.client.set_battery_profile = AsyncMock(return_value={})
    with freeze_time(real_asyncio=True) as clock:
        await runtime.async_set_system_profile("backup_only")
        coord.client.storm_guard_profile.return_value = {
            "data": {"profile": "backup_only", "batteryBackupPercentage": 100}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        clock.tick(timedelta(minutes=11))
        coord.client.battery_settings_details.return_value = {
            "data": {"batteryBackupPercentage": 25}
        }
        await runtime.async_refresh_battery_settings(force=True)
    assert coord.battery_effective_backup_percentage == number.native_value == 100


async def test_savings_toggle_keeps_legacy_settings_reserve_limits(reserve_repair):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    profile = {"profile": "cost_savings", "batteryBackupPercentage": 20}
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "operationModeSubType": ""}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    coord.client.set_battery_profile = AsyncMock(return_value={})
    await runtime.async_set_savings_use_battery_after_peak(True)
    coord.client.storm_guard_profile.return_value = {
        "data": {
            **profile,
            "operationModeSubType": "prioritize-energy",
            "batteryBackupPercentageMin": 25,
        }
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert coord.savings_use_battery_after_peak
    assert coord.battery_effective_backup_percentage == number.native_value == 20
    coord.client.storm_guard_profile.return_value["data"]["batteryBackupPercentage"] = 5
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_effective_backup_percentage == number.native_value == 20


@pytest.mark.parametrize("older_profile_subtype", [False, True])
async def test_savings_toggle_off_confirms_from_legacy_explicit_null(
    reserve_repair, older_profile_subtype
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    savings = {"profile": "cost_savings", "batteryBackupPercentage": 20}
    coord.client.set_battery_profile = AsyncMock(return_value={})
    entity = SavingsUseBatteryAfterPeakSwitch(coord)
    with freeze_time(real_asyncio=True) as clock:
        initial = (
            {**savings, "operationModeSubType": "prioritize-energy"}
            if older_profile_subtype
            else savings
        )
        coord.client.storm_guard_profile.return_value = {"data": initial}
        await runtime.async_refresh_storm_guard_profile(force=True)
        if older_profile_subtype:
            coord.client.battery_settings_details.return_value = {
                "data": {**savings, "operationModeSubType": ""}
            }
            await runtime.async_refresh_battery_settings(force=True)
            assert not entity.is_on
        await entity.async_turn_off()
        coord.client.storm_guard_profile.return_value = {
            "data": {**savings, "operationModeSubType": None}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert not coord.battery_profile_pending
        assert not entity.is_on
        assert (
            coord.control_updates.updates[
                ("savings_use_battery_after_peak", None)
            ].status
            == "confirmed"
        )
        if older_profile_subtype:
            clock.tick(timedelta(seconds=3))
            await number.async_set_native_value(30)
            coord.client.storm_guard_profile.return_value = {
                "data": {
                    **savings,
                    "batteryBackupPercentage": 30,
                    "operationModeSubType": "",
                }
            }
            await runtime.async_refresh_storm_guard_profile(force=True)
            assert not coord.battery_profile_pending
            assert number.native_value == 30


@pytest.mark.parametrize(
    "source",
    [
        "profile",
        "pending_settings",
        "after_reserve",
        "after_reserve_partial",
        "after_reserve_settings",
        "after_reserve_ai",
        "after_reserve_ai_partial",
        "after_reserve_ai_ignored_settings",
        "after_reserve_ai_missing_mode",
        "after_reserve_ai_overdue",
        "after_reserve_ai_inherited_mode",
    ],
)
async def test_legacy_profile_null_clears_subtype_for_next_mode_write(
    reserve_repair, source
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    runtime.parse_battery_site_settings_payload({"data": {"showSavingsMode": True}})
    coord.client.set_battery_profile = AsyncMock(return_value={})
    coord.client.storm_guard_profile.return_value = {
        "data": {
            "profile": "cost_savings",
            "batteryBackupPercentage": 20,
            "operationModeSubType": "prioritize-energy",
        }
    }
    inherited_mode = source.endswith("_inherited_mode")
    overdue = source.endswith("_overdue") or inherited_mode
    with freeze_time(real_asyncio=True) as clock:
        await runtime.async_refresh_storm_guard_profile(force=True)
        if inherited_mode:
            coord.client.battery_status = AsyncMock(
                return_value={
                    "storages": [
                        {
                            "id": "one",
                            "battery_mode": "Savings Mode",
                            "last_report": dt_util.utcnow().timestamp(),
                        }
                    ]
                }
            )
            await runtime.async_refresh_battery_status(force=True)
            await SavingsUseBatteryAfterPeakSwitch(coord).async_turn_on()
            await runtime.async_refresh_storm_guard_profile(force=True)
            assert not coord.battery_profile_pending
            coord.client.storm_guard_profile.return_value = {"data": {"profile": None}}
            await runtime.async_refresh_storm_guard_profile(force=True)
            clock.tick(timedelta(seconds=3))
        if source.startswith("after_reserve"):
            await number.async_set_native_value(30)
            if overdue:
                clock.tick(timedelta(seconds=601))
            else:
                coord.client.storm_guard_profile.return_value["data"][
                    "batteryBackupPercentage"
                ] = 30
                await runtime.async_refresh_storm_guard_profile(force=True)
                assert not coord.battery_profile_pending
        else:
            await runtime.async_set_system_profile("self-consumption")
        mode = "ai_optimisation" if "_ai" in source else "self-consumption"
        profile = {"profile": mode, "batteryBackupPercentage": 20}
        without_subtype = {**profile, "operationModeSubType": None}
        if source.endswith("_ignored_settings"):
            coord.client.battery_settings_details.return_value = {
                "data": {"profile": mode}
            }
            await runtime.async_refresh_battery_settings(force=True)
            assert coord.battery_profile == "cost_savings"
        elif source.endswith("_missing_mode"):
            coord.client.storm_guard_profile.return_value = {"data": {"profile": None}}
            await runtime.async_refresh_storm_guard_profile(force=True)
            assert coord.battery_profile == "cost_savings"
        if source in {"pending_settings", "after_reserve_settings"}:
            if source == "after_reserve_settings":
                clock.tick(timedelta(seconds=601))
            coord.client.battery_settings_details.return_value = {
                "data": without_subtype
            }
            await runtime.async_refresh_battery_settings(force=True)
        if source != "after_reserve_settings":
            coord.client.storm_guard_profile.return_value = {
                "data": (
                    profile
                    if source == "pending_settings" or source.endswith("_partial")
                    else without_subtype
                )
            }
            await runtime.async_refresh_storm_guard_profile(force=True)
        if source.endswith("_partial"):
            coord.client.storm_guard_profile.return_value = {
                "data": {"profile": None, "operationModeSubType": None}
            }
            await runtime.async_refresh_storm_guard_profile(force=True)
        if overdue:
            assert coord.battery_profile_pending
            assert not SavingsUseBatteryAfterPeakSwitch(coord).is_on
            coord.client.cancel_battery_profile_update = AsyncMock(return_value={})
            await runtime.async_cancel_pending_profile_change()
        assert not coord.battery_profile_pending
        assert not SavingsUseBatteryAfterPeakSwitch(coord).is_on
        clock.tick(timedelta(seconds=3))
        await runtime.async_set_system_profile("cost_savings")
    assert (
        coord.client.set_battery_profile.await_args.kwargs["operation_mode_sub_type"]
        == ""
    )


@pytest.mark.parametrize(
    "feedback", ["settings_reserve", "settings_null", "profile_null"]
)
async def test_legacy_mode_write_preserves_explicit_companion_feedback(
    reserve_repair, feedback
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    runtime.parse_battery_site_settings_payload({"data": {"showSavingsMode": True}})
    coord.client.set_battery_profile = AsyncMock(return_value={})
    coord.client.battery_settings_details.return_value = {
        "data": {"previousBatteryBackupPercentage": {"cost_savings": 30}}
    }
    await runtime.async_refresh_battery_settings(force=True)
    savings = {"profile": "cost_savings", "operationModeSubType": ""}
    reported_reserve = 30 if feedback == "settings_reserve" else None
    with freeze_time(real_asyncio=True) as clock:
        await runtime.async_set_system_profile("cost_savings")
        if feedback.startswith("settings"):
            coord.client.battery_settings_details.return_value = {
                "data": {**savings, "batteryBackupPercentage": reported_reserve}
            }
            await runtime.async_refresh_battery_settings(force=True)
            assert coord.battery_profile_pending
            assert number.native_value == reported_reserve

        profile = (
            {**savings, "batteryBackupPercentage": None}
            if feedback == "profile_null"
            else savings
        )
        coord.client.storm_guard_profile.return_value = {"data": profile}
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert not coord.battery_profile_pending
        assert number.native_value == reported_reserve
        clock.tick(timedelta(seconds=3))
        await SavingsUseBatteryAfterPeakSwitch(coord).async_turn_on()
    assert (
        coord.client.set_battery_profile.await_args.kwargs["battery_backup_percentage"]
        == 30
    )


@pytest.mark.parametrize("profile_key", ["self-consumption", "cost_savings"])
async def test_settings_read_started_while_pending_cannot_overwrite_confirmation(
    reserve_repair, profile_key
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    started = asyncio.Event()
    release = asyncio.Event()
    profile = {"profile": profile_key}
    settings_profile = profile
    if profile_key == "cost_savings":
        profile = {**profile, "operationModeSubType": ""}
        settings_profile = {**profile, "operationModeSubType": "prioritize-energy"}
        coord.client.storm_guard_profile.return_value = {
            "data": {**profile, "batteryBackupPercentage": 20}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
    savings_switch = SavingsUseBatteryAfterPeakSwitch(coord)

    async def delayed_settings():
        started.set()
        await release.wait()
        return {
            "data": {
                **settings_profile,
                "batteryBackupPercentage": 20,
                "veryLowSoc": 12,
            }
        }

    await number.async_set_native_value(30)
    coord.client.battery_settings_details.side_effect = delayed_settings
    read = asyncio.create_task(runtime.async_refresh_battery_settings(force=True))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        coord.client.storm_guard_profile.return_value = {
            "data": {**profile, "batteryBackupPercentage": 30}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert not coord.battery_profile_pending
        assert number.native_value == 30
    finally:
        release.set()
        await read
    assert coord.battery_shutdown_level == 12
    assert coord.battery_effective_backup_percentage == number.native_value == 30
    assert not savings_switch.is_on

    coord.client.battery_settings_details.side_effect = None
    coord.client.battery_settings_details.return_value = {
        "data": {**profile, "batteryBackupPercentage": 25}
    }
    await runtime.async_refresh_battery_settings(force=True)
    assert coord.battery_effective_backup_percentage == number.native_value == 25


@pytest.mark.parametrize("before_ack", [False, True])
async def test_pending_settings_subtype_cannot_poison_next_reserve_edit(
    reserve_repair, before_ack
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    savings = {"profile": "cost_savings"}
    coord.client.storm_guard_profile.return_value = {"data": savings}
    await runtime.async_refresh_storm_guard_profile(force=True)
    coord.client.battery_settings_details.return_value = {
        "data": {**savings, "operationModeSubType": ""}
    }
    await runtime.async_refresh_battery_settings(force=True)

    async def read_conflicting_settings(*_args, **_kwargs):
        coord.client.battery_settings_details.return_value = {
            "data": {**savings, "operationModeSubType": "prioritize-energy"}
        }
        await runtime.async_refresh_battery_settings(force=True)
        assert not SavingsUseBatteryAfterPeakSwitch(coord).is_on
        return {}

    with freeze_time(real_asyncio=True) as clock:
        if before_ack:
            coord.client.set_battery_settings_compat.side_effect = (
                read_conflicting_settings
            )
        await number.async_set_native_value(30)
        coord.client.set_battery_settings_compat.side_effect = None
        if not before_ack:
            await read_conflicting_settings()
        coord.client.storm_guard_profile.return_value = {
            "data": {**savings, "batteryBackupPercentage": 30}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert not coord.battery_profile_pending
        assert number.native_value == 30
        assert not SavingsUseBatteryAfterPeakSwitch(coord).is_on

        clock.tick(timedelta(seconds=3))
        await number.async_set_native_value(40)
        coord.client.storm_guard_profile.return_value = {
            "data": {
                **savings,
                "batteryBackupPercentage": 40,
                "operationModeSubType": "",
            }
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 40


async def test_older_settings_cannot_roll_back_reserve_confirmation_bounds(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    started = asyncio.Event()
    release = asyncio.Event()

    async def old_settings():
        started.set()
        await release.wait()
        return {"data": {"batteryBackupPercentageMin": 10}}

    await number.async_set_native_value(15)
    coord.client.battery_settings_details.side_effect = old_settings
    read = asyncio.create_task(runtime.async_refresh_battery_settings(force=True))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        coord.client.battery_settings_details.side_effect = None
        coord.client.battery_settings_details.return_value = {
            "data": {"batteryBackupPercentageMin": 20}
        }
        await runtime.async_refresh_battery_settings(force=True)
    finally:
        release.set()
        await read

    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption", "batteryBackupPercentage": 15}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    assert number.native_value == 20
    coord.client.storm_guard_profile.return_value["data"][
        "batteryBackupPercentageMin"
    ] = 10
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 15


@pytest.mark.parametrize("settings_profile", ["backup_only", {}, "", 42])
async def test_unrelated_settings_cannot_supply_bounds_for_overdue_reserve(
    reserve_repair, settings_profile
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    with freeze_time(real_asyncio=True) as clock:
        await number.async_set_native_value(30)
        clock.tick(timedelta(seconds=601))
        coord.client.battery_settings_details.return_value = {
            "data": {
                "profile": settings_profile,
                "batteryBackupPercentageMin": 100,
                "batteryBackupPercentageMax": 100,
            }
        }
        await runtime.async_refresh_battery_settings(force=True)
        coord.client.storm_guard_profile.return_value = {
            "data": {"profile": "self-consumption", "batteryBackupPercentage": 30}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


@pytest.mark.parametrize("source", ["profile", "settings"])
async def test_failed_reserve_put_leaves_next_read_available(reserve_repair, source):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    if source == "profile":
        fetcher = coord.client.storm_guard_profile
        refresh = runtime.async_refresh_storm_guard_profile
    else:
        fetcher = coord.client.battery_settings_details
        refresh = runtime.async_refresh_battery_settings

    async def rejected_write(*_args, **_kwargs):
        fetcher.return_value = {
            "data": {"profile": "self-consumption", "batteryBackupPercentage": 25}
        }
        await refresh(force=True)
        assert number.native_value == 20
        raise aiohttp.ClientConnectionError("mocked rejection")

    coord.client.set_battery_settings_compat.side_effect = rejected_write
    with pytest.raises(aiohttp.ClientConnectionError):
        await number.async_set_native_value(30)
    assert not coord.battery_profile_pending
    assert number.extra_state_attributes["control_update_status"] == "failed"

    assert await refresh()
    assert number.native_value == 25
    coord.client.set_battery_settings_compat.assert_awaited_once()


async def test_rejected_reserve_put_does_not_keep_subtype_across_mode_change(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    savings = {"profile": "cost_savings", "batteryBackupPercentage": 20}
    enabled = {"operationModeSubType": "prioritize-energy"}
    coord.client.storm_guard_profile.return_value = {"data": {**savings, **enabled}}
    await runtime.async_refresh_storm_guard_profile(force=True)

    async def rejected_write(*_args, **_kwargs):
        coord.client.storm_guard_profile.return_value = {
            "data": {"profile": "self-consumption"}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert coord.battery_profile == "self-consumption"
        raise aiohttp.ClientConnectionError("mocked rejection")

    with freeze_time(real_asyncio=True) as clock:
        coord.client.set_battery_settings_compat.side_effect = rejected_write
        with pytest.raises(aiohttp.ClientConnectionError):
            await number.async_set_native_value(30)

        coord.client.storm_guard_profile.return_value = {"data": savings}
        await runtime.async_refresh_storm_guard_profile(force=True)
        clock.tick(timedelta(seconds=3))
        coord.client.set_battery_settings_compat.side_effect = None
        await number.async_set_native_value(30)

        coord.client.storm_guard_profile.return_value = {
            "data": {**savings, "batteryBackupPercentage": 30}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert coord.battery_profile_pending
        assert number.native_value == 20
        coord.client.storm_guard_profile.return_value["data"].update(enabled)
        await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


@pytest.mark.parametrize("unrelated_settings", [False, True])
async def test_reserve_read_during_access_check_updates_display_if_write_is_rejected(
    reserve_repair, unrelated_settings
):
    coord, _manager, number = reserve_repair
    coord._battery_user_is_owner = None  # noqa: SLF001

    async def read_access():
        if unrelated_settings:
            coord.client.battery_settings_details.return_value = {
                "data": {
                    "profile": "cost_savings",
                    "batteryBackupPercentage": 70,
                    "batteryBackupPercentageMax": 100,
                }
            }
            await coord.battery_runtime.async_refresh_battery_settings(force=True)
            assert number.native_value == 20
        feedback = {} if unrelated_settings else {"batteryBackupPercentage": 25}
        coord.client.storm_guard_profile.return_value = {
            "data": {
                "profile": "self-consumption",
                "systemTask": True,
                **feedback,
            }
        }
        await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)
        return {"data": {"userDetails": {"isOwner": True}}}

    coord.client.battery_site_settings = AsyncMock(side_effect=read_access)
    with pytest.raises(ServiceValidationError):
        await number.async_set_native_value(30)
    assert coord.battery_system_task is True
    assert not coord.battery_profile_pending
    assert number.native_value == (20 if unrelated_settings else 25)
    coord.client.set_battery_settings_compat.assert_not_awaited()
    if unrelated_settings:
        coord.client.storm_guard_profile.return_value = {
            "data": {"profile": "self-consumption", "batteryBackupPercentage": 95}
        }
        await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)
        assert number.native_value == 20


@pytest.mark.parametrize("subtype", [None, " Prioritize-Energy "])
async def test_reserve_captures_configured_savings_subtype_at_dispatch(
    reserve_repair, subtype
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    profile = {"profile": "cost_savings", "operationModeSubType": "prioritize-energy"}
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 20}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    coord._battery_user_is_owner = None  # noqa: SLF001

    async def read_access():
        coord.client.storm_guard_profile.return_value = {
            "data": {"profile": "cost_savings", "operationModeSubType": subtype}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        return {"data": {"userDetails": {"isOwner": True}}}

    coord.client.battery_site_settings = AsyncMock(side_effect=read_access)
    await number.async_set_native_value(30)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30
    assert number.extra_state_attributes["control_update_status"] == "confirmed"


@pytest.mark.parametrize("malformed_subtype", [False, True])
async def test_partial_profile_preserves_savings_subtype_for_reserve(
    reserve_repair, malformed_subtype
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    coord.client.storm_guard_profile.return_value = {
        "data": {
            "profile": "cost_savings",
            "operationModeSubType": "prioritize-energy",
            "batteryBackupPercentage": 20,
        }
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    await number.async_set_native_value(30)

    profile = {"profile": "cost_savings", "batteryBackupPercentage": 30}
    incomplete = (
        {**profile, "operationModeSubType": {}}
        if malformed_subtype
        else {"systemTask": False}
    )
    coord.client.storm_guard_profile.return_value = {"data": incomplete}
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    savings_switch = SavingsUseBatteryAfterPeakSwitch(coord)
    assert savings_switch.is_on
    coord.client.storm_guard_profile.return_value = {
        "data": (
            {**profile, "operationModeSubType": None} if malformed_subtype else profile
        )
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert coord.savings_use_battery_after_peak
    assert savings_switch.is_on
    assert number.native_value == 30
    assert number.extra_state_attributes["control_update_status"] == "confirmed"


@pytest.mark.parametrize(
    "source",
    [
        "profile",
        "settings",
        "pending",
        "same",
        "profileless_settings",
        "superseded_settings",
    ],
)
async def test_unassociated_profile_subtype_does_not_replace_typed_evidence(
    reserve_repair, source
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    savings = {"profile": "cost_savings"}
    enabled = "prioritize-energy"
    known = {**savings, "operationModeSubType": enabled}
    settings_sources = {"settings", "profileless_settings", "superseded_settings"}
    coord.client.storm_guard_profile.return_value = {
        "data": savings if source in settings_sources else known
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    if source in settings_sources or source == "same":
        previous = {**savings, "operationModeSubType": ""}
        coord.client.battery_settings_details.return_value = {
            "data": previous if source == "superseded_settings" else known
        }
        await runtime.async_refresh_battery_settings(force=True)
    if source == "superseded_settings":
        coord.client.storm_guard_profile.return_value = {"data": known}
        await runtime.async_refresh_storm_guard_profile(force=True)
    if source in {"pending", "same"}:
        await number.async_set_native_value(30)
    partial = {"data": {"operationModeSubType": enabled if source == "same" else ""}}
    if source == "profileless_settings":
        coord.client.battery_settings_details.return_value = partial
        await runtime.async_refresh_battery_settings(force=True)
    else:
        coord.client.storm_guard_profile.return_value = partial
        await runtime.async_refresh_storm_guard_profile(force=True)
    if source in settings_sources or source == "profile":
        await number.async_set_native_value(30)

    coord.client.storm_guard_profile.return_value = {
        "data": {**savings, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    if source != "same":
        assert coord.battery_profile_pending
        assert number.native_value == 20
        coord.client.storm_guard_profile.return_value["data"][
            "operationModeSubType"
        ] = (enabled if source == "pending" else "")
        await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


async def test_legacy_mode_promotion_cannot_associate_profileless_subtype(
    battery_repair_manager,
):
    coord, _manager = battery_repair_manager
    runtime = coord.battery_runtime
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    runtime.parse_battery_site_settings_payload({"data": {"showSavingsMode": True}})
    coord.async_request_refresh = AsyncMock()
    coord.client.set_battery_profile = AsyncMock(return_value={})
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {"profile": "self-consumption", "batteryBackupPercentage": 20}
        }
    )
    await runtime.async_refresh_storm_guard_profile(force=True)
    with freeze_time(real_asyncio=True) as clock:
        await runtime.async_set_system_profile("cost_savings")
        coord.client.battery_status = AsyncMock(
            return_value={
                "storages": [
                    {
                        "id": "one",
                        "battery_mode": "Savings Mode",
                        "last_report": dt_util.utcnow().timestamp(),
                    }
                ]
            }
        )
        await runtime.async_refresh_battery_status(force=True)
        assert not coord.battery_profile_pending
        coord.client.storm_guard_profile.return_value = {
            "data": {"operationModeSubType": "prioritize-energy"}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        clock.tick(timedelta(seconds=3))

        number = BatteryReserveNumber(coord)
        await number.async_set_native_value(30)
        coord.client.storm_guard_profile.return_value = {
            "data": {"profile": "cost_savings", "batteryBackupPercentage": 30}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert coord.battery_profile_pending
        assert number.native_value == 20
        coord.client.storm_guard_profile.return_value["data"][
            "operationModeSubType"
        ] = "prioritize-energy"
        await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


@pytest.mark.parametrize("includes_mode", [True, False])
async def test_reserve_cannot_borrow_savings_subtype_from_before_acknowledgement(
    reserve_repair, includes_mode
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    profile = {"profile": "cost_savings"}
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "operationModeSubType": "", "batteryBackupPercentage": 20}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)

    async def write_with_early_read(*_args, on_accepted_profile, **_kwargs):
        explicit_profile = profile if includes_mode else {}
        coord.client.storm_guard_profile.return_value = {
            "data": {
                **explicit_profile,
                "operationModeSubType": "prioritize-energy",
                "batteryBackupPercentage": 30,
            }
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        on_accepted_profile(
            "cost_savings", "prioritize-energy" if includes_mode else None
        )
        return {}

    coord.client.set_battery_settings_compat.side_effect = write_with_early_read
    await number.async_set_native_value(30)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    assert number.native_value == 20

    coord.client.storm_guard_profile.return_value["data"][
        "operationModeSubType"
    ] = "prioritize-energy"
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


@pytest.mark.parametrize("when", ["early", "overdue", "before_retry"])
async def test_profile_switch_during_reserve_retires_savings_subtype(
    hass, reserve_repair, when
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    savings = {"profile": "cost_savings", "batteryBackupPercentage": 20}
    coord.client.storm_guard_profile.return_value = {
        "data": {**savings, "operationModeSubType": "prioritize-energy"}
    }
    overdue = when == "overdue"
    with freeze_time(real_asyncio=True) as clock:
        await runtime.async_refresh_storm_guard_profile(force=True)
        coord.client.battery_settings_details.return_value = (
            coord.client.storm_guard_profile.return_value
        )
        await runtime.async_refresh_battery_settings(force=True)
        await number.async_set_native_value(30)
        if overdue:
            clock.tick(timedelta(seconds=601))
        coord.client.storm_guard_profile.return_value = {
            "data": {"profile": "self-consumption"}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert coord.battery_profile == (
            "self-consumption" if overdue else "cost_savings"
        )

        requested = 30
        if when == "before_retry":
            clock.tick(timedelta(seconds=601))
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
            requested = 35
            await number.async_set_native_value(requested)
        coord.client.storm_guard_profile.return_value = {
            "data": {**savings, "batteryBackupPercentage": requested}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert coord.battery_profile_pending
        assert number.native_value == 20

        coord.client.storm_guard_profile.return_value["data"][
            "operationModeSubType"
        ] = "prioritize-energy"
        await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == requested


async def test_reserve_cannot_borrow_savings_subtype_from_another_configured_profile(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    coord.client.storm_guard_profile.return_value = {
        "data": {
            "profile": "ai_optimisation",
            "operationModeSubType": "prioritize-energy",
            "batteryBackupPercentage": 20,
        }
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "cost_savings"}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    await number.async_set_native_value(30)

    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "cost_savings", "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    assert number.native_value == 20

    coord.client.storm_guard_profile.return_value["data"]["operationModeSubType"] = ""
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30


async def test_conflicting_savings_subtype_cannot_replace_pending_reserve(
    hass, reserve_repair
):
    coord, manager, number = reserve_repair
    runtime = coord.battery_runtime
    profile = {"profile": "cost_savings", "operationModeSubType": "prioritize-energy"}
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 20}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    with freeze_time(real_asyncio=True) as clock:
        await number.async_set_native_value(30)
        coord.client.storm_guard_profile.return_value = {
            "data": {
                **profile,
                "batteryBackupPercentage": 30,
                "operationModeSubType": "",
            }
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert coord.battery_profile_pending
        assert number.native_value == 20

        issue_id, _ = await open_overdue_reserve_repair(hass, coord, manager, clock)
        coord.client.storm_guard_profile.return_value = {
            "data": {
                **profile,
                "profile": "ai_optimisation",
                "batteryBackupPercentage": 30,
            }
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        coord.client.storm_guard_profile.return_value = {
            "data": {"profile": "cost_savings", "batteryBackupPercentage": 30}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)
        assert coord.battery_profile_pending
        assert number.native_value == 20
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

        coord.client.storm_guard_profile.return_value = {
            "data": {**profile, "batteryBackupPercentage": 30}
        }
        await runtime.async_refresh_storm_guard_profile(force=True)

    assert not coord.battery_profile_pending
    assert number.native_value == 30


async def test_profile_reserve_bounds_precede_settings_during_confirmation(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    profile = {"profile": "self-consumption"}
    await number.async_set_native_value(30)

    # The current Profile response contradicts the older 90% Settings maximum.
    coord.client.storm_guard_profile.return_value = {
        "data": {
            **profile,
            "batteryBackupPercentage": 30,
            "batteryBackupPercentageMax": 25,
        }
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    assert number.native_value == 20

    # A contradictory range must not replace the earlier valid limit.
    coord.client.storm_guard_profile.return_value["data"][
        "batteryBackupPercentageMin"
    ] = 50
    await runtime.async_refresh_storm_guard_profile(force=True)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    assert number.native_value == 20

    # An older Settings limit cannot clobber an explicit valid Profile reserve.
    coord.client.battery_settings_details.return_value = {
        "data": {**profile, "batteryBackupPercentageMax": 25}
    }
    await runtime.async_refresh_battery_settings(force=True)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 30, **RESERVE_BOUNDS}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 95}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert number.native_value == 30

    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentageMin": 95}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert number.native_value == 30
    assert number.extra_state_attributes["control_update_status"] == "confirmed"
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert number.native_value == 30
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert number.native_value == 30


@pytest.mark.parametrize(
    "settings_bounds",
    [{}, {"batteryBackupPercentageMax": -1}, {"batteryBackupPercentageMax": 101}],
)
async def test_settings_omissions_or_invalid_bounds_preserve_reserve_confirmation_limits(
    reserve_repair, settings_bounds
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    profile = {"profile": "self-consumption"}
    await number.async_set_native_value(30)
    coord.client.battery_settings_details.return_value = {
        "data": {**profile, **settings_bounds}
    }
    await runtime.async_refresh_battery_settings(force=True)

    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 95}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_profile_pending
    assert number.native_value == 20

    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    assert number.native_value == 30
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert number.native_value == 30


async def test_profile_readback_tracks_new_valid_settings_limits(reserve_repair):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    profile = {"profile": "self-consumption"}
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 95}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert number.native_value == 20

    await number.async_set_native_value(30)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 30}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert not coord.battery_profile_pending
    coord.client.battery_settings_details.return_value = {
        "data": {"batteryBackupPercentageMax": 95}
    }
    await runtime.async_refresh_battery_settings(force=True)
    coord.client.storm_guard_profile.return_value = {
        "data": {**profile, "batteryBackupPercentage": 95}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert number.native_value == 95


async def test_partial_profile_validated_reserve_matches_displayed_reserve(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    runtime = coord.battery_runtime
    await number.async_set_native_value(30)
    coord.client.storm_guard_profile.return_value = {
        "data": {
            "profile": "self-consumption",
            "batteryBackupPercentage": 30,
            "batteryBackupPercentageMax": 90,
        }
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    coord.client.storm_guard_profile.return_value = {
        "data": {"batteryBackupPercentage": 95, "batteryBackupPercentageMax": 95}
    }
    await runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.battery_effective_backup_percentage == number.native_value == 95


async def test_reserve_readback_matches_configured_profile_when_live_differs(
    reserve_repair,
):
    coord, _manager, number = reserve_repair
    coord.client.battery_status = AsyncMock(
        return_value={
            "storages": [
                {
                    "id": "one",
                    "battery_mode": "Savings Mode",
                    "last_report": dt_util.utcnow().timestamp(),
                }
            ]
        }
    )
    await coord.battery_runtime.async_refresh_battery_status(force=True)
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_live_profile == "cost_savings"

    await number.async_set_native_value(30)
    coord.client.storm_guard_profile.return_value = {
        "data": {"profile": "self-consumption", "batteryBackupPercentage": 30}
    }
    await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)

    assert not coord.battery_profile_pending
    assert coord.battery_live_profile == "cost_savings"
    assert coord.battery_profile == "self-consumption"
    assert number.native_value == 30
    assert number.extra_state_attributes["control_update_status"] == "confirmed"
    coord.client.set_battery_settings_compat.assert_awaited_once_with(
        {"batteryBackupPercentage": 30},
        merged_payload=True,
        strip_devices=True,
        on_accepted_profile=ANY,
    )


@pytest.mark.parametrize("feedback", ["wrong_profile", "missing_profile", "timeout"])
async def test_reserve_check_again_ignores_inconclusive_profile(
    hass, reserve_repair, feedback
):
    coord, manager, number = reserve_repair
    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": {"profile": "self-consumption", "batteryBackupPercentage": 20}
        }
    )
    with freeze_time(real_asyncio=True) as clock:
        await number.async_set_native_value(30)
        issue_id, result = await open_overdue_reserve_repair(
            hass, coord, manager, clock
        )
        if feedback == "timeout":
            coord.client.storm_guard_profile.side_effect = TimeoutError
        else:
            data = {"batteryBackupPercentage": 30}
            if feedback == "wrong_profile":
                data["profile"] = "cost_savings"
            coord.client.storm_guard_profile.return_value = {"data": data}
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "check_again"}
        )

    assert result["step_id"] == "pending"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert coord.battery_profile_pending
    assert number.native_value == 20
    assert number.extra_state_attributes["requested_reserve_percentage"] == 30
    coord.client.set_battery_settings_compat.assert_awaited_once()


@pytest.mark.parametrize("profile", ["cost_savings", "backup_only"])
async def test_check_again_uses_authoritative_readback(hass, profile_repair, profile):
    coord, issue_id, manager = profile_repair
    runtime = coord.battery_runtime
    runtime.async_refresh_battery_settings = type(
        runtime
    ).async_refresh_battery_settings.__get__(runtime)
    runtime.async_refresh_storm_guard_profile = type(
        runtime
    ).async_refresh_storm_guard_profile.__get__(runtime)
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"profile": profile, "batteryBackupPercentage": 20}}
    )
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {
                "profile": profile,
                "batteryBackupPercentage": 20,
                "isBatteryChangePending": False,
            }
        }
    )
    result = await start(manager, issue_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "check_again"}
    )
    if profile == "cost_savings":
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert not coord.battery_profile_pending
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    else:
        assert result["step_id"] == "pending"
        assert coord.battery_profile_pending
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    coord.client.battery_settings_details.assert_awaited_once_with()
    coord.client.storm_guard_profile.assert_awaited_once_with(
        locale=hass.config.language
    )
    coord.client.cancel_battery_profile_update.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", [False, HomeAssistantError(), aiohttp.ClientError(), TimeoutError()]
)
async def test_check_failure_retains_repair_and_offers_retry(
    hass, profile_repair, failure
):
    coord, issue_id, manager = profile_repair
    if failure is False:
        coord.battery_runtime.async_refresh_battery_settings.return_value = False
    else:
        coord.battery_runtime.async_refresh_battery_settings.side_effect = failure
    result = await start(manager, issue_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "check_again"}
    )
    assert result["step_id"] == "check_failed"
    assert coord.battery_profile_pending
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_cancel_requires_confirmation_and_calls_backend(hass, profile_repair):
    coord, issue_id, manager = profile_repair
    result = await start(manager, issue_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "cancel_change"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "cancel_change"
    coord.client.cancel_battery_profile_update.assert_not_awaited()
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    coord.client.cancel_battery_profile_update.assert_awaited_once_with()
    assert not coord.battery_profile_pending
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    "failure",
    [
        HomeAssistantError(),
        aiohttp.ClientError(),
        TimeoutError(),
        Unauthorized(),
        EnphaseLoginWallUnauthorized(
            endpoint="battery_profile", request_label="cancel", status=200
        ),
    ],
)
async def test_cancel_failure_keeps_request_and_allows_retry(
    hass, profile_repair, failure
):
    coord, issue_id, manager = profile_repair
    coord.client.cancel_battery_profile_update.side_effect = failure
    result = await start(manager, issue_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "cancel_change"}
    )
    result = await manager.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cannot_cancel"}
    assert coord.battery_profile_pending
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    coord.client.cancel_battery_profile_update.side_effect = None
    coord._battery_profile_last_write_mono = None  # noqa: SLF001
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("step", ["init", "check_again", "cancel_change"])
@pytest.mark.parametrize(
    "state", ["resolved", "changed", "unloaded", "retired", "missing_time"]
)
async def test_stale_flow_does_not_touch_request(hass, profile_repair, step, state):
    coord, issue_id, manager = profile_repair
    flow = await async_create_fix_flow(
        hass, issue_id, ir.async_get(hass).async_get_issue(DOMAIN, issue_id).data
    )
    flow.hass = hass
    if state == "resolved":
        coord.battery_runtime.clear_battery_pending()
    elif state == "changed":
        coord._battery_pending_requested_at = dt_util.utcnow()  # noqa: SLF001
    elif state == "missing_time":
        coord._battery_pending_requested_at = None  # noqa: SLF001
    elif state == "unloaded":
        coord.config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)
    else:
        coord.mark_runtime_stopped()
    result = await getattr(flow, f"async_step_{step}")()
    if state == "resolved":
        assert result["type"] == FlowResultType.CREATE_ENTRY
    else:
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == (
            "entry_unavailable"
            if state in {"unloaded", "retired"}
            else "request_changed"
        )
    coord.client.cancel_battery_profile_update.assert_not_awaited()
    coord.battery_runtime.async_refresh_battery_settings.assert_not_awaited()


async def test_request_changes_during_check_aborts(hass, profile_repair):
    coord, issue_id, manager = profile_repair

    async def replace_request(*, force):
        coord._battery_pending_requested_at = dt_util.utcnow()  # noqa: SLF001

    coord.battery_runtime.async_refresh_storm_guard_profile.side_effect = (
        replace_request
    )
    result = await start(manager, issue_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "check_again"}
    )
    assert result["reason"] == "request_changed"
    assert coord.battery_profile_pending
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


@pytest.mark.parametrize("state", ["retired", "unloaded", "changed", "resolved"])
async def test_first_check_read_revalidates_before_second_read(
    hass, profile_repair, state
):
    coord, issue_id, manager = profile_repair

    async def invalidate_during_settings_read(*, force):
        if state == "retired":
            coord.mark_runtime_stopped()
        elif state == "unloaded":
            coord.config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)
        elif state == "changed":
            coord._battery_pending_requested_at = dt_util.utcnow()  # noqa: SLF001
        else:
            coord.battery_runtime.clear_battery_pending()
        return True

    coord.battery_runtime.async_refresh_battery_settings.side_effect = (
        invalidate_during_settings_read
    )
    result = await start(manager, issue_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "check_again"}
    )
    if state == "resolved":
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    else:
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == (
            "request_changed" if state == "changed" else "entry_unavailable"
        )
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    coord.battery_runtime.async_refresh_storm_guard_profile.assert_not_awaited()


async def test_reload_during_first_read_does_not_use_replaced_runtime(
    hass, profile_repair
):
    coord, issue_id, manager = profile_repair
    entry = coord.config_entry
    replacement = EnphaseCoordinator(hass, dict(entry.data), config_entry=entry)
    replacement.battery_runtime.set_battery_pending(
        profile="cost_savings", reserve=20, sub_type=None
    )
    replacement._battery_pending_requested_at = (  # noqa: SLF001
        coord.battery_pending_requested_at
    )
    replacement._battery_pending_requested_mono = time.monotonic() - 660  # noqa: SLF001
    replacement.diagnostics.sync_battery_profile_pending_issue()
    replacement.battery_runtime.async_refresh_storm_guard_profile = AsyncMock()

    async def reload_during_settings_read(*, force):
        coord.mark_runtime_stopped()
        entry.runtime_data = EnphaseRuntimeData(coordinator=replacement)
        return True

    coord.battery_runtime.async_refresh_battery_settings.side_effect = (
        reload_during_settings_read
    )
    try:
        result = await start(manager, issue_id)
        result = await manager.async_configure(
            result["flow_id"], {"next_step_id": "check_again"}
        )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "entry_unavailable"
        coord.battery_runtime.async_refresh_storm_guard_profile.assert_not_awaited()
        replacement.battery_runtime.async_refresh_storm_guard_profile.assert_not_awaited()
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
        assert replacement.battery_profile_pending
    finally:
        replacement.control_updates.cleanup()
        entry.runtime_data = EnphaseRuntimeData(coordinator=coord)


@pytest.mark.parametrize("state", ["changed", "retired"])
async def test_request_changes_during_permission_check_does_not_cancel(
    hass, profile_repair, state
):
    coord, issue_id, manager = profile_repair

    async def change_during_permission_check():
        if state == "changed":
            coord._battery_pending_requested_at = dt_util.utcnow()  # noqa: SLF001
        else:
            coord.mark_runtime_stopped()

    coord.battery_runtime.async_assert_battery_profile_write_allowed = AsyncMock(
        side_effect=change_during_permission_check
    )
    result = await start(manager, issue_id)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "cancel_change"}
    )
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.ABORT
    coord.client.cancel_battery_profile_update.assert_not_awaited()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


@pytest.mark.parametrize(
    "issue_id,data",
    [
        ("other_issue", {"entry_id": "missing", "requested_at": "time"}),
        (ISSUE_BATTERY_PROFILE_PENDING, None),
        (ISSUE_BATTERY_PROFILE_PENDING, {}),
        (ISSUE_BATTERY_PROFILE_PENDING, {"entry_id": 7, "requested_at": "time"}),
        (ISSUE_BATTERY_PROFILE_PENDING, {"entry_id": "missing", "requested_at": 7}),
        (
            ISSUE_BATTERY_PROFILE_PENDING,
            {"entry_id": "missing", "requested_at": "time"},
        ),
    ],
)
async def test_invalid_or_deleted_entry_cannot_be_repaired(hass, issue_id, data):
    flow = await async_create_fix_flow(hass, issue_id, data)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["reason"] == "entry_unavailable"


async def test_other_domain_entry_cannot_be_targeted(hass):
    entry = MockConfigEntry(domain="other", data={})
    entry.add_to_hass(hass)
    flow = await async_create_fix_flow(
        hass,
        ISSUE_BATTERY_PROFILE_PENDING,
        {"entry_id": entry.entry_id, "requested_at": "time"},
    )
    flow.hass = hass
    assert (await flow.async_step_init())["reason"] == "entry_unavailable"


async def test_repairs_are_isolated_between_entries(hass, profile_repair):
    first, first_issue, manager = profile_repair
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        data={**first.config_entry.data, "site_id": "another-site"},
        unique_id="another-site",
    )
    second_entry.add_to_hass(hass)
    second = EnphaseCoordinator(hass, second_entry.data, second_entry)
    second_entry.runtime_data = EnphaseRuntimeData(coordinator=second)
    second_entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    second.battery_runtime.set_battery_pending(
        profile="backup_only", reserve=100, sub_type=None
    )
    second._battery_pending_requested_at = dt_util.utcnow() - timedelta(
        minutes=12
    )  # noqa: SLF001
    second._battery_pending_requested_mono = time.monotonic() - 720  # noqa: SLF001
    second.diagnostics.sync_battery_profile_pending_issue()
    second_issue = second.diagnostics._repair_issue_id(
        ISSUE_BATTERY_PROFILE_PENDING
    )  # noqa: SLF001
    assert second_issue != first_issue
    assert ir.async_get(hass).async_get_issue(DOMAIN, first_issue) is not None
    result = await start(manager, first_issue)
    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "cancel_change"}
    )
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert second.battery_profile_pending
    assert ir.async_get(hass).async_get_issue(DOMAIN, second_issue) is not None
    second.control_updates.cleanup()
    second_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)


async def test_repair_survives_unavailable_optional_diagnostic_property(
    hass, profile_repair, monkeypatch
):
    coord, issue_id, manager = profile_repair

    def unavailable(_coord):
        raise RuntimeError("Optional battery capability is unavailable")

    monkeypatch.setattr(type(coord), "battery_reserve_editable", property(unavailable))
    coord.battery_runtime.clear_battery_pending()
    coord.battery_runtime.set_battery_pending(
        profile="cost_savings", reserve=20, sub_type=None
    )
    coord._battery_pending_requested_at = dt_util.utcnow() - timedelta(
        minutes=11
    )  # noqa: SLF001
    coord._battery_pending_requested_mono = time.monotonic() - 660  # noqa: SLF001
    coord.diagnostics.sync_battery_profile_pending_issue()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue.is_fixable
    assert issue.data["site_metrics"]["battery_reserve_editable"] is False
    assert (await start(manager, issue_id))["step_id"] == "init"
