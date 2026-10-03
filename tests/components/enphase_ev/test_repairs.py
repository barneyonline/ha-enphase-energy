"""Exercise battery profile recovery through Home Assistant's Repair manager."""

from datetime import timedelta
import time
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.api import EnphaseLoginWallUnauthorized, Unauthorized
from custom_components.enphase_ev.const import DOMAIN, ISSUE_BATTERY_PROFILE_PENDING
from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.repairs import async_create_fix_flow
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData


@pytest.fixture
async def profile_repair(hass, config_entry):
    coord = EnphaseCoordinator(hass, dict(config_entry.data), config_entry=config_entry)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    config_entry._async_set_state(hass, ConfigEntryState.LOADED, None)
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
    hass.config.components.add(DOMAIN)
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    yield coord, issue_id, manager
    coord.control_updates.cleanup()
    config_entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)


async def start(manager, issue_id):
    return await manager.async_init(DOMAIN, data={"issue_id": issue_id})


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
