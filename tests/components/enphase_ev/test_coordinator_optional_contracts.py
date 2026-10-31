"""Coordinator behavior with partial metadata and optional host capabilities."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev import coordinator as module
from custom_components.enphase_ev.api import (
    AuthTokens,
    EnphaseLoginWallUnauthorized,
    Unauthorized,
)
from custom_components.enphase_ev.const import (
    CONF_EAUTH,
    CONF_HEMS_AUTH_BACKOFF_UNTIL,
    CONF_SITE_ID,
    CONF_SERIALS,
    OPT_GRID_PROFILE_CONTROLS_ENABLED,
)
from custom_components.enphase_ev.endpoint_policies import EndpointFamilyPolicy
from custom_components.enphase_ev.state_models import BatteryControlCapability


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({}, None),
        ([], None),
        ([{}, []], None),
        ([{}, {"message": "failure"}], "failure"),
        ({"data": [None]}, None),
    ],
)
def test_error_description_search_ignores_empty_nested_containers(payload, expected):
    assert module._extract_error_description(json.dumps(payload)) == expected


def test_first_mapping_ignores_non_mapping_list_head():
    assert module._first_mapping_or_empty([None, {"value": 1}]) == {}


def test_coordinator_without_serials_and_with_expired_hems_backoff(coordinator_factory):
    coord = coordinator_factory(
        config={
            CONF_SITE_ID: "SITE",
            CONF_SERIALS: None,
            CONF_HEMS_AUTH_BACKOFF_UNTIL: (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat(),
        },
        serials=[],
    )
    assert coord._configured_serials == set()
    assert coord._hems_auth_backoff_until is None


def test_setup_timing_methods_before_tracking_starts_are_harmless(coordinator_factory):
    coord = coordinator_factory()
    coord._setup_phase_timings = None
    coord._setup_milestones = None
    coord.record_setup_phase("phase", time.monotonic())
    coord.mark_setup_milestone("milestone")
    coord.finish_setup_tracking(1)
    assert getattr(coord, "_setup_phase_timings", None) is None
    assert getattr(coord, "_setup_milestones", None) is None


def test_endpoint_policy_without_maximum_retains_its_delay(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    policy = EndpointFamilyPolicy(
        failure_backoff_schedule_s=(5,), suppress_after_failures=1
    )
    coord._endpoint_family_policies["test"] = policy
    monkeypatch.setattr(module.random, "uniform", lambda low, high: 1)
    assert coord._endpoint_family_backoff_delay("test", 1) == 5
    coord.note_endpoint_family_success("test")
    assert coord._endpoint_family_state("test").support_state == "unknown"
    assert coord.note_endpoint_family_failure("test", RuntimeError("temporary"))
    assert coord._endpoint_family_state("test").support_state == "suppressed"


async def test_timed_refresh_lookup_accepts_synchronous_cached_value(
    coordinator_factory,
):
    coord = coordinator_factory()
    phases = {}
    assert (
        await coord._async_run_timed_refresh_lookup(phases, "cache", lambda: 42) == 42
    )
    assert phases["cache"] >= 0


async def test_cleanup_releases_references_to_already_completed_tasks(
    coordinator_factory,
):
    coord = coordinator_factory()
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    coord._grid_profile_metadata_task = task
    coord._battery_profile_recovery_restore_task = task
    coord._auto_resume_tasks["EV"] = task
    coord._streaming_stop_task = task
    coord._auth_refresh_task = task
    coord._backoff_refresh_tasks.add(task)
    await coord.async_cleanup_runtime_state()
    assert coord._grid_profile_metadata_task is None
    assert coord._battery_profile_recovery_restore_task is None
    assert coord._streaming_stop_task is None
    assert coord._auth_refresh_task is None
    assert coord._auto_resume_tasks == {}
    assert coord._backoff_refresh_tasks == set()


async def test_grid_profile_metadata_without_refresh_capability(
    coordinator_factory, config_entry, monkeypatch
):
    coord = coordinator_factory()
    coord.config_entry = config_entry
    object.__setattr__(
        config_entry, "options", {OPT_GRID_PROFILE_CONTROLS_ENABLED: True}
    )
    monkeypatch.setattr(coord.grid_profile_runtime, "async_refresh", None)
    await coord.async_refresh_grid_profile_metadata()


async def test_grid_profile_metadata_completed_task_does_not_clear_newer_task(
    coordinator_factory,
):
    coord = coordinator_factory()
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    coord._clear_grid_profile_metadata_task(task)
    assert coord._grid_profile_metadata_task is None


@pytest.mark.parametrize("info", [[], [None, {}]])
def test_empty_embedded_schedule_does_not_imply_charge_mode(coordinator_factory, info):
    coord = coordinator_factory()
    assert not coord._has_embedded_charge_mode({"sch_d": {"info": info}})


def test_auth_repairs_are_optional_for_legacy_hosts(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    with monkeypatch.context() as context:
        context.setattr(coord, "diagnostics", None)
        until = datetime.now(timezone.utc) + timedelta(hours=1)
        coord._note_auth_refresh_suspended(suspended_until=until)
        coord._clear_auth_block(persist=False)
        coord._note_auth_blocked(blocked_until=until, reason="test")
        coord._auth_refresh_rejected_until = time.monotonic() + 100
        assert coord._activate_auth_block_from_login_wall(
            EnphaseLoginWallUnauthorized(
                endpoint="/test",
                request_label="GET /test",
                content_type="text/html",
                body_preview_redacted="login",
            )
        )
        assert coord._note_hems_auth_failure(
            Unauthorized("rejected"), endpoint="hems_devices"
        )
        coord._note_hems_auth_success()
    assert coord._auth_blocked_until_utc == until


def test_hems_repair_context_without_inventory_view_callback(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    coord._selected_type_keys = None
    with monkeypatch.context() as context:
        context.setattr(coord, "inventory_view", SimpleNamespace())
        assert not coord._hems_auth_repair_context_available()


async def test_clearing_inactive_hems_backoff_has_no_retry_timestamp(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    monkeypatch.setattr(coord, "async_request_refresh", AsyncMock())
    response = await coord.async_clear_hems_auth_backoff()
    assert response["reason"] == "not_active"
    assert "hems_backoff_until" not in response
    response = await coord.async_clear_hems_auth_backoff()
    assert response["reason"] == "manual_clear_cooldown_active"
    assert "hems_backoff_until" not in response


def test_persisting_tokens_without_diagnostics_updates_entry(
    coordinator_factory, config_entry, monkeypatch
):
    coord = coordinator_factory()
    coord.config_entry = config_entry
    with monkeypatch.context() as context:
        context.setattr(coord, "diagnostics", None)
        coord._persist_tokens(AuthTokens(cookie="session=new", access_token="new"))
    assert config_entry.data[CONF_EAUTH] == "new"


def test_inventory_serial_lookup_ignores_blank_serial_fields(coordinator_factory):
    coord = coordinator_factory(serials=[])
    coord._type_device_buckets["iqevse"] = {
        "count": 1,
        "devices": [{"serial_number": " ", "serial": "EV"}],
    }
    assert coord._inventory_evse_serials() == ["EV"]


async def test_coordinator_options_preserve_enabled_repairs_and_grid_controls(
    coordinator_factory, config_entry
):
    from custom_components.enphase_ev.const import (
        OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
        OPT_SYSTEM_EVENT_REPAIR_ISSUES,
    )

    coord = coordinator_factory()
    coord.config_entry = config_entry
    options = {
        OPT_GRID_PROFILE_CONTROLS_ENABLED: True,
        OPT_DEGRADED_SERVICE_REPAIR_ISSUES: True,
        OPT_SYSTEM_EVENT_REPAIR_ISSUES: True,
    }
    object.__setattr__(config_entry, "options", options)
    await coord.async_apply_config_entry_options(previous_options=options)


async def test_optional_grid_metadata_refresh_propagates_transport_error(
    coordinator_factory, config_entry, monkeypatch
):
    coord = coordinator_factory()
    coord.config_entry = config_entry
    object.__setattr__(
        config_entry, "options", {OPT_GRID_PROFILE_CONTROLS_ENABLED: True}
    )
    monkeypatch.setattr(
        coord.grid_profile_runtime,
        "async_refresh",
        AsyncMock(side_effect=RuntimeError("unavailable")),
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        await coord.async_refresh_grid_profile_metadata()


async def test_completed_first_refresh_followup_can_be_joined(coordinator_factory):
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    context = SimpleNamespace(first_refresh_followups_task=task)
    await coordinator_factory()._async_cancel_first_refresh_followups(context)
    assert context.first_refresh_followups_task is None


def test_refresh_performance_without_request_metrics(coordinator_factory):
    coord = coordinator_factory()
    context = SimpleNamespace(
        performance_recorded=False,
        phase_timings={},
        request_metrics=None,
        started_mono=time.monotonic(),
        fast_poll=False,
        refresh_started_utc=datetime.now(timezone.utc),
        first_refresh=False,
    )
    coord._record_refresh_pipeline_performance(context, outcome="success")
    assert context.performance_recorded


def test_amp_summary_requires_previous_mapping(coordinator_factory):
    coord = coordinator_factory(serials=["EV"], data={"EV": None})
    coord.iter_serials = lambda: ["EV"]
    assert coord._summary_refresh_needed_for_amp_bounds(
        {"EV": {"charging_amps_supported": True}}
    )


async def test_sparse_status_and_summary_fields(coordinator_factory, monkeypatch):
    from unittest.mock import Mock

    coord = coordinator_factory(serials=["EV"])
    coord.iter_serials = lambda: ["EV"]
    coord._schedule_session_enrichment = Mock()
    coord.client.status = AsyncMock(
        return_value={
            "ts": {},
            "evChargerData": [
                {
                    "sn": "EV",
                    "commissioned": False,
                    "charging": False,
                    "sch_d": {"info": [{"remindFlag": False}]},
                }
            ],
        }
    )
    coord.client.summary_v2 = AsyncMock(
        return_value=[
            {
                "serialNumber": "EV",
                "networkConfig": 'not-json\n\n""\nplain\n',
                "functionalValDetails": {},
                "gatewayConnectivityDetails": [
                    {},
                    {"gwConnStatus": 1},
                    {"gwConnFailureReason": 0},
                    {"lastConnTime": 200},
                    {"lastConnTime": 100},
                ],
                "lifeTimeConsumption": "invalid",
            }
        ]
    )
    coord.evse_runtime.async_resolve_charge_modes = AsyncMock(
        return_value={"EV": "invalid"}
    )
    coord.evse_runtime.async_resolve_green_battery_settings = AsyncMock(return_value={})
    monkeypatch.setattr(
        coord.evse_runtime,
        "auth_settings_lookup_candidates",
        lambda serials, **kwargs: list(serials),
    )
    coord.evse_runtime.async_resolve_auth_settings = AsyncMock(
        return_value={"EV": (None, None, True, True)}
    )
    coord.evse_runtime.async_resolve_charger_config = AsyncMock(return_value={})
    monkeypatch.setattr(coord.refresh_runner, "async_run_refresh_plan", AsyncMock())
    monkeypatch.setattr(
        coord, "_summary_refresh_needed_for_amp_bounds", Mock(return_value=True)
    )
    result = await coord._async_update_data()
    assert result["EV"]["commissioned"] is False
    assert result["EV"]["gateway_last_connection_at"] == 200
    assert "lifetime_kwh" not in result["EV"]
    coord.client.summary_v2.return_value = [
        {
            "serialNumber": "EV",
            "networkConfig": [None],
            "gatewayConnectivityDetails": [{}],
        }
    ]
    coord.evse_runtime.async_resolve_auth_settings.return_value = {
        "EV": (None, None, False, False)
    }
    coord._summary_cache_until = None
    result = await coord._async_update_data()
    assert result["EV"]["gateway_connected_count"] == 0
    coord.evse_runtime.async_resolve_auth_settings.return_value = {
        "EV": (None, None, True, True)
    }
    coord.client.status.return_value["evChargerData"][0]["session_d"] = {
        "e_c": "invalid"
    }
    coord.client.summary_v2.return_value = [
        {"serialNumber": "EV", "modelId": "IQ-EVSE-EU-3032"}
    ]
    coord._summary_cache_until = None
    result = await coord._async_update_data()
    assert result["EV"]["session_kwh"] == "invalid"


async def test_metadata_refresh_succeeds_with_request_scopes(
    coordinator_factory, config_entry, monkeypatch
):
    coord = coordinator_factory()
    coord.config_entry = config_entry
    object.__setattr__(
        config_entry, "options", {OPT_GRID_PROFILE_CONTROLS_ENABLED: True}
    )
    monkeypatch.setattr(coord.grid_profile_runtime, "async_refresh", AsyncMock())
    await coord.async_refresh_grid_profile_metadata()
    coord.grid_profile_runtime.async_refresh.assert_awaited_once_with(
        force=False, load_profiles=True
    )


def test_coordinator_client_without_reauthentication_callback(
    coordinator_factory, monkeypatch
):
    monkeypatch.setattr(
        module, "EnphaseEVClient", lambda *args, **kwargs: SimpleNamespace()
    )
    assert coordinator_factory().site_id


async def test_cleanup_handles_completed_entry_tasks_and_legacy_metadata_task(
    coordinator_factory,
):
    from unittest.mock import Mock

    coord = coordinator_factory()
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    coord._entry_background_tasks.add(done)
    legacy = SimpleNamespace(done=lambda: False, cancel=Mock())
    coord._grid_profile_metadata_task = legacy
    await coord.async_cleanup_runtime_state()
    legacy.cancel.assert_called_once()
    assert not coord._entry_background_tasks


async def test_reload_without_optional_task_collections(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    with monkeypatch.context() as context:
        context.setattr(coord, "_entry_background_tasks", None)
        context.setattr(coord.session_history, "_enrichment_tasks", None)
        assert await coord.async_quiesce_for_reload()


async def test_reload_retains_new_task_replacing_cancelled_task(coordinator_factory):
    coord = coordinator_factory()
    ready = asyncio.Event()
    replacement = asyncio.create_task(asyncio.sleep(0))
    await replacement

    async def work():
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            coord._warmup_task = replacement

    old = asyncio.create_task(work())
    coord._warmup_task = old
    await ready.wait()
    assert await coord.async_quiesce_for_reload()
    assert coord._warmup_task is replacement


def test_warmup_invalid_lifetime_does_not_publish_energy(coordinator_factory):
    coord = coordinator_factory(data={"EV": {}})
    coord.apply_warmup_evse_summary(
        [{"serialNumber": "EV", "lifeTimeConsumption": "invalid"}], coord.data
    )
    assert "lifetime_kwh" not in coord.data["EV"]


def test_emea_reserve_editable_without_restrict_control(coordinator_factory):
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"
    coord._battery_user_is_owner = True
    coord._battery_is_emea = True
    coord._battery_cfg_control = BatteryControlCapability(show=True)
    coord._battery_rbd_control = BatteryControlCapability(show=True)
    assert coord.battery_reserve_editable


async def test_site_refresh_skips_empty_followup_plan(coordinator_factory, monkeypatch):
    coord = coordinator_factory(serials=[])
    context = coord._start_refresh_pipeline()
    context.first_refresh = False
    monkeypatch.setattr(coord.energy, "_async_refresh_site_energy", AsyncMock())
    monkeypatch.setattr(
        module,
        "build_site_only_followup_plan",
        lambda *args, **kwargs: SimpleNamespace(stages=()),
    )
    monkeypatch.setattr(coord.refresh_runner, "async_run_refresh_plan", AsyncMock())
    assert await coord._async_run_site_only_refresh_pipeline(context) == {}
    coord.refresh_runner.async_run_refresh_plan.assert_not_awaited()


def test_hems_auth_failure_without_optional_clear_issue_callback(
    coordinator_factory, config_entry, monkeypatch
):
    coord = coordinator_factory()
    from custom_components.enphase_ev.const import OPT_DEGRADED_SERVICE_REPAIR_ISSUES

    coord.config_entry = config_entry
    object.__setattr__(
        config_entry, "options", {OPT_DEGRADED_SERVICE_REPAIR_ISSUES: True}
    )
    monkeypatch.setattr(coord, "_hems_auth_repair_context_available", lambda: False)
    with monkeypatch.context() as context:
        context.setattr(coord.diagnostics, "clear_hems_auth_degraded_issue", None)
        assert coord._note_hems_auth_failure(Unauthorized(), endpoint="hems_devices")


async def test_invalid_status_response_does_not_replace_cached_payload(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    cached = {"evChargerData": [{"sn": "EV"}]}
    coord._status_payload_cache = cached
    coord.client.status = AsyncMock(return_value=[])
    monkeypatch.setattr(coord, "_async_run_post_status_refresh_pipeline", AsyncMock())
    with pytest.raises(AttributeError, match="get"):
        await coord._async_update_data()
    assert coord._status_payload_cache is cached
