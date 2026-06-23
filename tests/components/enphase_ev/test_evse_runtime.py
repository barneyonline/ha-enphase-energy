from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call

import aiohttp
import pytest

from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.api import SchedulerUnavailable
from custom_components.enphase_ev.evse_runtime import (
    EVSE_LOOKUP_CONCURRENCY,
    FAST_TOGGLE_POLL_HOLD_S,
    ChargeModeResolution,
    ChargeModeStartPreferences,
    EvseRuntime,
    evse_power_is_actively_charging,
)


@pytest.fixture(autouse=True)
def _force_utc_timezone() -> None:
    dt_util.set_default_time_zone(UTC)


def _client_response_error(status: int, *, message: str = "", headers=None):
    req = aiohttp.RequestInfo(
        url=aiohttp.client.URL("https://example"),
        method="GET",
        headers={},
        real_url=aiohttp.client.URL("https://example"),
    )
    return aiohttp.ClientResponseError(
        request_info=req,
        history=(),
        status=status,
        message=message,
        headers=headers or {},
    )


def test_evse_runtime_helper_paths(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.evse_runtime

    coord.data = {
        "EV1": {
            "min_amp": "10",
            "max_amp": "40",
            "charging_level": "18",
            "session_charge_level": "20",
            "plugged": True,
            "charge_mode": "scheduled",
        }
    }
    coord.last_set_amps["EV1"] = 24
    coord._charge_mode_cache["EV1"] = ("GREEN_CHARGING", 10.0)  # noqa: SLF001

    assert runtime.normalize_serials([None, " EV1 ", "EV1"]) == {"EV1"}
    assert runtime.session_history_day(
        {"charging": True}, datetime(2025, 1, 1, tzinfo=UTC)
    ) == datetime(2025, 1, 1, tzinfo=UTC)
    assert runtime.coerce_amp("16") == 16
    assert runtime.amp_limits("EV1") == (10, 40)
    assert runtime.apply_amp_limits("EV1", 50) == 40
    assert runtime.pick_start_amps("EV1", requested=None, fallback=16) == 24
    assert runtime.normalize_charge_mode_preference("scheduled") == "SCHEDULED_CHARGING"
    assert runtime.normalize_charge_mode_preference("smart") == "SMART_CHARGING"
    assert runtime.normalize_effective_charge_mode(0) == "MANUAL_CHARGING"
    assert runtime.normalize_effective_charge_mode(1) == "GREEN_CHARGING"
    assert runtime.normalize_effective_charge_mode("1") == "GREEN_CHARGING"
    assert runtime.normalize_effective_charge_mode(False) is None
    assert runtime.normalize_effective_charge_mode("idle") == "IDLE"
    assert runtime.resolve_charge_mode_pref("EV1") == "SCHEDULED_CHARGING"
    prefs = runtime.charge_mode_start_preferences("EV1")
    assert prefs == ChargeModeStartPreferences(
        mode="SCHEDULED_CHARGING",
        include_level=True,
        strict=False,
        enforce_mode="SCHEDULED_CHARGING",
    )


@pytest.mark.asyncio
async def test_evse_runtime_green_battery_write_rebuilds_pending_dict(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.evse_runtime
    coord._green_battery_pending = None  # noqa: SLF001
    coord.client.set_green_battery_setting = AsyncMock(return_value={"status": "ok"})
    coord.async_request_refresh = AsyncMock()

    await runtime.async_set_green_battery_setting("EV1", enabled=True)

    assert coord._green_battery_pending["EV1"][0] is True  # noqa: SLF001
    coord.client.set_green_battery_setting.assert_awaited_once_with("EV1", enabled=True)
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_evse_runtime_green_battery_write_marks_scheduler_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.set_green_battery_setting = AsyncMock(
        side_effect=SchedulerUnavailable("down")
    )

    with pytest.raises(SchedulerUnavailable):
        await coord.evse_runtime.async_set_green_battery_setting("EV1", enabled=True)

    assert coord.scheduler_available is False


def test_evse_runtime_battery_profile_charge_mode_preference_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime

    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord._storm_guard_cache_until = 10.0**12  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "GREEN", "enable": True}
    ]
    assert runtime.battery_profile_charge_mode_preference("EV1") == "GREEN_CHARGING"

    coord._storm_guard_cache_until = 0.0  # noqa: SLF001
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord._storm_guard_cache_until = 10.0**12  # noqa: SLF001
    coord._battery_profile_devices = ["bad"]  # noqa: SLF001
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "GREEN", "enable": True},
        {"uuid": "evse-2", "chargeMode": "MANUAL", "enable": True},
    ]
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord.serials.add("EV2")
    coord._configured_serials = {"EV1"}  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "GREEN", "enable": True}
    ]
    assert runtime.battery_profile_charge_mode_preference("EV1") == "GREEN_CHARGING"

    coord._configured_serials = {"EV1", "EV2"}  # noqa: SLF001
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord._configured_serials = {"EV1"}  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "SMART", "enable": True}
    ]
    assert runtime.battery_profile_charge_mode_preference("EV1") == "SMART_CHARGING"


def test_evse_runtime_battery_profile_charge_mode_preference_error_paths() -> None:
    runtime = EvseRuntime(
        SimpleNamespace(
            _configured_serials=set(),
            serials={"EV1", "EV2"},
            _storm_guard_cache_until=10.0**12,
            _battery_profile_devices=[{"chargeMode": "GREEN"}],
        )
    )
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    runtime = EvseRuntime(
        SimpleNamespace(
            _configured_serials={"EV1"},
            serials={"EV1"},
            _storm_guard_cache_until="bad",
            _battery_profile_devices=[{"chargeMode": "GREEN"}],
        )
    )
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    class _BrokenDevices:
        _configured_serials = {"EV1"}
        serials = {"EV1"}
        _storm_guard_cache_until = 10.0**12

        @property
        def _battery_profile_devices(self):
            raise RuntimeError("boom")

    runtime = EvseRuntime(_BrokenDevices())
    assert runtime.battery_profile_charge_mode_preference("EV1") is None


def test_evse_runtime_schedule_type_charge_mode_preference_paths(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().evse_runtime

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    assert (
        runtime.schedule_type_charge_mode_preference("greencharging")
        == "GREEN_CHARGING"
    )
    assert (
        runtime.schedule_type_charge_mode_preference("GREEN_CHARGING")
        == "GREEN_CHARGING"
    )
    assert runtime.schedule_type_charge_mode_preference("   ") is None
    assert runtime.schedule_type_charge_mode_preference("CUSTOM") is None
    assert runtime.schedule_type_charge_mode_preference(BadStr()) is None


@pytest.mark.asyncio
async def test_evse_runtime_resolvers_use_runtime_methods(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1", "EV2"])
    runtime = coord.evse_runtime

    runtime.async_get_charge_mode = AsyncMock(return_value="GREEN_CHARGING")  # type: ignore[method-assign]
    runtime.async_get_green_battery_setting = AsyncMock(  # type: ignore[method-assign]
        return_value=(True, True)
    )
    runtime.async_get_auth_settings = AsyncMock(  # type: ignore[method-assign]
        return_value=(True, False, True, True)
    )

    assert await runtime.async_resolve_charge_modes(["EV1"]) == {
        "EV1": ChargeModeResolution("GREEN_CHARGING", "scheduler_endpoint")
    }
    assert await runtime.async_resolve_green_battery_settings(["EV1"]) == {
        "EV1": (True, True)
    }
    assert await runtime.async_resolve_auth_settings(["EV1"]) == {
        "EV1": (True, False, True, True)
    }

    runtime.async_get_charge_mode.assert_awaited_once_with("EV1")
    runtime.async_get_green_battery_setting.assert_awaited_once_with("EV1")
    runtime.async_get_auth_settings.assert_awaited_once_with("EV1")


@pytest.mark.asyncio
async def test_evse_runtime_session_history_helpers_pass_max_cache_age(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    manager = SimpleNamespace(
        async_enrich=AsyncMock(return_value={"EV1": [{"energy_kwh": 1.0}]}),
        _async_fetch_sessions_today=AsyncMock(return_value=[{"energy_kwh": 1.0}]),
        schedule_enrichment=MagicMock(),
        schedule_enrichment_with_options=MagicMock(),
    )
    coord.session_history = manager
    day = datetime(2025, 1, 1, tzinfo=UTC)

    runtime.schedule_session_enrichment(["EV1"], day, max_cache_age=120.0)
    result = await runtime.async_enrich_sessions(
        ["EV1"],
        day,
        in_background=True,
        max_cache_age=120.0,
    )
    sessions = await runtime.async_fetch_sessions_today(
        "EV1",
        day_local=day,
        max_cache_age=120.0,
    )

    manager.schedule_enrichment_with_options.assert_called_once_with(
        ["EV1"],
        day_local=day,
        max_cache_age=120.0,
    )
    manager.async_enrich.assert_awaited_once_with(
        ["EV1"],
        day,
        in_background=True,
        max_cache_age=120.0,
    )
    manager._async_fetch_sessions_today.assert_awaited_once_with(
        "EV1",
        day_local=day,
        max_cache_age=120.0,
    )
    assert result == {"EV1": [{"energy_kwh": 1.0}]}
    assert sessions == [{"energy_kwh": 1.0}]


@pytest.mark.asyncio
async def test_evse_runtime_async_fetch_sessions_today_ignores_invalid_max_cache_age(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    manager = SimpleNamespace(
        _async_fetch_sessions_today=AsyncMock(return_value=[{"energy_kwh": 2.0}]),
        cache_ttl=600,
    )
    coord.session_history = manager
    day = datetime(2025, 1, 2, tzinfo=UTC)

    sessions = await runtime.async_fetch_sessions_today(
        "EV1",
        day_local=day,
        max_cache_age="bad",
    )

    manager._async_fetch_sessions_today.assert_awaited_once_with(
        "EV1",
        day_local=day,
        max_cache_age="bad",
    )
    assert sessions == [{"energy_kwh": 2.0}]


@pytest.mark.asyncio
async def test_evse_runtime_run_lookup_tasks_handles_empty_input(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().evse_runtime

    assert await runtime._run_lookup_tasks({}) == {}  # noqa: SLF001


@pytest.mark.parametrize(
    ("resolver_name", "getter_name", "kwargs", "expected"),
    [
        (
            "async_resolve_charge_modes",
            "async_get_charge_mode",
            {},
            lambda _sn: "GREEN_CHARGING",
        ),
        (
            "async_resolve_green_battery_settings",
            "async_get_green_battery_setting",
            {},
            lambda _sn: (True, True),
        ),
        (
            "async_resolve_auth_settings",
            "async_get_auth_settings",
            {},
            lambda _sn: (True, False, True, True),
        ),
        (
            "async_resolve_charger_config",
            "async_get_charger_config",
            {"keys": ["DefaultChargeLevel"]},
            lambda _sn: {"DefaultChargeLevel": 80},
        ),
    ],
)
@pytest.mark.asyncio
async def test_evse_runtime_resolvers_limit_lookup_concurrency(
    coordinator_factory,
    resolver_name,
    getter_name,
    kwargs,
    expected,
) -> None:
    coord = coordinator_factory(serials=[f"EV{i:02d}" for i in range(12)])
    runtime = coord.evse_runtime
    current = 0
    peak = 0

    async def _fake_get(sn: str, **_kwargs):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        current -= 1
        return expected(sn)

    setattr(runtime, getter_name, _fake_get)

    resolver = getattr(runtime, resolver_name)
    serials = [f"EV{i:02d}" for i in range(12)]
    result = await resolver(serials, **kwargs)

    assert peak == EVSE_LOOKUP_CONCURRENCY
    expected_value = expected(serials[0])
    if resolver_name == "async_resolve_charge_modes":
        assert result[serials[0]] == ChargeModeResolution(
            expected_value,
            "scheduler_endpoint",
        )
    else:
        assert result[serials[0]] == expected_value


@pytest.mark.asyncio
async def test_evse_runtime_start_stop_and_auto_resume_use_coordinator_hooks(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.data = {
        "EV1": {
            "plugged": True,
            "display_name": "Driveway",
            "charge_mode_pref": "SCHEDULED_CHARGING",
        }
    }
    coord.pick_start_amps = MagicMock(return_value=28)
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_start_streaming = AsyncMock()
    coord._ensure_charge_mode = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.require_plugged = MagicMock()
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})

    await runtime.async_start_charging("EV1", fallback_amps=24)
    await runtime.async_stop_charging("EV1")
    await runtime.async_auto_resume("EV1", {"plugged": True})

    assert coord.pick_start_amps.call_count == 2
    coord.require_plugged.assert_any_call("EV1")
    coord.set_last_set_amps.assert_any_call("EV1", 28)
    coord.set_desired_charging.assert_any_call("EV1", True)
    coord.set_desired_charging.assert_any_call("EV1", False)
    coord.async_start_streaming.assert_any_await(
        manual=False,
        serial="EV1",
        expected_state=True,
    )
    coord.async_start_streaming.assert_any_await(
        manual=False,
        serial="EV1",
        expected_state=False,
    )
    coord._ensure_charge_mode.assert_awaited()
    assert coord.async_request_refresh.await_count == 3


@pytest.mark.asyncio
async def test_evse_runtime_start_charging_noops_when_already_charging(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.data = {
        "EV1": {
            "plugged": True,
            "connector_status": "CHARGING",
            "charging": True,
            "charge_mode_pref": "MANUAL_CHARGING",
        }
    }
    coord.require_plugged = MagicMock()
    coord.pick_start_amps = MagicMock(return_value=28)
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.client.start_charging = AsyncMock()

    result = await runtime.async_start_charging("EV1")

    assert result == {"status": "already_charging"}
    coord.require_plugged.assert_called_once_with("EV1")
    coord.client.start_charging.assert_not_awaited()
    coord.pick_start_amps.assert_not_called()
    coord.set_desired_charging.assert_called_once_with("EV1", True)
    coord.set_charging_expectation.assert_called_once_with(
        "EV1",
        True,
        hold_for=90.0,
    )


@pytest.mark.asyncio
async def test_evse_runtime_start_charging_sends_explicit_amps_when_already_charging(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.data = {
        "EV1": {
            "plugged": True,
            "connector_status": "CHARGING",
            "charging": True,
            "charge_mode_pref": "MANUAL_CHARGING",
        }
    }
    coord.require_plugged = MagicMock()
    coord.pick_start_amps = MagicMock(return_value=24)
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})

    await runtime.async_start_charging("EV1", requested_amps=24)

    coord.client.start_charging.assert_awaited_once_with(
        "EV1",
        24,
        1,
        include_level=True,
        strict_preference=True,
    )
    coord.set_last_set_amps.assert_called_once_with("EV1", 24)


@pytest.mark.asyncio
async def test_evse_runtime_start_charging_invalid_level_falls_back_and_caches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.data = {"EV1": {"plugged": True, "charge_mode_pref": "MANUAL_CHARGING"}}
    coord.pick_start_amps = MagicMock(return_value=28)
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.require_plugged = MagicMock()
    coord.client.start_charging = AsyncMock(
        side_effect=[
            _client_response_error(
                500,
                message='{"error":{"displayMessage":"Invalid charge level","code":"500"}}',
            ),
            {"status": "ok"},
        ]
    )

    await runtime.async_start_charging("EV1")

    assert coord.client.start_charging.await_args_list[0] == call(
        "EV1",
        28,
        1,
        include_level=True,
        strict_preference=False,
    )
    assert coord.client.start_charging.await_args_list[1] == call(
        "EV1",
        28,
        1,
        include_level=False,
        strict_preference=True,
    )
    assert coord._start_without_level_fallback == {"EV1": True}  # noqa: SLF001


@pytest.mark.asyncio
async def test_evse_runtime_start_charging_invalid_level_flag_falls_back(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.data = {"EV1": {"plugged": True, "charge_mode_pref": "MANUAL_CHARGING"}}
    coord.pick_start_amps = MagicMock(return_value=28)
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.require_plugged = MagicMock()
    invalid_level_error = _client_response_error(
        500,
        message="HTTP error from Enphase endpoint (status=500, body_length=61)",
    )
    invalid_level_error.enphase_invalid_charge_level = True
    coord.client.start_charging = AsyncMock(
        side_effect=[invalid_level_error, {"status": "ok"}]
    )

    await runtime.async_start_charging("EV1")

    assert coord.client.start_charging.await_args_list[1] == call(
        "EV1",
        28,
        1,
        include_level=False,
        strict_preference=True,
    )
    assert coord._start_without_level_fallback == {"EV1": True}  # noqa: SLF001


@pytest.mark.asyncio
async def test_evse_runtime_start_charging_uses_cached_no_level_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.data = {"EV1": {"plugged": True, "charge_mode_pref": "MANUAL_CHARGING"}}
    coord.pick_start_amps = MagicMock(return_value=28)
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.require_plugged = MagicMock()
    coord._start_without_level_fallback = {"EV1": True}  # noqa: SLF001
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})

    await runtime.async_start_charging("EV1")

    coord.client.start_charging.assert_awaited_once_with(
        "EV1",
        28,
        1,
        include_level=False,
        strict_preference=True,
    )
    assert coord._start_without_level_fallback == {"EV1": True}  # noqa: SLF001


@pytest.mark.asyncio
async def test_evse_runtime_explicit_start_clears_cached_no_level_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.data = {"EV1": {"plugged": True, "charge_mode_pref": "MANUAL_CHARGING"}}
    coord.pick_start_amps = MagicMock(return_value=24)
    coord.set_last_set_amps = MagicMock()
    coord.set_desired_charging = MagicMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.require_plugged = MagicMock()
    coord._start_without_level_fallback = {"EV1": True}  # noqa: SLF001
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})

    await runtime.async_start_charging("EV1", requested_amps=24)

    coord.client.start_charging.assert_awaited_once_with(
        "EV1",
        24,
        1,
        include_level=True,
        strict_preference=True,
    )
    assert coord._start_without_level_fallback == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_evse_runtime_start_charging_reraises_non_fallback_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.data = {"EV1": {"plugged": True, "charge_mode_pref": "MANUAL_CHARGING"}}
    coord.pick_start_amps = MagicMock(return_value=28)
    coord.require_plugged = MagicMock()
    coord.client.start_charging = AsyncMock(
        side_effect=_client_response_error(
            500,
            message='{"error":{"displayMessage":"Backend unavailable","code":"500"}}',
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await runtime.async_start_charging("EV1")

    coord.client.start_charging.assert_awaited_once_with(
        "EV1",
        28,
        1,
        include_level=True,
        strict_preference=False,
    )


@pytest.mark.asyncio
async def test_evse_runtime_start_charging_maps_client_errors_to_validation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.data = {"EV1": {"plugged": True, "charge_mode_pref": "MANUAL_CHARGING"}}
    coord.pick_start_amps = MagicMock(return_value=28)
    coord.require_plugged = MagicMock()
    coord.client.start_charging = AsyncMock(
        side_effect=_client_response_error(
            404,
            message="HTTP error from Enphase endpoint (status=404)",
        )
    )

    with pytest.raises(ServiceValidationError) as err:
        await runtime.async_start_charging("EV1")

    assert err.value.translation_key == "start_charging_rejected"
    assert err.value.translation_placeholders == {"status": "404"}
    coord.client.start_charging.assert_awaited_once_with(
        "EV1",
        28,
        1,
        include_level=True,
        strict_preference=False,
    )


@pytest.mark.asyncio
async def test_evse_runtime_schedule_amp_restart_uses_coordinator_override(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.evse_runtime
    pending = asyncio.Future()
    coord._amp_restart_tasks["EV1"] = pending  # noqa: SLF001
    calls: list[tuple[str, float]] = []

    async def _fake_restart(sn: str, delay: float) -> None:
        calls.append((sn, delay))

    coord.__dict__["_async_restart_after_amp_change"] = _fake_restart
    tasks: list[asyncio.Task[None]] = []
    names: list[str | None] = []

    def _capture(coro, name=None):
        names.append(name)
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(coord.hass, "async_create_task", _capture)

    runtime.schedule_amp_restart("EV1", delay=12)

    assert pending.cancelled()
    await tasks[0]
    assert calls == [("EV1", 12)]
    assert names == ["enphase_ev_amp_restart_E...1"]


@pytest.mark.asyncio
async def test_evse_runtime_streaming_and_record_actual_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.evse_runtime
    coord.client.start_live_stream = AsyncMock(return_value={"duration_s": 30})
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "ok"})
    coord.kick_fast = MagicMock()
    coord._schedule_stream_stop = MagicMock()  # noqa: SLF001

    await runtime.async_start_streaming(serial="EV1", expected_state=True)
    runtime.record_actual_charging("EV1", True)
    runtime.record_actual_charging("EV1", False)
    runtime.record_actual_charging("EV1", False)
    await runtime.async_stop_streaming(manual=True)

    coord.kick_fast.assert_called_with(FAST_TOGGLE_POLL_HOLD_S)
    coord._schedule_stream_stop.assert_called_once_with(force=True)
    assert runtime.streaming_active() is False


def test_evse_power_is_actively_charging_coerces_numeric_flags() -> None:
    assert evse_power_is_actively_charging(None, 1) is True
    assert evse_power_is_actively_charging(None, 0) is False


def test_evse_runtime_require_plugged_and_desired_state(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.evse_runtime
    coord.data = {"EV1": {"name": "Garage", "plugged": False}}

    with pytest.raises(ServiceValidationError):
        runtime.require_plugged("EV1")

    runtime.set_desired_charging("EV1", True)
    assert runtime.get_desired_charging("EV1") is True
    runtime.set_desired_charging("EV1", None)
    assert runtime.get_desired_charging("EV1") is None


def test_coordinator_evse_runtime_wrapper_delegation(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = Mock()
    runtime.sum_session_energy.return_value = 1.5
    runtime.retained_session_history_days.return_value = {"2025-01-01"}
    runtime.prune_serial_runtime_state.return_value = {"EV1"}
    runtime.determine_polling_state.return_value = {"target": 60}
    runtime.streaming_active.return_value = True
    runtime.slow_interval_floor.return_value = 60
    runtime.get_desired_charging.return_value = True
    runtime.amp_limits.return_value = (10, 40)
    runtime.apply_amp_limits.return_value = 32
    runtime.pick_start_amps.return_value = 30
    runtime.resolve_charge_mode_pref.return_value = "GREEN_CHARGING"
    runtime.cached_charge_mode_preference.return_value = "GREEN_CHARGING"
    runtime.normalize_effective_charge_mode.return_value = "IDLE"
    runtime.charge_mode_start_preferences.return_value = ChargeModeStartPreferences()
    coord.evse_runtime = runtime

    assert coord._sum_session_energy([]) == 1.5  # noqa: SLF001
    assert coord._retained_session_history_days() == {"2025-01-01"}  # noqa: SLF001
    coord._set_session_history_cache_shim_entry("EV1", "2025-01-01", [])  # noqa: SLF001
    assert coord._prune_serial_runtime_state(["EV1"]) == {"EV1"}  # noqa: SLF001
    assert coord._determine_polling_state({}) == {"target": 60}  # noqa: SLF001
    assert coord._streaming_active() is True  # noqa: SLF001
    coord._clear_streaming_state()  # noqa: SLF001
    assert coord._slow_interval_floor() == 60  # noqa: SLF001
    assert coord.get_desired_charging("EV1") is True
    assert coord._amp_limits("EV1") == (10, 40)  # noqa: SLF001
    assert coord._apply_amp_limits("EV1", 50) == 32  # noqa: SLF001
    assert coord.pick_start_amps("EV1") == 30
    assert coord._resolve_charge_mode_pref("EV1") == "GREEN_CHARGING"  # noqa: SLF001
    assert (
        coord._cached_charge_mode_preference("EV1") == "GREEN_CHARGING"
    )  # noqa: SLF001
    assert coord._normalize_effective_charge_mode("idle") == "IDLE"  # noqa: SLF001
    assert (
        coord._charge_mode_start_preferences("EV1") == ChargeModeStartPreferences()
    )  # noqa: SLF001

    runtime.sum_session_energy.assert_called_once_with([])
    runtime.retained_session_history_days.assert_called_once_with(None)
    runtime.set_session_history_cache_shim_entry.assert_called_once_with(
        "EV1",
        "2025-01-01",
        [],
    )
    runtime.prune_serial_runtime_state.assert_called_once_with(["EV1"])
    runtime.determine_polling_state.assert_called_once_with({})
    runtime.streaming_active.assert_called_once_with()
    runtime.clear_streaming_state.assert_called_once_with()
    runtime.slow_interval_floor.assert_called_once_with()
    runtime.get_desired_charging.assert_called_once_with("EV1")
    runtime.amp_limits.assert_called_once_with("EV1")
    runtime.apply_amp_limits.assert_called_once_with("EV1", 50)
    runtime.pick_start_amps.assert_called_once_with("EV1", None, 32)
    runtime.resolve_charge_mode_pref.assert_called_once_with("EV1")
    runtime.cached_charge_mode_preference.assert_called_once_with("EV1", now=None)
    runtime.normalize_effective_charge_mode.assert_called_once_with("idle")
    runtime.charge_mode_start_preferences.assert_called_once_with("EV1")
