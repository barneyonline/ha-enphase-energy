from __future__ import annotations

import asyncio
import time
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


async def test_charge_mode_waits_for_fresh_readback(coordinator_factory):
    from custom_components.enphase_ev.select import ChargeModeSelect
    from custom_components.enphase_ev.sensor import EnphaseChargeModeSensor

    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord.last_update_success = True
    coord.data["EV1"]["charge_mode_pref"] = "MANUAL_CHARGING"
    runtime = coord.evse_runtime
    runtime.set_charge_mode_cache("EV1", "MANUAL_CHARGING")
    coord.client.set_charge_mode = AsyncMock()
    coord.client.charge_mode = AsyncMock(
        side_effect=["MANUAL_CHARGING", None, RuntimeError("offline"), "SCHEDULED"]
    )
    coord.async_request_refresh = AsyncMock()
    notifications = Mock()
    coord.async_add_listener(notifications)
    select = ChargeModeSelect(coord, "EV1")
    sensor = EnphaseChargeModeSensor(coord, "EV1")

    await runtime.async_set_charge_mode("EV1", "SCHEDULED_CHARGING")

    assert notifications.called
    assert select.available
    assert ChargeModeSelect(coord, "EV2").available
    assert sensor.native_value == "MANUAL_CHARGING"
    assert sensor.extra_state_attributes["requested_mode"] == "SCHEDULED_CHARGING"
    assert sensor.extra_state_attributes["preferred_mode"] == "MANUAL_CHARGING"
    assert runtime.charge_mode_lookup_candidates(["EV1"]) == ["EV1"]
    assert runtime.determine_polling_state({})["want_fast"] is True
    with pytest.raises(ServiceValidationError, match="awaiting confirmation"):
        await runtime.async_set_charge_mode("EV1", "GREEN_CHARGING")
    coord.client.set_charge_mode.assert_awaited_once()

    for _ in range(3):
        await runtime.async_get_charge_mode("EV1")
        assert sensor.native_value == "MANUAL_CHARGING"
        assert select.available
    assert await runtime.async_get_charge_mode("EV1") == "SCHEDULED_CHARGING"
    assert select.available
    assert sensor.extra_state_attributes["requested_mode"] is None
    assert runtime.snapshot.charge_modes["EV1"] == "SCHEDULED_CHARGING"
    assert not runtime.snapshot.pending_charge_modes


async def test_charge_mode_submission_blocks_confirmation(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(*_args, **_kwargs):
        entered.set()
        await release.wait()

    coord.client.set_charge_mode = write
    coord.client.charge_mode = AsyncMock(return_value="SCHEDULED_CHARGING")
    coord.async_request_refresh = AsyncMock()
    task = asyncio.create_task(
        runtime.async_set_charge_mode("EV1", "SCHEDULED_CHARGING")
    )
    await entered.wait()
    assert await runtime.async_get_charge_mode("EV1") is None
    coord.client.charge_mode.assert_not_awaited()
    assert runtime.snapshot.pending_charge_modes["EV1"] == "SCHEDULED_CHARGING"
    release.set()
    await task
    assert await runtime.async_get_charge_mode("EV1") == "SCHEDULED_CHARGING"
    assert not runtime.snapshot.pending_charge_modes


@pytest.mark.parametrize(
    "error",
    [RuntimeError("failed"), SchedulerUnavailable("down"), asyncio.CancelledError()],
)
async def test_charge_mode_submission_failure_clears_pending(
    coordinator_factory, error
):
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    runtime.set_charge_mode_cache("EV1", "MANUAL_CHARGING")
    coord.client.set_charge_mode = AsyncMock(side_effect=error)
    coord.async_request_refresh = AsyncMock()
    with pytest.raises(type(error)):
        await runtime.async_set_charge_mode("EV1", "SCHEDULED_CHARGING")
    assert not runtime.snapshot.pending_charge_modes
    assert runtime.snapshot.charge_modes["EV1"] == "MANUAL_CHARGING"
    assert not runtime._charge_mode_submitting
    coord.async_request_refresh.assert_not_awaited()


async def test_charge_mode_ignores_lookup_started_before_request(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(_sn):
        entered.set()
        await release.wait()
        return "SCHEDULED_CHARGING"

    coord.client.charge_mode = read
    coord.client.set_charge_mode = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    lookup = asyncio.create_task(runtime.async_get_charge_mode("EV1"))
    await entered.wait()
    await runtime.async_set_charge_mode("EV1", "SCHEDULED_CHARGING")
    release.set()
    assert await lookup is None
    assert runtime.snapshot.pending_charge_modes["EV1"] == "SCHEDULED_CHARGING"
    assert not runtime.snapshot.charge_modes


def test_charge_mode_confirmation_fast_polling_is_bounded(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    monkeypatch.setattr(time, "monotonic", lambda: 700.0)
    runtime.state._charge_mode_pending["EV1"] = ("SCHEDULED_CHARGING", 100.0)
    assert runtime.determine_polling_state({})["want_fast"] is False
    assert runtime.snapshot.pending_charge_modes["EV1"] == "SCHEDULED_CHARGING"


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
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "custom_components.enphase_ev.evse_runtime.time.monotonic",
        lambda: clock["now"],
    )

    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    coord._battery_profile_devices_last_success_mono = 1000.0  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "GREEN", "enable": True}
    ]
    assert runtime.battery_profile_charge_mode_preference("EV1") == "GREEN_CHARGING"

    clock["now"] = 1299.0
    assert runtime.battery_profile_charge_mode_preference("EV1") == "GREEN_CHARGING"

    clock["now"] = 1300.0
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    clock["now"] = 999.0
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    clock["now"] = 1000.0
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
            _battery_profile_devices_last_success_mono=time.monotonic(),
            _battery_profile_devices=[{"chargeMode": "GREEN"}],
        )
    )
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    runtime = EvseRuntime(
        SimpleNamespace(
            _configured_serials={"EV1"},
            serials={"EV1"},
            _battery_profile_devices_last_success_mono="bad",
            _battery_profile_devices=[{"chargeMode": "GREEN"}],
        )
    )
    assert runtime.battery_profile_charge_mode_preference("EV1") is None

    class _BrokenDevices:
        _configured_serials = {"EV1"}
        serials = {"EV1"}
        _battery_profile_devices_last_success_mono = time.monotonic()

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


@pytest.mark.parametrize("phase", ["stop", "delay", "start"])
async def test_amp_restart_cancellation_propagates(
    coordinator_factory, monkeypatch, phase
):
    """Cancellation at each await must stop the real restart sequence."""
    coord = coordinator_factory(serials=["EV1"])
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def blocked(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    coord.async_stop_charging = AsyncMock(
        side_effect=blocked if phase == "stop" else None
    )
    coord.async_start_charging = AsyncMock(
        side_effect=blocked if phase == "start" else None
    )
    if phase == "delay":
        monkeypatch.setattr(
            "custom_components.enphase_ev.evse_runtime.asyncio",
            SimpleNamespace(**(vars(asyncio) | {"sleep": blocked})),
        )
    task = asyncio.create_task(
        coord.evse_runtime.async_restart_after_amp_change(
            "EV1", 30 if phase == "delay" else 0
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert stopped.is_set()
    assert coord.async_start_charging.await_count == (1 if phase == "start" else 0)


async def test_replacing_amp_restart_cancels_old_sequence(
    coordinator_factory, monkeypatch
):
    """Only the replacement restart may send a start command."""
    coord = coordinator_factory(serials=["EV1"])
    sleeping = asyncio.Event()
    release = asyncio.Event()
    original_sleep = asyncio.sleep

    async def delay(_seconds):
        sleeping.set()
        await release.wait()

    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.evse_runtime.asyncio",
        SimpleNamespace(**(vars(asyncio) | {"sleep": delay})),
    )
    coord.schedule_amp_restart("EV1", delay=30)
    old = coord._amp_restart_tasks["EV1"]
    await asyncio.wait_for(sleeping.wait(), 1)
    coord.schedule_amp_restart("EV1", delay=30)
    current = coord._amp_restart_tasks["EV1"]
    with pytest.raises(asyncio.CancelledError):
        await old
    assert coord._amp_restart_tasks["EV1"] is current
    coord.async_start_charging.assert_not_awaited()
    release.set()
    await asyncio.wait_for(current, 1)
    await original_sleep(0)
    coord.async_start_charging.assert_awaited_once_with("EV1")
    assert coord._amp_restart_tasks == {}


@pytest.mark.parametrize("command", ["start", "stop"])
@pytest.mark.parametrize("phase", ["stop", "delay"])
async def test_explicit_charging_intent_supersedes_amp_restart(
    coordinator_factory, monkeypatch, command, phase
):
    """New commands cancel the actual restart during either stop or delay."""
    coord = coordinator_factory(serials=["EV1"])
    coord.data = {"EV1": {"plugged": True, "charging": True}}
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def stop(_sn):
        calls.append("stop")
        if phase == "stop" and len(calls) == 1:
            entered.set()
            await release.wait()
        return {"status": "ok"}

    async def start(*_args, **_kwargs):
        calls.append("start")
        return {"status": "ok"}

    async def delay(_seconds):
        entered.set()
        await asyncio.Event().wait()

    coord.client.stop_charging = AsyncMock(side_effect=stop)
    coord.client.start_charging = AsyncMock(side_effect=start)
    monkeypatch.setattr(
        "custom_components.enphase_ev.evse_runtime.asyncio",
        SimpleNamespace(**(vars(asyncio) | {"sleep": delay})),
    )
    coord.schedule_amp_restart("EV1")
    restart = coord._amp_restart_tasks["EV1"]
    await asyncio.wait_for(entered.wait(), 1)
    latest = asyncio.create_task(getattr(coord, f"async_{command}_charging")("EV1"))
    await asyncio.sleep(0)
    if phase == "stop":
        assert not latest.done()
        release.set()
        await restart
    else:
        with pytest.raises(asyncio.CancelledError):
            await restart
    await latest
    assert calls == ["stop", command]
    assert coord.get_desired_charging("EV1") is (command == "start")
    assert coord._amp_restart_tasks == {}


@pytest.mark.parametrize("action", ["stop", "cleanup", "prune"])
async def test_amp_restart_rechecks_ownership_after_suppressed_cancellation(
    coordinator_factory, monkeypatch, action
):
    """A dependency suppressing cancellation cannot revive obsolete intent."""
    coord = coordinator_factory(serials=["EV1"])
    coord.data = {"EV1": {"plugged": True, "charging": False}}
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.start_charging = AsyncMock()
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    entered = asyncio.Event()

    async def delay(_seconds):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    monkeypatch.setattr(
        "custom_components.enphase_ev.evse_runtime.asyncio",
        SimpleNamespace(**(vars(asyncio) | {"sleep": delay})),
    )
    coord.schedule_amp_restart("EV1")
    restart = coord._amp_restart_tasks["EV1"]
    await asyncio.wait_for(entered.wait(), 1)
    if action == "cleanup":
        await coord.async_cleanup_runtime_state()
    elif action == "prune":
        coord._devices_inventory_ready = True
        # A retained charger must keep its pending intent until removal.
        coord.evse_runtime.prune_serial_runtime_state(["EV1"])
        assert coord._amp_restart_tasks["EV1"] is restart
        coord.evse_runtime.prune_serial_runtime_state([])
    else:
        await coord.async_stop_charging("EV1")
    await asyncio.wait_for(restart, 1)
    coord.client.start_charging.assert_not_awaited()
    assert coord.get_desired_charging("EV1") is not True
    assert coord.evse_runtime._amp_restart_intents == {}


async def test_explicit_stop_clears_completed_amp_restart(coordinator_factory):
    """A finished task awaiting its cleanup callback is safe to discard."""
    coord = coordinator_factory(serials=["EV1"])
    coord.client.stop_charging = AsyncMock()
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    completed = asyncio.create_task(asyncio.sleep(0))
    await completed
    coord._amp_restart_tasks["EV1"] = completed
    await coord.async_stop_charging("EV1")
    assert coord._amp_restart_tasks == {}


async def test_amp_restart_preserves_command_token_for_same_intent_retry(
    coordinator_factory, monkeypatch
):
    """Internal stops/starts must not invalidate a later amp edit's valid token."""
    coord = coordinator_factory(serials=["EV1"])
    coord.data = {"EV1": {"plugged": True, "charging": False}}
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delay(_seconds):
        entered.set()
        await release.wait()

    monkeypatch.setattr(
        "custom_components.enphase_ev.evse_runtime.asyncio",
        SimpleNamespace(**(vars(asyncio) | {"sleep": delay})),
    )
    token = coord.charging_command_token("EV1")
    coord.schedule_amp_restart("EV1", expected_token=token)
    original = coord._amp_restart_tasks["EV1"]
    await asyncio.wait_for(entered.wait(), 1)
    assert coord.charging_command_token("EV1") is token
    coord.set_last_set_amps("EV1", 24)
    coord.schedule_amp_restart("EV1", expected_token=token)
    replacement = coord._amp_restart_tasks["EV1"]
    with pytest.raises(asyncio.CancelledError):
        await original
    release.set()
    await asyncio.wait_for(replacement, 1)

    assert coord.client.stop_charging.await_count == 2
    coord.client.start_charging.assert_awaited_once()
    assert coord.client.start_charging.await_args.args[:2] == ("EV1", 24)
    assert coord.charging_command_token("EV1") is token


async def test_removed_charger_rejects_deferred_amp_restart_without_affecting_retained(
    coordinator_factory,
):
    """Inventory pruning invalidates deferred writes only for retired chargers."""
    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord._devices_inventory_ready = True
    coord.data = {
        "EV1": {"plugged": True, "charging": False},
        "EV2": {"plugged": True, "charging": False},
    }
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    removed_token = coord.charging_command_token("EV1")
    retained_token = coord.charging_command_token("EV2")

    coord.evse_runtime.prune_serial_runtime_state(["EV2"])
    coord.schedule_amp_restart("EV1", delay=0, expected_token=removed_token)

    assert coord._amp_restart_tasks == {}
    coord.client.stop_charging.assert_not_awaited()
    coord.client.start_charging.assert_not_awaited()
    assert coord.charging_command_token("EV2") is retained_token

    coord.schedule_amp_restart("EV2", delay=0, expected_token=retained_token)
    await coord._amp_restart_tasks["EV2"]

    coord.client.stop_charging.assert_awaited_once_with("EV2")
    coord.client.start_charging.assert_awaited_once()
    assert coord.client.start_charging.await_args.args[0] == "EV2"
    assert coord.charging_command_token("EV2") is retained_token


@pytest.mark.parametrize("older", ["start", "stop"])
@pytest.mark.parametrize("phase", ["cloud", "streaming", "mode"])
async def test_newer_command_waits_for_inflight_io_and_owns_side_effects(
    coordinator_factory, older, phase
):
    """Newer intent sends last and stale completion cannot republish its state."""
    coord = coordinator_factory(serials=["EV1"])
    coord.data = {"EV1": {"plugged": True, "charging": older == "stop"}}
    coord._charge_mode_start_preferences = lambda _sn: ChargeModeStartPreferences(
        enforce_mode="SCHEDULED_CHARGING"
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    cloud_order = []

    async def cloud(command, *_args, **_kwargs):
        if phase == "cloud" and command == older:
            entered.set()
            await release.wait()
        cloud_order.append(command)
        return {"status": "ok"}

    async def followup(*_args, **_kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()

    async def start(*args, **kwargs):
        return await cloud("start", *args, **kwargs)

    async def stop(*args, **kwargs):
        return await cloud("stop", *args, **kwargs)

    coord.client.start_charging = AsyncMock(side_effect=start)
    coord.client.stop_charging = AsyncMock(side_effect=stop)
    coord.async_start_streaming = AsyncMock(
        side_effect=followup if phase == "streaming" else None
    )
    coord._ensure_charge_mode = AsyncMock(
        side_effect=followup if phase == "mode" else None
    )
    coord.async_request_refresh = AsyncMock()
    first = asyncio.create_task(getattr(coord, f"async_{older}_charging")("EV1"))
    await asyncio.wait_for(entered.wait(), 1)
    newer = "stop" if older == "start" else "start"
    latest = asyncio.create_task(getattr(coord, f"async_{newer}_charging")("EV1"))
    await asyncio.sleep(0)
    assert not latest.done()
    release.set()
    assert await first == {"status": "superseded"}
    await latest
    assert cloud_order == [older, newer]
    assert coord.get_desired_charging("EV1") is (newer == "start")
    assert coord.evse_runtime.state._pending_charging["EV1"][0] is (newer == "start")
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.parametrize("automatic", [False, True])
async def test_newer_stop_prevents_start_fallback_retry(coordinator_factory, automatic):
    """A failed old Start must never retry after Stop takes ownership."""
    coord = coordinator_factory(serials=["EV1"])
    coord.data = {"EV1": {"plugged": True, "charging": False}}
    coord.set_desired_charging("EV1", True)
    coord._charge_mode_start_preferences = lambda _sn: ChargeModeStartPreferences(
        include_level=True
    )
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def start(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise _client_response_error(500, message="invalid charge level")

    coord.client.start_charging = AsyncMock(side_effect=start)
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    first = asyncio.create_task(
        coord.evse_runtime.async_auto_resume("EV1")
        if automatic
        else coord.async_start_charging("EV1")
    )
    await asyncio.wait_for(entered.wait(), 1)
    latest = asyncio.create_task(coord.async_stop_charging("EV1"))
    await asyncio.sleep(0)
    coord.client.stop_charging.assert_not_awaited()
    release.set()
    await first
    await latest
    coord.client.start_charging.assert_awaited_once()
    coord.client.stop_charging.assert_awaited_once()
    assert coord.get_desired_charging("EV1") is False
    assert coord.evse_runtime.state._pending_charging["EV1"][0] is False


async def test_amp_restart_starts_despite_stale_charging_telemetry(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord.data = {"EV1": {"plugged": True, "charging": True}}
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.schedule_amp_restart("EV1", delay=0)
    await coord._amp_restart_tasks["EV1"]
    coord.client.stop_charging.assert_awaited_once_with("EV1")
    coord.client.start_charging.assert_awaited_once()


@pytest.mark.parametrize("queued", ["start", "stop", "auto_resume"])
async def test_newer_intent_supersedes_queued_command(coordinator_factory, queued):
    """A queued operation rechecks intent only after acquiring its own lock."""
    coord = coordinator_factory(serials=["EV1"])
    coord.data = {"EV1": {"plugged": True, "charging": False}}
    coord.set_desired_charging("EV1", True)
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    lock = coord.evse_runtime._charging_command_lock("EV1")
    await lock.acquire()
    first = asyncio.create_task(
        coord.evse_runtime.async_auto_resume("EV1")
        if queued == "auto_resume"
        else getattr(coord, f"async_{queued}_charging")("EV1")
    )
    await asyncio.sleep(0)
    latest = asyncio.create_task(coord.async_stop_charging("EV1"))
    await asyncio.sleep(0)
    lock.release()
    await first
    await latest
    coord.client.start_charging.assert_not_awaited()
    coord.client.stop_charging.assert_awaited_once()
    assert coord.get_desired_charging("EV1") is False
    # Even without a changed token, the stopped intent must prevent auto-resume.
    await coord.evse_runtime.async_auto_resume("EV1")
    coord.client.start_charging.assert_not_awaited()


async def test_cancelled_waiter_keeps_other_command_lock_and_other_chargers_free(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord.data = {sn: {"plugged": True, "charging": False} for sn in ["EV1", "EV2"]}
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def start(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"status": "ok"}

    coord.client.start_charging = AsyncMock(side_effect=start)
    owner = asyncio.create_task(coord.async_start_charging("EV1"))
    await asyncio.wait_for(entered.wait(), 1)
    waiting = asyncio.create_task(coord.async_stop_charging("EV1"))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert coord.evse_runtime._charging_command_lock("EV1").locked()
    await asyncio.wait_for(coord.async_stop_charging("EV2"), 1)
    coord.client.stop_charging.assert_awaited_once_with("EV2")
    release.set()
    await owner
    assert not coord.evse_runtime._charging_command_lock("EV1").locked()


async def test_auto_resume_mode_completion_cannot_override_newer_stop(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1"])
    coord.data = {"EV1": {"plugged": True, "charging": False}}
    coord.set_desired_charging("EV1", True)
    coord._charge_mode_start_preferences = lambda _sn: ChargeModeStartPreferences(
        enforce_mode="SCHEDULED_CHARGING"
    )
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def mode(*_args, **_kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()

    coord._ensure_charge_mode = AsyncMock(side_effect=mode)
    first = asyncio.create_task(coord.evse_runtime.async_auto_resume("EV1"))
    await asyncio.wait_for(entered.wait(), 1)
    latest = asyncio.create_task(coord.async_stop_charging("EV1"))
    await asyncio.sleep(0)
    release.set()
    await first
    await latest
    assert coord.get_desired_charging("EV1") is False
    coord.async_request_refresh.assert_awaited_once()


async def test_start_after_completed_stop_ignores_stale_charging_telemetry(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1"])
    coord.data = {"EV1": {"plugged": True, "charging": True}}
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    await coord.async_stop_charging("EV1")
    await coord.async_start_charging("EV1")
    coord.client.stop_charging.assert_awaited_once_with("EV1")
    coord.client.start_charging.assert_awaited_once()
    assert coord.get_desired_charging("EV1") is True


async def test_pending_charge_mode_refresh_reads_scheduler_despite_embedded_mode(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1"])
    coord._has_successful_refresh = True
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "EV1",
                    "chargeMode": "MANUAL_CHARGING",
                    "connectors": [{}],
                    "session_d": {},
                }
            ]
        }
    )
    coord.client.charge_mode = AsyncMock(return_value="SCHEDULED_CHARGING")
    coord.client.set_charge_mode = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    runtime = coord.evse_runtime
    runtime.set_charge_mode_cache("EV1", "MANUAL_CHARGING")
    await runtime.async_set_charge_mode("EV1", "SCHEDULED_CHARGING")
    result = await coord._async_update_data()
    coord.client.charge_mode.assert_awaited_once_with("EV1")
    assert result["EV1"]["charge_mode_pref"] == "SCHEDULED_CHARGING"
    assert not runtime.snapshot.pending_charge_modes
