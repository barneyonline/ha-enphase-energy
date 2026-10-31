"""Battery readback remains conservative with incomplete optional capabilities."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.enphase_ev.battery_runtime import BatteryRuntime
from custom_components.enphase_ev.state_models import BatteryControlCapability
from tests.components.enphase_ev.test_api_client_methods import _make_cre


def test_clearing_pending_can_preserve_optimistic_profile(coordinator_factory):
    runtime = coordinator_factory().battery_runtime
    runtime.battery_state._battery_optimistic_profile = "self-consumption"
    runtime.battery_state._battery_optimistic_until_mono = time.monotonic() + 60
    runtime.clear_battery_pending(clear_optimistic=False)
    assert runtime.optimistic_battery_profile() == "self-consumption"
    runtime._note_authoritative_profile_read(None)
    assert not runtime.battery_state._battery_profile_authoritative_seen


@pytest.mark.parametrize("matching", [False, True])
def test_confirm_pending_without_reserve_does_not_replace_confirmed_reserve(
    coordinator_factory, matching
):
    coord = coordinator_factory()
    state = coord.battery_runtime.battery_state
    state._battery_profile = "self-consumption"
    state._battery_backup_percentage = 20
    state._battery_pending_profile = "self-consumption" if matching else "backup_only"
    state._battery_pending_reserve = None
    coord.battery_runtime.confirm_battery_pending()
    assert state._battery_backup_percentage == 20
    assert state._battery_pending_profile is None


def test_legacy_runtime_owns_schedule_target_without_separate_state():
    host = SimpleNamespace()
    runtime = BatteryRuntime(host)
    runtime._set_schedule_family_toggle_target("dtg", True)
    assert getattr(host, runtime._schedule_toggle_target_attr("dtg")) is True


def test_settings_observation_invalidation_skips_other_groups(coordinator_factory):
    coord = coordinator_factory()
    update = coord.control_updates.begin(
        "charge_mode", "EV", {"mode": "MANUAL_CHARGING"}, {}
    )
    token = update.read_token
    coord.battery_runtime.invalidate_settings_control_observations()
    assert update.read_token is token


async def test_battery_write_access_without_permission_endpoint_is_unconfirmed(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    monkeypatch.setattr(coord.client, "battery_site_settings", None)
    with pytest.raises(ServiceValidationError) as caught:
        await coord.battery_runtime.async_ensure_battery_write_access_confirmed()
    assert caught.value.translation_key == "battery_write_access_unconfirmed"


@pytest.mark.parametrize("status", [404, 405])
async def test_cfg_validation_unsupported_route_does_not_fail_commit(
    coordinator_factory, status
):
    coord = coordinator_factory()
    coord.client.validate_battery_schedule = AsyncMock(side_effect=_make_cre(status))
    await coord.battery_runtime._async_validate_cfg_schedule_commit()
    coord.client.validate_battery_schedule.assert_awaited_once_with("cfg")


@pytest.mark.parametrize("message", ["", "{}", '{"error": {"message": "Conflict."}}'])
def test_schedule_conflict_without_structured_status_uses_safe_message(
    coordinator_factory, message
):
    with pytest.raises(ServiceValidationError) as caught:
        coordinator_factory().battery_runtime.raise_schedule_update_validation_error(
            _make_cre(409, message)
        )
    assert caught.value.translation_key == (
        "schedule_update_conflict_detail"
        if "Conflict." in message
        else "schedule_update_conflict"
    )


@pytest.mark.parametrize(
    "family, enabled",
    [("cfg", True), ("cfg", None), ("dtg", True), ("dtg", None), ("rbd", False)],
)
def test_schedule_enablement_uses_legacy_flags_without_resolver(family, enabled):
    host = SimpleNamespace(
        battery_charge_from_grid_schedule_enabled=enabled,
        battery_cfg_control_force_schedule_opted=None,
        battery_dtg_control=None,
        battery_rbd_control=None,
    )
    runtime = BatteryRuntime(host)
    if family != "cfg":
        setattr(host, runtime._battery_schedule_enabled_attr(family), enabled)
    assert runtime._schedule_family_effective_enabled(family, None) is bool(enabled)


@pytest.mark.parametrize("family", ["rbd", "future"])
def test_schedule_control_payload_without_optional_capabilities(family):
    runtime = BatteryRuntime(SimpleNamespace(battery_rbd_control=None))
    assert runtime._schedule_family_control_payload(
        family, enabled=False, current_start=None, current_end=None
    ) == {"enabled": False}


def test_cfg_settings_ignore_unreported_capability_fields(coordinator_factory):
    coord = coordinator_factory()
    coord._battery_cfg_control = BatteryControlCapability(show=True)
    payload = coord.battery_runtime._schedule_family_settings_payload(
        "cfg", start_minutes=60, end_minutes=120, enabled=True
    )
    assert payload["cfgControl"] == {"show": True, "forceScheduleOpted": True}


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
async def test_unknown_schedule_family_does_not_commit_known_family_settings(
    coordinator_factory, monkeypatch, operation
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    monkeypatch.setattr(coord, "control_updates", None)
    call = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(coord.client, f"{operation}_battery_schedule", call)
    settings = AsyncMock()
    monkeypatch.setattr(runtime, "async_apply_schedule_family_settings", settings)
    kwargs = {"schedule_type": "future"}
    if operation != "delete":
        kwargs.update(
            start_time="01:00", end_time="02:00", limit=80, days=[1], timezone="UTC"
        )
    args = () if operation == "create" else ("schedule",)
    await getattr(runtime, f"async_{operation}_battery_schedule")(*args, **kwargs)
    call.assert_awaited_once()
    settings.assert_not_awaited()


@pytest.mark.parametrize(
    "method, field",
    [
        ("async_refresh_grid_control_check", "_grid_control_supported"),
        ("async_refresh_grid_mode_status", "_grid_mode_status_supported"),
        ("async_refresh_grid_outage_context", "_grid_outage_context_supported"),
        ("async_refresh_dry_contact_settings", "_dry_contact_settings_supported"),
    ],
)
@pytest.mark.parametrize("supported", [None, True])
async def test_skipped_optional_battery_reads_preserve_support_state(
    coordinator_factory, monkeypatch, method, field, supported
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    setattr(runtime.battery_state, field, supported)
    monkeypatch.setattr(
        runtime.health, "endpoint_family_should_run", Mock(return_value=False)
    )
    await getattr(runtime, method)(force=True)
    assert getattr(runtime.battery_state, field) is supported


async def test_grid_mode_transient_failure_preserves_recent_readback(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    runtime.battery_state._grid_mode_status_supported = True
    runtime.battery_state._grid_mode_status = "on_grid"
    runtime.battery_state._grid_mode_status_last_success_mono = time.monotonic()
    monkeypatch.setattr(runtime, "grid_envoy_serial", lambda: "GW")
    coord.client.site_livestream_payload = AsyncMock(
        side_effect=RuntimeError("temporary")
    )
    await runtime.async_refresh_grid_mode_status(force=True)
    assert runtime.battery_state._grid_mode_status == "on_grid"


async def test_battery_settings_failure_does_not_clear_newer_ev_preference(
    coordinator_factory,
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    runtime.ev_preferences.value = True

    async def fail_after_preference_changes():
        runtime.ev_preferences.generation += 1
        raise RuntimeError("temporary")

    coord.client.battery_settings_details = fail_after_preference_changes
    assert not await runtime.async_refresh_battery_settings(force=True)
    assert runtime.ev_preferences.value is True


def test_profile_devices_without_evse_list_and_feature_objects_are_ignored(
    coordinator_factory,
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    runtime.parse_battery_profile_payload(
        {"data": {"profile": "self-consumption", "devices": {"iqEvse": None}}}
    )
    runtime.parse_battery_site_settings_payload(
        {"data": {"featureDetails": {"unknown": {"nested": True}}}}
    )
    assert runtime.battery_state._battery_feature_details == {}


def test_storm_alert_parser_ignores_null_and_nested_only_fields(coordinator_factory):
    runtime = coordinator_factory().battery_runtime
    assert (
        runtime.parse_storm_alert(
            {"stormAlerts": [None, {"nested": {}}, {"status": "inactive"}]}
        )
        is True
    )
    assert runtime.battery_state._storm_alerts == [
        {"active": True},
        {"status": "inactive"},
    ]
    assert runtime.normalize_storm_guard_state({}) is None


@pytest.mark.parametrize("start,end", [(60, None), (None, 120)])
async def test_partial_schedule_window_preserves_existing_boundary(
    coordinator_factory, monkeypatch, start, end
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    monkeypatch.setattr(
        runtime, "_current_battery_schedule_window_for_type", lambda family: (0, 180)
    )
    coord.client.set_battery_settings = AsyncMock()
    monkeypatch.setattr(runtime, "async_apply_battery_settings_compat", AsyncMock())
    await runtime.async_apply_schedule_family_settings(
        "dtg", start_time=start, end_time=end, enabled=True
    )
    assert (
        coord.client.set_battery_settings.await_count
        + runtime.async_apply_battery_settings_compat.await_count
        > 0
    )


async def test_storm_alert_reads_without_control_tracking(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    monkeypatch.setattr(coord, "control_updates", None)
    coord.client.storm_guard_alert = AsyncMock(
        return_value={"criticalAlertActive": True, "stormAlerts": []}
    )
    await coord.battery_runtime.async_refresh_storm_alert(force=True)
    assert coord.battery_runtime.battery_state._storm_alert_active is True


@pytest.mark.parametrize("pending", [False, True])
async def test_cancel_profile_without_control_tracking(
    coordinator_factory, monkeypatch, pending
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    if pending:
        runtime.battery_state._battery_pending_profile = "backup_only"
        runtime.battery_state._battery_pending_requested_at = 1
    monkeypatch.setattr(coord, "control_updates", None)
    monkeypatch.setattr(
        runtime, "async_assert_battery_profile_write_allowed", AsyncMock()
    )
    monkeypatch.setattr(coord, "async_request_refresh", AsyncMock())
    coord.client.cancel_battery_profile_update = AsyncMock()
    await runtime.async_cancel_pending_profile_change()
    assert not coord.battery_profile_pending


@pytest.mark.parametrize(
    "window,expected",
    [({"end": "10:00"}, {"end": "10:00"}), ({"start": "09:00"}, {"start": "09:00"})],
)
def test_dry_contact_schedule_with_single_boundary(
    coordinator_factory, window, expected
):
    runtime = coordinator_factory().battery_runtime
    assert runtime._normalize_dry_contact_schedule_windows([window]) == [expected]
    payload = {
        "contactId": "DRY",
        **(
            {"scheduleStart": window["start"]}
            if "start" in window
            else {"scheduleEnd": window["end"]}
        ),
    }
    runtime.parse_dry_contact_settings_payload({"data": {"contacts": [payload]}})
    result = runtime.battery_state._dry_contact_settings_entries[0]
    assert result["schedule_windows"] == [expected]


def test_dry_contact_nested_settings_skip_scalar_children(coordinator_factory):
    runtime = coordinator_factory().battery_runtime
    runtime.parse_dry_contact_settings_payload({"nested": [None, "invalid"]})
    assert runtime.battery_state._dry_contact_settings_entries == []


def test_young_pending_profile_waits_for_backend_grace(coordinator_factory):
    from homeassistant.util import dt as dt_util

    coord = coordinator_factory()
    state = coord.battery_runtime.battery_state
    state._battery_pending_profile = "backup_only"
    state._battery_pending_requested_at = dt_util.utcnow()
    state._battery_backend_not_pending_observed_at = dt_util.utcnow()
    coord.battery_runtime.sync_backend_battery_profile_pending(False)
    assert coord.battery_profile_pending


def test_multiple_profile_evse_devices_preserve_first_device(coordinator_factory):
    coord = coordinator_factory()
    coord.battery_runtime.parse_battery_profile_payload(
        {"data": {"devices": {"iqEvse": [{"uuid": "EV-A"}, {"uuid": "EV-B"}]}}}
    )
    assert (
        coord.battery_runtime.battery_state._battery_profile_evse_device["uuid"]
        == "EV-A"
    )


async def test_enable_existing_restrict_schedule_uses_control_settings(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    state = runtime.battery_state
    state._battery_rbd_control = BatteryControlCapability(show=True, locked=False)
    state._battery_rbd_schedule_id = "RBD"
    state._battery_rbd_begin_time = 60
    state._battery_rbd_end_time = 120
    monkeypatch.setattr(runtime, "_assert_battery_settings_feature_writable", Mock())
    monkeypatch.setattr(
        runtime, "async_assert_battery_settings_write_allowed", AsyncMock()
    )
    monkeypatch.setattr(
        runtime, "_async_verify_schedule_family_toggle_applied", AsyncMock()
    )
    monkeypatch.setattr(coord, "async_request_refresh", AsyncMock())
    coord.client.set_battery_settings = AsyncMock()
    runtime._schedule_family_toggle_validation_details(
        "rbd", enabled=True, schedule_id="RBD", current_start=60, current_end=120
    )
    await runtime._async_set_schedule_family_enabled("rbd", True)
    assert coord.client.set_battery_settings.await_args.kwargs["schedule_type"] == "rbd"
    assert (
        coord.client.set_battery_settings.await_args.args[0]["rbdControl"]["enabled"]
        is True
    )


def test_schedule_without_id_still_reads_boundary_and_day_values(coordinator_factory):
    coord = coordinator_factory()
    coord.battery_runtime.parse_battery_schedules_payload(
        {"dtg": {"details": [{"startTime": "01:00", "endTime": "02:00", "days": [1]}]}}
    )
    assert coord.battery_runtime.battery_state._battery_dtg_schedule_id is None


async def test_old_reserve_restore_does_not_clear_newer_task(
    coordinator_factory, monkeypatch
):
    import asyncio
    from custom_components.enphase_ev import battery_runtime as module

    coord = coordinator_factory()
    runtime = coord.battery_runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def restore(**kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(module, "_PROFILE_RECOVERY_RESERVE_RESTORE_DELAY_S", 0)
    monkeypatch.setattr(runtime, "async_apply_battery_reserve_only", restore)
    runtime._schedule_profile_recovery_reserve_restore("self-consumption", 20)
    old = runtime.battery_state._battery_profile_recovery_restore_task
    await entered.wait()
    newer = asyncio.create_task(asyncio.sleep(0))
    await newer
    runtime.battery_state._battery_profile_recovery_restore_task = newer
    release.set()
    await old
    assert runtime.battery_state._battery_profile_recovery_restore_task is newer
