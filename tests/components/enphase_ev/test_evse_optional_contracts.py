"""EVSE state and commands tolerate optional adapters and concurrent cleanup."""

import asyncio
from datetime import datetime, timezone
import time
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.enphase_ev import evse_runtime as module
from custom_components.enphase_ev.const import AUTH_APP_SETTING, GREEN_BATTERY_SETTING
from custom_components.enphase_ev.evse_runtime import ChargeModeStartPreferences
from tests.components.enphase_ev.test_api_client_methods import _make_cre


def test_session_enrichment_without_history_manager(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    with monkeypatch.context() as patch:
        patch.setattr(coord, "session_history", None)
        coord.evse_runtime.schedule_session_enrichment(
            ["EV"], datetime.now(timezone.utc)
        )


async def test_session_adapter_refreshes_expired_cache_after_timezone_fallback(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory(serials=["EV"])
    day = datetime(2026, 10, 31, tzinfo=timezone.utc)
    coord._session_history_cache_shim[("EV", "2026-10-31")] = (0, [{"old": True}])
    with monkeypatch.context() as patch:
        patch.setattr(
            coord.session_history,
            "_async_fetch_sessions_today",
            AsyncMock(return_value=[]),
        )
        patch.setattr(
            module.dt_util, "as_local", Mock(side_effect=[ValueError("timezone"), day])
        )
        assert (
            await coord.evse_runtime.async_fetch_sessions_today("EV", day_local=day)
            == []
        )
    assert coord._session_history_cache_shim[("EV", "2026-10-31")][1] == []


def test_serial_normalization_ignores_whitespace_only_values(coordinator_factory):
    assert coordinator_factory().evse_runtime.normalize_serials(["", " ", "EV"]) == {
        "EV"
    }


def test_pruning_tolerates_absent_auto_resume_task_mapping(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory(serials=["EV"])
    with monkeypatch.context() as patch:
        patch.setattr(coord, "_auto_resume_tasks", [])
        assert coord.evse_runtime.prune_serial_runtime_state(["EV"]) == {"EV"}


@pytest.mark.parametrize("operation", ["start", "stop", "default_current"])
async def test_charger_commands_without_control_tracking_extension(
    coordinator_factory, monkeypatch, operation
):
    coord = coordinator_factory(
        serials=["EV"], data={"EV": {"plugged": True, "min_amp": 6, "max_amp": 32}}
    )
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.set_default_charge_level = AsyncMock(return_value={"data": []})
    monkeypatch.setattr(coord, "async_start_streaming", AsyncMock())
    monkeypatch.setattr(coord, "async_request_refresh", AsyncMock())
    with monkeypatch.context() as patch:
        patch.setattr(coord, "control_updates", None)
        if operation == "start":
            assert await coord.evse_runtime.async_start_charging(
                "EV", requested_amps=16
            ) == {"status": "ok"}
            coord.client.start_charging.assert_awaited_once()
        elif operation == "stop":
            assert await coord.evse_runtime.async_stop_charging("EV") == {
                "status": "ok"
            }
            coord.client.stop_charging.assert_awaited_once_with("EV")
        else:
            await coord.evse_runtime.async_set_default_charge_level("EV", 16)
            coord.client.set_default_charge_level.assert_awaited_once_with("EV", 16)


@pytest.mark.parametrize("cache", [None, {}])
async def test_start_fallback_creates_missing_fallback_cache(
    coordinator_factory, cache
):
    coord = coordinator_factory(serials=["EV"])
    coord._start_without_level_fallback = cache
    coord.client.start_charging = AsyncMock(
        side_effect=[_make_cre(500, "invalid charge level"), {"status": "ok"}]
    )
    assert await coord.evse_runtime.async_issue_start_charging(
        "EV", 16, ChargeModeStartPreferences(include_level=True)
    ) == {"status": "ok"}
    assert coord._start_without_level_fallback == {"EV": True}


@pytest.mark.parametrize("response", [None, {}, {"duration_s": None}])
def test_streaming_uses_default_duration_when_unspecified(response):
    assert (
        module.EvseRuntime.streaming_duration_s(response)
        == module.STREAMING_DEFAULT_DURATION_S
    )


@pytest.mark.parametrize("serial", [None, "EV"])
async def test_streaming_without_expected_state_does_not_add_targets(
    coordinator_factory, serial
):
    coord = coordinator_factory()
    coord.client.start_live_stream = AsyncMock(return_value={"status": "ok"})
    await coord.evse_runtime.async_start_streaming(serial=serial)
    assert coord._streaming_targets == {}


def test_actual_charging_keeps_streaming_until_all_targets_match(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = time.monotonic() + 60
    coord._streaming_targets = {"one": True, "two": False}
    coord.evse_runtime.record_actual_charging("one", True)
    assert coord._streaming_targets == {"two": False}
    assert coord._streaming is True


def test_slow_interval_without_coordinator_interval_uses_default(coordinator_factory):
    coord = coordinator_factory()
    coord.update_interval = None
    assert coord.evse_runtime.slow_interval_floor() > 0


def test_discovered_serial_already_in_order_is_not_duplicated(coordinator_factory):
    coord = coordinator_factory(serials=[])
    coord._serial_order = ["EV"]
    assert coord.evse_runtime.ensure_serial_tracked("EV")
    assert coord._serial_order == ["EV"]


@pytest.mark.parametrize(
    "kind, payload, expected",
    [
        ("green", {}, (None, False)),
        (
            "green",
            [{"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": {}}],
            (None, True),
        ),
        ("auth", {}, None),
        ("auth", [{"key": AUTH_APP_SETTING, "value": {}}], (None, None, True, False)),
    ],
)
async def test_optional_setting_payloads_do_not_treat_objects_as_flags(
    coordinator_factory, kind, payload, expected
):
    coord = coordinator_factory()
    endpoint = "green_charging_settings" if kind == "green" else "charger_auth_settings"
    setattr(coord.client, endpoint, AsyncMock(return_value=payload))
    method = (
        "async_get_green_battery_setting"
        if kind == "green"
        else "async_get_auth_settings"
    )
    assert await getattr(coord.evse_runtime, method)("EV") == expected


@pytest.mark.parametrize("kind", ["green", "auth", "config"])
async def test_failed_optional_lookup_without_cache_does_not_invent_values(
    coordinator_factory, monkeypatch, kind
):
    coord = coordinator_factory(serials=["EV"])
    runtime = coord.evse_runtime
    getter, resolver = {
        "green": (
            "async_get_green_battery_setting",
            "async_resolve_green_battery_settings",
        ),
        "auth": ("async_get_auth_settings", "async_resolve_auth_settings"),
        "config": ("async_get_charger_config", "async_resolve_charger_config"),
    }[kind]
    monkeypatch.setattr(
        runtime, getter, AsyncMock(side_effect=RuntimeError("temporary"))
    )
    kwargs = {"keys": ["requested"]} if kind == "config" else {}
    assert await getattr(runtime, resolver)(["EV"], **kwargs) == {}


async def test_failed_config_lookup_does_not_restore_unrequested_keys(
    coordinator_factory, monkeypatch
):
    runtime = coordinator_factory(serials=["EV"]).evse_runtime
    runtime.state._charger_config_cache["EV"] = ({"other": True}, 0)
    monkeypatch.setattr(
        runtime,
        "async_get_charger_config",
        AsyncMock(side_effect=RuntimeError("temporary")),
    )
    assert await runtime.async_resolve_charger_config(["EV"], keys=["requested"]) == {}


def test_profile_solar_hint_with_discovered_single_charger(coordinator_factory):
    coord = coordinator_factory(serials=["EV"])
    coord._configured_serials = set()
    coord._battery_profile_devices_last_success_mono = None
    assert coord.evse_runtime.battery_profile_charge_mode_preference("EV") is None


@pytest.mark.parametrize("kind", ["stream_stop", "auto_resume"])
async def test_completed_evse_task_does_not_remove_its_replacement(
    coordinator_factory, monkeypatch, kind
):
    coord = coordinator_factory(serials=["EV"])
    runtime = coord.evse_runtime
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait(*args):
        entered.set()
        await release.wait()

    if kind == "stream_stop":
        coord.client.stop_live_stream = wait
        runtime.schedule_stream_stop(force=True)
        old = coord._streaming_stop_task
    else:
        monkeypatch.setattr(runtime, "async_auto_resume", wait)
        runtime.state._desired_charging["EV"] = True
        runtime.sync_desired_charging(
            {
                "EV": {
                    "plugged": True,
                    "charging": False,
                    "connector_status": "SUSPENDED_EVSE",
                }
            }
        )
        old = coord._auto_resume_tasks["EV"]
    await entered.wait()
    replacement = asyncio.create_task(asyncio.sleep(0))
    if kind == "stream_stop":
        coord._streaming_stop_task = replacement
    else:
        coord._auto_resume_tasks["EV"] = replacement
    release.set()
    await asyncio.gather(old, replacement)
    await asyncio.sleep(0)
    assert (
        coord._streaming_stop_task
        if kind == "stream_stop"
        else coord._auto_resume_tasks["EV"]
    ) is replacement
