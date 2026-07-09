"""Focused coverage tests for EnphaseCoordinator edge branches."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp.client_reqrep import RequestInfo
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

import builtins
from homeassistant.exceptions import ConfigEntryAuthFailed
from http import HTTPStatus
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev import auth_refresh_runtime as arr_mod
from custom_components.enphase_ev import coordinator as coord_mod
from custom_components.enphase_ev import coordinator_diagnostics as coord_diag_mod
from custom_components.enphase_ev.coordinator import (
    EnphaseCoordinator,
    ChargeModeStartPreferences,
)
from custom_components.enphase_ev.api import (
    AuthSettingsUnavailable,
    ChargerConfigUnavailable,
    SchedulerUnavailable,
    Unauthorized,
)
from custom_components.enphase_ev.const import (
    AUTH_APP_SETTING,
    AUTH_RFID_SETTING,
    DEFAULT_CHARGE_LEVEL_SETTING,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_SLOW_POLL_INTERVAL,
    GREEN_BATTERY_SETTING,
    ISSUE_AUTH_BLOCKED,
    ISSUE_CLOUD_ERRORS,
    ISSUE_DNS_RESOLUTION,
    ISSUE_REAUTH_REQUIRED,
    ISSUE_NETWORK_UNREACHABLE,
    ISSUE_AUTH_SETTINGS_UNAVAILABLE,
    ISSUE_HEMS_AUTH_DEGRADED,
    ISSUE_SCHEDULER_UNAVAILABLE,
    ISSUE_SESSION_HISTORY_UNAVAILABLE,
    ISSUE_SITE_ENERGY_UNAVAILABLE,
    ISSUE_TOO_MANY_ACTIVE_SESSIONS,
    MIN_FAST_POLL_INTERVAL,
    MIN_SLOW_POLL_INTERVAL,
    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    PHASE_SWITCH_CONFIG_SETTING,
)
from custom_components.enphase_ev.evse_runtime import (
    AUTH_SETTINGS_CACHE_TTL,
    CHARGER_CONFIG_CACHE_TTL,
    CHARGE_MODE_CACHE_TTL,
    ChargeModeResolution,
    FAST_TOGGLE_POLL_HOLD_S,
    GREEN_BATTERY_CACHE_TTL,
    STREAMING_DEFAULT_DURATION_S,
    EvseRuntime,
)
from custom_components.enphase_ev.session_history import MIN_SESSION_HISTORY_CACHE_TTL
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL as SERIAL_ONE

pytestmark = pytest.mark.session_history_real

pytest.importorskip("homeassistant")


def _request_info() -> RequestInfo:
    """Build a minimal RequestInfo for ClientResponseError."""
    return RequestInfo(
        url=URL("https://enphase.example/status"),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL("https://enphase.example/status"),
    )


def _attach_evse_runtime(coord: EnphaseCoordinator) -> EnphaseCoordinator:
    coord.evse_runtime = EvseRuntime(coord)
    return coord


@pytest.mark.asyncio
async def test_async_update_data_http_error_description(
    coordinator_factory, mock_issue_registry, monkeypatch
):
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=502,
        message='{"error":{"description":"bad"}}',
        headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00"},
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._http_errors = 1
    coord._schedule_backoff_timer = MagicMock()
    monkeypatch.setattr(
        coord_mod.dt_util, "utcnow", lambda: datetime(2025, 1, 1, tzinfo=timezone.utc)
    )

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_description == "bad"
    assert any(issue[1] == ISSUE_CLOUD_ERRORS for issue in mock_issue_registry.created)


@pytest.mark.asyncio
async def test_async_update_data_http_error_trimmed_json(
    coordinator_factory, mock_issue_registry
):
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=500,
        message='"{"error":{"displayMessage":"trimmed"}}"',
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_description == "trimmed"


@pytest.mark.asyncio
async def test_async_update_data_scheduler_unavailable_returns_cached(
    coordinator_factory, mock_issue_registry
):
    coord = coordinator_factory()
    coord.data = {SERIAL_ONE: {"sn": SERIAL_ONE}}
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=503,
        message='{"error":{"displayMessage":"Service Unavailable from POST http://iqevc-scheduler.prodinternal.com/api/v1/iqevc/schedules/status"}}',
        headers=CIMultiDictProxy(CIMultiDict()),
    )
    coord.client.status = AsyncMock(side_effect=err)

    result = await coord._async_update_data()

    assert result == coord.data
    assert coord.scheduler_available is False
    assert coord.last_failure_source == "scheduler"
    assert any(
        issue[1] == ISSUE_SCHEDULER_UNAVAILABLE for issue in mock_issue_registry.created
    )


def test_collect_site_metrics_handles_site_energy_cache_age_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class BadEnergy(SimpleNamespace):
        def _site_energy_cache_age(self):
            raise RuntimeError("boom")

    coord.energy = BadEnergy(
        site_energy={"grid_import": {}},
        _site_energy_meta={"start_date": "2024-01-01"},
    )
    metrics = coord.collect_site_metrics()

    assert metrics["site_energy"]["cache_age_s"] is None


def test_coordinator_public_diagnostics_helpers(coordinator_factory) -> None:
    coord = coordinator_factory()
    backoff_end = datetime(2025, 1, 2, tzinfo=timezone.utc)
    coord._charge_mode_cache = {  # noqa: SLF001
        SERIAL_ONE: ("FAST", time.monotonic()),
        "EMPTY": (None, time.monotonic()),
    }
    coord.session_history = None
    coord._scheduler_backoff_ends_utc = backoff_end  # noqa: SLF001
    coord._scheduler_backoff_until = time.monotonic() + 60  # noqa: SLF001
    coord._scheduler_failures = 3  # noqa: SLF001
    coord._scheduler_available = True  # noqa: SLF001
    coord._scheduler_last_error = None  # noqa: SLF001
    coord._battery_profile_payload = {"profile": "cost_savings"}  # noqa: SLF001
    coord._battery_pending_profile = "backup_only"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._battery_backend_profile_update_pending = False  # noqa: SLF001
    coord._battery_backend_not_pending_observed_at = datetime(
        2025, 1, 3, tzinfo=timezone.utc
    )  # noqa: SLF001
    coord._evse_site_feature_flags = {"evse_charging_mode": True}  # noqa: SLF001
    coord._evse_feature_flags_by_serial = {  # noqa: SLF001
        SERIAL_ONE: {"max_current_config_support": True}
    }
    coord._evse_feature_flags_payload = {  # noqa: SLF001
        "meta": {"serverTimeStamp": "2026-03-08T09:40:02.917+00:00"},
        "error": {},
    }
    coord.evse_timeseries = SimpleNamespace(
        diagnostics=lambda: {
            "cache_ttl_seconds": 900.0,
            "daily_cache_days": ["2026-03-11"],
            "daily_cache_age_seconds": {"2026-03-11": 5.0},
            "lifetime_cache_age_seconds": 10.0,
            "lifetime_serial_count": 1,
        }
    )
    coord.data[SERIAL_ONE]["charge_mode_supported_source"] = "feature_flag"
    coord.data["BROKEN"] = []
    coord._inverter_summary_counts = {"total": 1}  # noqa: SLF001
    coord._inverter_panel_info = {"pv_module_manufacturer": "Acme"}  # noqa: SLF001
    coord._inverter_status_type_counts = {"IQ7A": 1}  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "count": 1,
            "devices": [{"serial_number": "INV-A"}],
        }
    }

    assert coord.charge_mode_cache_snapshot() == {SERIAL_ONE: "FAST"}
    assert coord.session_history_diagnostics() == {
        "cache_ttl_seconds": None,
        "cache_keys": 0,
        "interval_minutes": None,
        "in_progress": 0,
    }
    coord.session_history = SimpleNamespace(
        cache_ttl=300,
        cache_key_count=2,
        in_progress=1,
    )
    coord._session_history_interval_min = 15  # noqa: SLF001
    assert coord.session_history_diagnostics() == {
        "available": None,
        "using_stale": None,
        "failures": None,
        "last_error": None,
        "last_failure_utc": None,
        "last_payload_signature": None,
        "cache_ttl_seconds": 300,
        "cache_keys": 2,
        "cache_state_counts": None,
        "interval_minutes": 15,
        "in_progress": 1,
    }
    assert coord.evse_timeseries_diagnostics() == {
        "cache_ttl_seconds": 900.0,
        "daily_cache_days": ["2026-03-11"],
        "daily_cache_age_seconds": {"2026-03-11": 5.0},
        "lifetime_cache_age_seconds": 10.0,
        "lifetime_serial_count": 1,
    }
    assert coord.scheduler_backoff_active() is True
    assert coord.scheduler_diagnostics()["backoff_ends_utc"] == backoff_end.isoformat()
    metrics = coord.collect_site_metrics()
    assert metrics["battery_backend_profile_update_pending"] is False
    assert (
        metrics["battery_backend_not_pending_observed_at"]
        == "2025-01-03T00:00:00+00:00"
    )
    assert coord.battery_diagnostics_payloads()["profile_payload"] == {
        "profile": "cost_savings"
    }
    assert coord.battery_diagnostics_payloads()["hems_devices_payload"] is None
    assert coord.evse_diagnostics_payloads()["site_feature_flags"] == {
        "evse_charging_mode": True
    }
    assert coord.evse_diagnostics_payloads()["charger_feature_flags"] == [
        {
            "serial": SERIAL_ONE,
            "flags": {"max_current_config_support": True},
        }
    ]
    assert coord.evse_diagnostics_payloads()["charger_support_sources"] == [
        {
            "serial": SERIAL_ONE,
            "sources": {"charge_mode_supported": "feature_flag"},
        }
    ]
    assert coord.evse_diagnostics_payloads()["timeseries"] == {
        "cache_ttl_seconds": 900.0,
        "daily_cache_days": ["2026-03-11"],
        "daily_cache_age_seconds": {"2026-03-11": 5.0},
        "lifetime_cache_age_seconds": 10.0,
        "lifetime_serial_count": 1,
    }
    assert coord.inverter_diagnostics_payloads()["summary_counts"] == {"total": 1}
    assert coord.inverter_diagnostics_payloads()["panel_info"] == {
        "pv_module_manufacturer": "Acme"
    }
    assert coord.inverter_diagnostics_payloads()["status_type_counts"] == {"IQ7A": 1}
    assert coord.inverter_diagnostics_payloads()["bucket_snapshot"]["count"] == 1
    coord._system_dashboard_devices_tree_payload = {  # noqa: SLF001
        "devices": [{"device_uid": "GW-1"}]
    }
    coord._system_dashboard_devices_details_payloads = {  # noqa: SLF001
        "envoy": {"envoy": {"modem": {"rssi": -70}}}
    }
    coord._system_dashboard_hierarchy_summary = {  # noqa: SLF001
        "total_nodes": 1,
        "counts_by_type": {"envoy": 1},
    }
    coord._system_dashboard_type_summaries = {  # noqa: SLF001
        "envoy": {"modem": {"rssi": -70}}
    }
    system_dashboard = coord.system_dashboard_diagnostics()
    assert (
        system_dashboard["devices_tree_payload"]["devices"][0]["device_uid"] == "GW-1"
    )
    assert system_dashboard["devices_details_payloads"]["envoy"]["envoy"]["modem"] == {
        "rssi": -70
    }
    assert system_dashboard["hierarchy_summary"]["counts_by_type"]["envoy"] == 1
    assert system_dashboard["type_summaries"]["envoy"]["modem"]["rssi"] == -70


def test_payload_health_helpers_cover_edge_paths(coordinator_factory) -> None:
    coord = coordinator_factory()

    assert coord._payload_endpoint_reusable("status", 10) is False  # noqa: SLF001

    coord._payload_health["status"]["last_success_mono"] = object()  # noqa: SLF001
    assert coord._payload_endpoint_reusable("status", 10) is False  # noqa: SLF001

    class BadFloat(float):
        def __new__(cls):
            return float.__new__(cls, 1.0)

        def __float__(self) -> float:
            raise ValueError("boom")

    coord._payload_health["status"]["last_success_mono"] = BadFloat()  # noqa: SLF001
    assert coord._payload_endpoint_reusable("status", 10) is False  # noqa: SLF001

    coord._payload_health["status"]["last_success_mono"] = (
        time.monotonic() + 5
    )  # noqa: SLF001
    assert coord._payload_endpoint_reusable("status", 10) is True  # noqa: SLF001

    coord._payload_health = "bad"  # type: ignore[assignment]  # noqa: SLF001
    diagnostics = coord.payload_health_diagnostics()
    assert "summary_v2" in diagnostics
    assert "session_history" in diagnostics
    assert "evse_timeseries" in diagnostics

    coord = coordinator_factory()
    coord._payload_health["status"] = {  # noqa: SLF001
        "available": False,
        "using_stale": True,
        "failures": 1,
        "last_error": "bad payload",
        "last_success_utc": None,
        "last_success_mono": "bad",
        "last_failure_utc": None,
        "last_payload_signature": {"endpoint": "/service/status"},
    }

    coord._payload_health["other"] = {  # noqa: SLF001
        "available": True,
        "using_stale": False,
        "failures": 0,
        "last_error": None,
        "last_success_utc": None,
        "last_success_mono": BadFloat(),
        "last_failure_utc": None,
        "last_payload_signature": None,
    }

    diagnostics = coord.payload_health_diagnostics()
    assert diagnostics["status"]["last_success_age_s"] is None
    assert diagnostics["other"]["last_success_age_s"] is None

    coord._payload_health["status"]["last_success_mono"] = (
        time.monotonic() - 1
    )  # noqa: SLF001
    diagnostics = coord.payload_health_diagnostics()
    assert diagnostics["status"]["last_success_age_s"] is not None

    coord.evse_timeseries = SimpleNamespace(
        diagnostics=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    diagnostics = coord.payload_health_diagnostics()
    assert "evse_timeseries" not in diagnostics


def test_evse_timeseries_diagnostics_handles_missing_manager(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.evse_timeseries = None

    assert coord.evse_timeseries_diagnostics() == {
        "cache_ttl_seconds": None,
        "daily_cache_days": [],
        "daily_cache_age_seconds": {},
        "lifetime_cache_age_seconds": None,
        "lifetime_serial_count": 0,
    }


@pytest.mark.asyncio
async def test_async_update_data_handles_system_dashboard_refresh_error(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.client.status = AsyncMock(
        return_value={"evChargerData": [], "ts": 1_700_000_000}
    )
    refresh_dashboard = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        coord,
        "_async_refresh_system_dashboard",
        refresh_dashboard,
    )

    result = await coord._async_update_data()

    assert result == {}
    refresh_dashboard.assert_not_awaited()
    assert "system_dashboard_s" not in coord.phase_timings


def test_evse_feature_flag_helpers_cover_edge_cases(coordinator_factory) -> None:
    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord = coordinator_factory()

    assert coord.evse_feature_flag("", SERIAL_ONE) is None
    assert coord._coerce_evse_feature_flags_map([]) == {}  # noqa: SLF001
    assert coord._coerce_evse_feature_flags_map(
        {BadStr(): True, " ": True, "ok": 1}
    ) == {  # noqa: SLF001
        "ok": 1
    }

    coord._parse_evse_feature_flags_payload([])  # noqa: SLF001
    assert coord._evse_site_feature_flags == {}  # noqa: SLF001
    assert coord._evse_feature_flags_by_serial == {}  # noqa: SLF001

    coord._parse_evse_feature_flags_payload({"data": []})  # noqa: SLF001
    assert coord._evse_site_feature_flags == {}  # noqa: SLF001
    assert coord._evse_feature_flags_by_serial == {}  # noqa: SLF001

    coord._parse_evse_feature_flags_payload(  # noqa: SLF001
        {
            "data": {
                BadStr(): True,
                " ": False,
                "site_flag": True,
                SERIAL_ONE: {BadStr(): False, " ": True, "rfid": True},
            }
        }
    )
    assert coord._evse_site_feature_flags == {"site_flag": True}  # noqa: SLF001
    assert coord._evse_feature_flags_by_serial == {  # noqa: SLF001
        SERIAL_ONE: {"rfid": True}
    }


@pytest.mark.asyncio
async def test_async_refresh_evse_feature_flags_edge_cases(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._evse_feature_flags_cache_until = (
        coord_mod.time.monotonic() + 60
    )  # noqa: SLF001
    coord.client.evse_feature_flags = AsyncMock()

    await coord._async_refresh_evse_feature_flags()  # noqa: SLF001
    coord.client.evse_feature_flags.assert_not_awaited()

    coord._evse_feature_flags_cache_until = None  # noqa: SLF001
    coord.client.evse_feature_flags = None
    await coord._async_refresh_evse_feature_flags(force=True)  # noqa: SLF001
    assert coord._evse_feature_flags_payload is None  # noqa: SLF001

    coord.client.evse_feature_flags = AsyncMock(return_value=[])
    await coord._async_refresh_evse_feature_flags(force=True)  # noqa: SLF001
    assert coord._evse_feature_flags_payload is None  # noqa: SLF001
    assert coord._evse_site_feature_flags == {}  # noqa: SLF001
    assert coord._evse_feature_flags_by_serial == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_update_data_handles_bad_data_mapping(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()

    class BadDictType(type):
        def __instancecheck__(self, instance):  # noqa: ANN001
            return True

        def __call__(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("boom")

    class BadDict(metaclass=BadDictType):
        pass

    monkeypatch.setattr(coord_mod, "dict", BadDict, raising=False)
    coord.data = {SERIAL_ONE: {"sn": SERIAL_ONE}}
    coord.site_only = True
    coord.energy._async_refresh_site_energy = AsyncMock()  # noqa: SLF001

    result = await coord._async_update_data()

    assert result == {}


@pytest.mark.asyncio
async def test_async_update_data_handles_bad_request_info_url(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()

    class BadRequestInfo:
        @property
        def real_url(self):
            raise RuntimeError("boom")

    err = aiohttp.ClientResponseError(
        BadRequestInfo(),
        (),
        status=500,
        message="Server error",
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_data_charge_mode_probe_handles_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_until = None  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": SERIAL_ONE,
                    "chargeMode": "MANUAL_CHARGING",
                    "connectors": [{}],
                    "session_d": {},
                }
            ]
        }
    )
    coord._get_charge_mode = AsyncMock(side_effect=RuntimeError("boom"))  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock()  # noqa: SLF001

    await coord._async_update_data()

    coord._get_charge_mode.assert_awaited_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_resolve_charge_modes_backoff_uses_cache(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    now = time.monotonic()
    coord._scheduler_backoff_until = now + 60  # noqa: SLF001
    coord._charge_mode_cache[SERIAL_ONE] = ("MANUAL_CHARGING", now)

    result = await coord.evse_runtime.async_resolve_charge_modes(["", SERIAL_ONE])

    assert result == {
        SERIAL_ONE: ChargeModeResolution("MANUAL_CHARGING", "cache_backoff")
    }


def test_scheduler_note_default_reason_and_backoff_error(
    coordinator_factory, mock_issue_registry, monkeypatch
) -> None:
    coord = coordinator_factory()

    calls = {"count": 0}

    def fake_utcnow():
        calls["count"] += 1
        if calls["count"] == 1:
            return datetime(2025, 1, 1, tzinfo=timezone.utc)
        raise RuntimeError("boom")

    monkeypatch.setattr(coord_mod.dt_util, "utcnow", fake_utcnow)

    coord._note_scheduler_unavailable(None)  # noqa: SLF001

    assert coord.scheduler_last_error == "Scheduler unavailable"
    assert coord._scheduler_backoff_ends_utc is None  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_SCHEDULER_UNAVAILABLE for issue in mock_issue_registry.created
    )

    coord._mark_scheduler_available()  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_SCHEDULER_UNAVAILABLE for issue in mock_issue_registry.deleted
    )


def test_auth_settings_note_default_reason_and_backoff_error(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()

    calls = {"count": 0}

    def fake_utcnow():
        calls["count"] += 1
        if calls["count"] == 1:
            return datetime(2025, 1, 1, tzinfo=timezone.utc)
        raise RuntimeError("boom")

    monkeypatch.setattr(coord_mod.dt_util, "utcnow", fake_utcnow)

    coord._note_auth_settings_unavailable(None)  # noqa: SLF001

    assert coord.auth_settings_last_error == "Auth settings unavailable"
    assert coord._auth_settings_backoff_ends_utc is None  # noqa: SLF001


def test_sync_issue_helpers_return_when_missing_manager(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.session_history = None
    coord._sync_session_history_issue()  # noqa: SLF001

    coord.energy = None
    coord._sync_site_energy_issue()  # noqa: SLF001


@pytest.mark.asyncio
async def test_get_charge_mode_handles_scheduler_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.charge_mode = AsyncMock(side_effect=SchedulerUnavailable("down"))

    result = await coord._get_charge_mode(SERIAL_ONE)  # noqa: SLF001

    assert result is None


@pytest.mark.asyncio
async def test_get_green_battery_setting_handles_scheduler_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.green_charging_settings = AsyncMock(
        side_effect=SchedulerUnavailable("down")
    )

    result = await coord._get_green_battery_setting(SERIAL_ONE)  # noqa: SLF001

    assert result is None


@pytest.mark.asyncio
async def test_get_auth_settings_handles_backoff_and_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    now = time.monotonic()
    coord._auth_settings_backoff_until = now + 60  # noqa: SLF001
    coord._auth_settings_cache[SERIAL_ONE] = (
        True,
        False,
        True,
        True,
        now - (AUTH_SETTINGS_CACHE_TTL + 1),
    )

    cached = await coord._get_auth_settings(SERIAL_ONE)  # noqa: SLF001
    assert cached == (True, False, True, True)

    coord._auth_settings_cache.clear()
    empty = await coord._get_auth_settings(SERIAL_ONE)  # noqa: SLF001
    assert empty is None

    coord._auth_settings_backoff_until = None  # noqa: SLF001
    coord.client.charger_auth_settings = AsyncMock(
        side_effect=AuthSettingsUnavailable("down")
    )
    unavailable = await coord._get_auth_settings(SERIAL_ONE)  # noqa: SLF001
    assert unavailable is None


@pytest.mark.asyncio
async def test_async_resolve_green_battery_settings_backoff_uses_cache(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    now = time.monotonic()
    coord._scheduler_backoff_until = now + 60  # noqa: SLF001
    coord._green_battery_cache[SERIAL_ONE] = (True, True, now)

    result = await coord._async_resolve_green_battery_settings(
        ["", SERIAL_ONE]
    )  # noqa: SLF001

    assert result == {SERIAL_ONE: (True, True)}


@pytest.mark.asyncio
async def test_async_resolve_auth_settings_backoff_uses_cache(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    now = time.monotonic()
    coord._auth_settings_backoff_until = now + 60  # noqa: SLF001
    coord._auth_settings_cache[SERIAL_ONE] = (True, False, True, True, now)

    result = await coord._async_resolve_auth_settings(["", SERIAL_ONE])  # noqa: SLF001

    assert result == {SERIAL_ONE: (True, False, True, True)}


def test_auth_settings_issue_tracking(coordinator_factory, mock_issue_registry) -> None:
    coord = coordinator_factory()

    coord._note_auth_settings_unavailable("auth down")  # noqa: SLF001

    assert coord.auth_settings_available is False
    assert coord._auth_settings_issue_reported is True  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_AUTH_SETTINGS_UNAVAILABLE
        for issue in mock_issue_registry.created
    )

    coord._mark_auth_settings_available()  # noqa: SLF001
    assert coord.auth_settings_available is True
    assert any(
        issue[1] == ISSUE_AUTH_SETTINGS_UNAVAILABLE
        for issue in mock_issue_registry.deleted
    )


def test_degraded_service_repair_issues_can_be_disabled(
    hass,
    config_entry,
    mock_issue_registry,
) -> None:
    config_entry.__dict__["options"] = MappingProxyType(
        {OPT_DEGRADED_SERVICE_REPAIR_ISSUES: False}
    )
    coord = EnphaseCoordinator(hass, config_entry.data, config_entry)

    degraded_issue_ids = {
        ISSUE_SCHEDULER_UNAVAILABLE,
        ISSUE_SESSION_HISTORY_UNAVAILABLE,
        ISSUE_SITE_ENERGY_UNAVAILABLE,
        ISSUE_AUTH_SETTINGS_UNAVAILABLE,
        ISSUE_HEMS_AUTH_DEGRADED,
    }
    assert degraded_issue_ids <= {
        issue_id for _domain, issue_id in mock_issue_registry.deleted
    }
    mock_issue_registry.created.clear()
    mock_issue_registry.deleted.clear()

    coord._note_scheduler_unavailable("scheduler down")  # noqa: SLF001
    coord.session_history = SimpleNamespace(service_available=False)
    coord._sync_session_history_issue()  # noqa: SLF001
    coord.energy = SimpleNamespace(service_available=False)
    coord._sync_site_energy_issue()  # noqa: SLF001
    coord._note_auth_settings_unavailable("auth settings down")  # noqa: SLF001
    coord._note_hems_auth_failure(  # noqa: SLF001
        Unauthorized(),
        endpoint="hems_devices",
    )

    assert mock_issue_registry.created == []
    assert degraded_issue_ids <= {
        issue_id for _domain, issue_id in mock_issue_registry.deleted
    }
    assert coord.scheduler_available is False
    assert coord._session_history_issue_reported is False  # noqa: SLF001
    assert coord._site_energy_issue_reported is False  # noqa: SLF001
    assert coord._auth_settings_issue_reported is False  # noqa: SLF001
    assert coord._hems_auth_issue_reported is False  # noqa: SLF001


def test_degraded_service_repair_issue_toggle_is_entry_scoped(
    hass,
    config_entry,
    mock_issue_registry,
) -> None:
    config_entry.__dict__["options"] = MappingProxyType(
        {OPT_DEGRADED_SERVICE_REPAIR_ISSUES: True}
    )
    enabled_coord = EnphaseCoordinator(hass, config_entry.data, config_entry)
    enabled_coord._note_scheduler_unavailable("scheduler down")  # noqa: SLF001
    enabled_issue_id = enabled_coord.diagnostics._repair_issue_id(  # noqa: SLF001
        ISSUE_SCHEDULER_UNAVAILABLE
    )
    assert any(
        issue[1] == enabled_issue_id
        and issue[2]["translation_key"] == ISSUE_SCHEDULER_UNAVAILABLE
        for issue in mock_issue_registry.created
    )

    disabled_entry = MockConfigEntry(
        domain=coord_mod.DOMAIN,
        entry_id="disabled-entry",
        data={
            **config_entry.data,
            coord_mod.CONF_SITE_ID: "9633675",
        },
        options={OPT_DEGRADED_SERVICE_REPAIR_ISSUES: False},
        title="Disabled repairs",
        unique_id="9633675",
    )
    disabled_entry.add_to_hass(hass)
    disabled_coord = EnphaseCoordinator(hass, disabled_entry.data, disabled_entry)
    disabled_coord._note_scheduler_unavailable("scheduler down")  # noqa: SLF001
    disabled_issue_id = disabled_coord.diagnostics._repair_issue_id(  # noqa: SLF001
        ISSUE_SCHEDULER_UNAVAILABLE
    )

    assert (coord_mod.DOMAIN, disabled_issue_id) in mock_issue_registry.deleted
    assert (coord_mod.DOMAIN, enabled_issue_id) not in mock_issue_registry.deleted
    assert not any(
        issue[1] == disabled_issue_id for issue in mock_issue_registry.created
    )


def test_degraded_service_repair_issue_create_clears_legacy_static_issue(
    hass,
    config_entry,
    mock_issue_registry,
) -> None:
    config_entry.__dict__["options"] = MappingProxyType(
        {OPT_DEGRADED_SERVICE_REPAIR_ISSUES: True}
    )
    coord = EnphaseCoordinator(hass, config_entry.data, config_entry)
    mock_issue_registry.deleted.clear()

    coord._note_scheduler_unavailable("scheduler down")  # noqa: SLF001
    scoped_issue_id = coord.diagnostics._repair_issue_id(  # noqa: SLF001
        ISSUE_SCHEDULER_UNAVAILABLE
    )

    assert scoped_issue_id != ISSUE_SCHEDULER_UNAVAILABLE
    assert any(issue[1] == scoped_issue_id for issue in mock_issue_registry.created)
    assert (
        coord_mod.DOMAIN,
        ISSUE_SCHEDULER_UNAVAILABLE,
    ) in mock_issue_registry.deleted


def test_degraded_service_repair_issue_init_clears_stale_legacy_static_issues(
    hass,
    config_entry,
    mock_issue_registry,
) -> None:
    config_entry.__dict__["options"] = MappingProxyType(
        {OPT_DEGRADED_SERVICE_REPAIR_ISSUES: True}
    )

    EnphaseCoordinator(hass, config_entry.data, config_entry)

    assert mock_issue_registry.created == []
    assert {
        ISSUE_SCHEDULER_UNAVAILABLE,
        ISSUE_SESSION_HISTORY_UNAVAILABLE,
        ISSUE_SITE_ENERGY_UNAVAILABLE,
        ISSUE_AUTH_SETTINGS_UNAVAILABLE,
        ISSUE_HEMS_AUTH_DEGRADED,
    } <= {issue_id for _domain, issue_id in mock_issue_registry.deleted}


def test_sync_session_history_issue_creates_and_clears(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    coord.session_history = SimpleNamespace(service_available=False)

    coord._sync_session_history_issue()  # noqa: SLF001

    assert coord._session_history_issue_reported is True  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_SESSION_HISTORY_UNAVAILABLE
        for issue in mock_issue_registry.created
    )

    coord.session_history.service_available = True
    coord._sync_session_history_issue()  # noqa: SLF001

    assert coord._session_history_issue_reported is False  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_SESSION_HISTORY_UNAVAILABLE
        for issue in mock_issue_registry.deleted
    )


def test_sync_session_history_issue_uses_current_day_unavailable_view(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    coord.data = {SERIAL_ONE: {"display_name": "Garage EV"}}
    coord._session_history_day = lambda payload, default: default  # type: ignore[method-assign]  # noqa: SLF001
    coord.session_history = SimpleNamespace(
        service_available=True,
        get_cache_view=lambda *_args, **_kwargs: SimpleNamespace(
            has_valid_cache=False,
            state="unavailable",
            last_error="503 Service Unavailable",
            cache_age=1.0,
        ),
    )

    coord._sync_session_history_issue()  # noqa: SLF001

    assert coord._session_history_issue_reported is True  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_SESSION_HISTORY_UNAVAILABLE
        for issue in mock_issue_registry.created
    )

    coord.session_history.get_cache_view = lambda *_args, **_kwargs: SimpleNamespace(
        has_valid_cache=True,
        state="valid",
        last_error=None,
        cache_age=1.0,
    )
    coord._sync_session_history_issue()  # noqa: SLF001

    assert coord._session_history_issue_reported is False  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_SESSION_HISTORY_UNAVAILABLE
        for issue in mock_issue_registry.deleted
    )


def test_sync_session_history_issue_handles_fallback_day_resolution(
    coordinator_factory, mock_issue_registry, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.data = {
        "bad": "skip-me",
        SERIAL_ONE: {"display_name": "Garage EV"},
    }
    coord._session_history_day = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[assignment]  # noqa: SLF001
    coord.session_history = SimpleNamespace(
        service_available=True,
        get_cache_view=lambda serial, _day_key: SimpleNamespace(
            has_valid_cache=serial == "bad",
            state="unavailable",
            last_error=None if serial == "bad" else "fetch failed",
            cache_age=1.0,
        ),
    )
    monkeypatch.setattr(
        coord_diag_mod.dt_util,
        "now",
        lambda: datetime(2025, 10, 16, 12, 0, 0),
    )
    monkeypatch.setattr(
        coord_diag_mod.dt_util,
        "as_local",
        lambda _value: (_ for _ in ()).throw(ValueError("boom")),
    )

    coord._sync_session_history_issue()  # noqa: SLF001

    assert coord._session_history_issue_reported is True  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_SESSION_HISTORY_UNAVAILABLE
        for issue in mock_issue_registry.created
    )


def test_sync_session_history_issue_ignores_startup_missing_cache(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    coord.data = {SERIAL_ONE: {"display_name": "Garage EV"}}
    coord._session_history_day = lambda payload, default: default  # type: ignore[method-assign]  # noqa: SLF001
    coord.session_history = SimpleNamespace(
        service_available=True,
        get_cache_view=lambda *_args, **_kwargs: SimpleNamespace(
            has_valid_cache=False,
            state="unavailable",
            last_error=None,
            cache_age=None,
        ),
    )

    coord._sync_session_history_issue()  # noqa: SLF001

    assert coord._session_history_issue_reported is False  # noqa: SLF001
    assert not any(
        issue[1] == ISSUE_SESSION_HISTORY_UNAVAILABLE
        for issue in mock_issue_registry.created
    )


def test_sync_session_history_issue_reports_materialized_unavailable_cache(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    coord.data = {SERIAL_ONE: {"display_name": "Garage EV"}}
    coord._session_history_day = lambda payload, default: default  # type: ignore[method-assign]  # noqa: SLF001
    coord.session_history = SimpleNamespace(
        service_available=True,
        get_cache_view=lambda *_args, **_kwargs: SimpleNamespace(
            has_valid_cache=False,
            state="unavailable",
            last_error=None,
            cache_age=1.0,
        ),
    )

    coord._sync_session_history_issue()  # noqa: SLF001

    assert coord._session_history_issue_reported is True  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_SESSION_HISTORY_UNAVAILABLE
        for issue in mock_issue_registry.created
    )


def test_sync_site_energy_issue_creates_and_clears(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    coord.energy = SimpleNamespace(service_available=False)

    coord._sync_site_energy_issue()  # noqa: SLF001

    assert coord._site_energy_issue_reported is True  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_SITE_ENERGY_UNAVAILABLE
        for issue in mock_issue_registry.created
    )

    coord.energy.service_available = True
    coord._sync_site_energy_issue()  # noqa: SLF001

    assert coord._site_energy_issue_reported is False  # noqa: SLF001
    assert any(
        issue[1] == ISSUE_SITE_ENERGY_UNAVAILABLE
        for issue in mock_issue_registry.deleted
    )


@pytest.mark.asyncio
async def test_async_update_data_http_retry_after_invalid(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    coord._cloud_issue_reported = True
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=418,
        message="I'm a teapot",
        headers={"Retry-After": "invalid"},
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._cloud_issue_reported is False


@pytest.mark.asyncio
async def test_async_update_data_unauthorized_promotes_config_error(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(side_effect=Unauthorized())
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_data_http_status_phrase_fallback(coordinator_factory):
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=429,
        message="  ",
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_description == HTTPStatus(429).phrase


def test_summary_compat_shims(coordinator_factory):
    coord = coordinator_factory()
    coord.summary = None

    assert coord._summary_cache is None
    coord._summary_cache = (0.0, [], 5.0)
    assert coord._summary_cache == (0.0, [], 5.0)

    coord._summary_ttl = 12.5
    assert coord._summary_ttl == 12.5

    dummy_summary = SimpleNamespace(_cache=None, ttl=0.0, _ttl=0.0)
    coord.summary = dummy_summary
    coord._summary_cache = (1.0, [{"sn": "1"}], 2.0)
    assert dummy_summary._cache == (1.0, [{"sn": "1"}], 2.0)
    coord._summary_ttl = 15.0
    assert dummy_summary._ttl == 15.0

    coord.__dict__.pop("session_history", None)
    coord._session_history_cache_ttl = None
    assert coord._session_history_cache_ttl is None

    coord.session_history = SimpleNamespace(cache_ttl=300)
    coord._session_history_cache_ttl = 120
    assert coord.session_history.cache_ttl == 120
    assert coord._session_history_cache_ttl == 120


@pytest.mark.asyncio
async def test_async_enrich_sessions_invokes_history(coordinator_factory):
    coord = coordinator_factory()
    serials = [SERIAL_ONE]
    fake_history = SimpleNamespace(
        async_enrich=AsyncMock(return_value={"123": []}),
        cache_ttl=MIN_SESSION_HISTORY_CACHE_TTL,
    )
    coord.session_history = fake_history

    day = datetime(2025, 5, 1, tzinfo=timezone.utc)
    result = await coord._async_enrich_sessions(serials, day, in_background=False)

    assert result == {"123": []}
    fake_history.async_enrich.assert_awaited_once_with(
        serials, day, in_background=False
    )


@pytest.mark.asyncio
async def test_async_enrich_sessions_invokes_history_with_max_cache_age(
    coordinator_factory,
):
    coord = coordinator_factory()
    serials = [SERIAL_ONE]
    fake_history = SimpleNamespace(
        async_enrich=AsyncMock(return_value={"123": []}),
        cache_ttl=MIN_SESSION_HISTORY_CACHE_TTL,
    )
    coord.session_history = fake_history

    day = datetime(2025, 5, 1, tzinfo=timezone.utc)
    result = await coord._async_enrich_sessions(
        serials,
        day,
        in_background=True,
        max_cache_age=120.0,
    )

    assert result == {"123": []}
    fake_history.async_enrich.assert_awaited_once_with(
        serials,
        day,
        in_background=True,
        max_cache_age=120.0,
    )


@pytest.mark.asyncio
async def test_async_fetch_sessions_today_handles_timezone_error(
    monkeypatch, coordinator_factory
):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))

    original = coord_mod.dt_util.as_local
    calls = {"count": 0}

    def fake_as_local(value):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("boom")
        return original(value)

    monkeypatch.setattr(coord_mod.dt_util, "as_local", fake_as_local)
    coord.session_history = SimpleNamespace(
        cache_ttl=MIN_SESSION_HISTORY_CACHE_TTL,
        _async_fetch_sessions_today=AsyncMock(return_value=[{"energy_kwh": 1.2}]),
    )

    naive_day = datetime(2025, 1, 1, 12, 0, 0)
    first = await coord._async_fetch_sessions_today(sn, day_local=naive_day)
    assert first == [{"energy_kwh": 1.2}]

    # Immediate second call should reuse cache without invoking session history again.
    second = await coord._async_fetch_sessions_today(sn, day_local=naive_day)
    assert second == first
    coord.session_history._async_fetch_sessions_today.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_history_shims_without_manager(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    coord.__dict__.pop("session_history", None)

    day = datetime(2025, 5, 1, tzinfo=timezone.utc)
    sessions = await coord._async_enrich_sessions(["SN"], day, in_background=False)
    assert sessions == {}

    assert coord._sum_session_energy(
        [{"energy_kwh": 1.0}, {"energy_kwh": "bad"}]
    ) == pytest.approx(1.0)

    result = await coord._async_fetch_sessions_today("", day_local=day)
    assert result == []

    monkeypatch.setattr(
        coord_mod.dt_util, "now", lambda: datetime(2025, 5, 1, 10, 0, 0)
    )
    assert await coord._async_fetch_sessions_today("SN", day_local=None) == []


def test_prune_runtime_caches_removes_stale_serial_state(coordinator_factory):
    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord._configured_serials = {"EV1"}  # noqa: SLF001
    coord.serials = {"EV1", "EV2", "EV3"}
    coord._serial_order = ["EV1", "EV2", "EV3"]  # noqa: SLF001
    coord.last_set_amps = {"EV1": 16, "EV2": 32}
    coord._charge_mode_cache = {"EV1": ("A", 1.0), "EV2": ("B", 1.0)}  # noqa: SLF001
    coord._green_battery_cache = {  # noqa: SLF001
        "EV1": (True, True, 1.0),
        "EV2": (False, True, 1.0),
    }
    coord._auth_settings_cache = {  # noqa: SLF001
        "EV1": (True, False, True, True, 1.0),
        "EV3": (False, False, True, True, 1.0),
    }
    coord._evse_transition_snapshots = {  # noqa: SLF001
        "EV1": [{"to_connector_status": "CHARGING"}],
        "EV2": [{"to_connector_status": "SUSPENDED_EVSE"}],
    }
    coord._desired_charging = {"EV1": True, "EV2": False}  # noqa: SLF001
    coord._session_history_cache_shim = {  # noqa: SLF001
        ("EV1", "2020-01-02"): (1.0, [{"session_id": "keep"}]),
        ("EV2", "2020-01-02"): (1.0, [{"session_id": "drop-serial"}]),
        ("EV1", "2020-01-01"): (1.0, [{"session_id": "drop-day"}]),
    }
    coord.session_history = SimpleNamespace(prune=MagicMock(), clear=MagicMock())

    coord._prune_runtime_caches(  # noqa: SLF001
        active_serials={"EV1"},
        keep_day_keys={"2020-01-02"},
    )

    assert coord.serials == {"EV1"}
    assert coord._serial_order == ["EV1"]  # noqa: SLF001
    assert coord.last_set_amps == {"EV1": 16}
    assert "EV2" not in coord._charge_mode_cache  # noqa: SLF001
    assert "EV2" not in coord._green_battery_cache  # noqa: SLF001
    assert "EV3" not in coord._auth_settings_cache  # noqa: SLF001
    assert coord._evse_transition_snapshots == {  # noqa: SLF001
        "EV1": [{"to_connector_status": "CHARGING"}]
    }
    assert coord._desired_charging == {"EV1": True}  # noqa: SLF001
    assert coord._session_history_cache_shim == {  # noqa: SLF001
        ("EV1", "2020-01-02"): (1.0, [{"session_id": "keep"}])
    }
    coord.session_history.prune.assert_called_once()


def test_prune_runtime_caches_drops_configured_retired_serial_after_inventory_ready(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord._configured_serials = {"EV1", "EV2"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.serials = {"EV1", "EV2"}
    coord._serial_order = ["EV1", "EV2"]  # noqa: SLF001
    coord.last_set_amps = {"EV1": 16, "EV2": 32}

    coord._prune_runtime_caches(  # noqa: SLF001
        active_serials={"EV1"},
        keep_day_keys={"2020-01-02"},
    )

    assert coord.serials == {"EV1"}
    assert coord._serial_order == ["EV1"]  # noqa: SLF001
    assert coord.last_set_amps == {"EV1": 16}


def test_iter_serials_uses_authoritative_active_inventory(coordinator_factory):
    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "iqevse": {
            "count": 3,
            "devices": [
                {"serial_number": "EV1"},
                {"serialNumber": "EV1"},
                {"serial": "EV3"},
            ],
        }
    }
    coord._type_device_order = ["iqevse"]  # noqa: SLF001
    coord.data = {"EV2": {"status": "stale"}}

    assert coord.iter_serials() == ["EV1", "EV3"]

    coord._type_device_buckets = {"iqevse": {"count": 0, "devices": []}}  # noqa: SLF001
    coord._type_device_order = []  # noqa: SLF001

    assert coord.iter_serials() == []


def test_iter_serials_waits_with_empty_non_authoritative_status_when_evse_bucket_missing(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["OLD"])
    coord.discovery_snapshot.apply({"serial_order": ["NEW", "NEW"]})
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._status_charger_data_authoritative = False  # noqa: SLF001
    coord.data = {}
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001

    assert coord._active_inventory_evse_serials() is None  # noqa: SLF001
    assert coord.iter_serials() == ["NEW"]
    assert coord.serials == {"NEW"}


def test_iter_serials_drops_restored_snapshot_when_empty_status_is_authoritative(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["OLD"])
    coord.discovery_snapshot.apply({"serial_order": ["NEW", "NEW"]})
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._status_charger_data_authoritative = True  # noqa: SLF001
    coord.data = {}
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001

    assert coord.iter_serials() == []


def test_iter_serials_uses_fresh_status_when_evse_bucket_missing(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["OLD"])
    coord.discovery_snapshot.apply({"serial_order": ["RESTORED"]})
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._status_charger_data_authoritative = True  # noqa: SLF001
    coord.payload_using_stale = False
    coord.data = {"ACTIVE": {"sn": "ACTIVE"}}
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001

    assert coord.iter_serials() == ["ACTIVE"]


def test_iter_serials_prefers_fresh_status_over_stale_evse_bucket(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["OLD"])
    coord.discovery_snapshot.apply({"serial_order": ["OLD", "ACTIVE"]})
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._status_charger_data_authoritative = True  # noqa: SLF001
    coord.payload_using_stale = False
    coord.data = {"ACTIVE": {"sn": "ACTIVE"}}
    coord._type_device_buckets = {  # noqa: SLF001
        "iqevse": {
            "count": 2,
            "devices": [
                {"serial_number": "OLD"},
                {"serial_number": "ACTIVE"},
            ],
        }
    }
    coord._type_device_order = ["iqevse"]  # noqa: SLF001

    assert coord._active_inventory_evse_serials() == ["ACTIVE"]  # noqa: SLF001
    assert coord.iter_serials() == ["ACTIVE"]


def test_iter_serials_waits_when_only_stale_status_is_available(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["OLD"])
    coord.discovery_snapshot.apply({"serial_order": ["RESTORED"]})
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.payload_using_stale = True
    coord.data = {"STALE": {"sn": "STALE"}}
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001

    assert coord._active_inventory_evse_serials() is None  # noqa: SLF001
    assert coord.iter_serials() == ["RESTORED", "STALE"]


def test_iter_serials_uses_restored_snapshot_when_inventory_view_raises(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["OLD"])
    coord.discovery_snapshot.apply({"serial_order": ["NEW"]})
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.inventory_view = SimpleNamespace(
        type_bucket=lambda _type_key: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    assert coord.iter_serials() == ["NEW", "OLD"]


def test_iter_serials_handles_malformed_authoritative_inventory(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.inventory_view = SimpleNamespace(
        type_bucket=lambda _type_key: {"count": 1, "devices": "bad"}
    )

    assert coord.iter_serials() == ["EV1"]

    coord.inventory_view = SimpleNamespace(
        type_bucket=lambda _type_key: {"devices": []}
    )

    assert coord.iter_serials() == ["EV1"]

    class BadSerial:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    class BadCount:
        def __int__(self) -> int:
            raise TypeError("boom")

        def __str__(self) -> str:
            raise TypeError("boom")

    coord.inventory_view = SimpleNamespace(
        type_bucket=lambda _type_key: {
            "count": BadCount(),
            "devices": [
                "not-a-dict",
                {"serial_number": BadSerial()},
            ],
        }
    )

    assert coord.iter_serials() == ["EV1"]

    coord.inventory_view = SimpleNamespace(
        type_bucket=lambda _type_key: {"count": 0, "devices": []}
    )

    assert coord.iter_serials() == []


def test_cleanup_runtime_state_clears_session_history(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    clear = MagicMock()
    prune = MagicMock()
    warmup_task = MagicMock()
    stream_stop_task = MagicMock()
    amp_restart_task = MagicMock()
    coord.session_history = SimpleNamespace(clear=clear, prune=prune)
    coord._session_history_cache_shim = {
        ("EV1", "2020-01-02"): (1.0, [])
    }  # noqa: SLF001
    coord._warmup_task = warmup_task  # noqa: SLF001
    coord._streaming_stop_task = stream_stop_task  # noqa: SLF001
    coord._amp_restart_tasks = {"EV1": amp_restart_task}  # noqa: SLF001
    coord._streaming = True  # noqa: SLF001
    coord._streaming_until = 123.0  # noqa: SLF001
    coord._streaming_manual = True  # noqa: SLF001
    coord._streaming_targets = {"EV1": True}  # noqa: SLF001

    coord.cleanup_runtime_state()

    warmup_task.cancel.assert_called_once()
    stream_stop_task.cancel.assert_called_once()
    amp_restart_task.cancel.assert_called_once()
    assert coord._warmup_task is None  # noqa: SLF001
    assert coord._streaming_stop_task is None  # noqa: SLF001
    assert coord._amp_restart_tasks == {}  # noqa: SLF001
    assert coord._streaming is False  # noqa: SLF001
    assert coord._streaming_until is None  # noqa: SLF001
    assert coord._streaming_manual is False  # noqa: SLF001
    assert coord._streaming_targets == {}  # noqa: SLF001
    clear.assert_called_once()
    assert coord._session_history_cache_shim == {}  # noqa: SLF001


def test_prune_helpers_cover_edge_branches(monkeypatch):
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))

    class BadSerial:
        def __str__(self) -> str:
            raise RuntimeError("bad")

    assert coord._normalize_serials(None) == set()  # noqa: SLF001
    assert coord._normalize_serials([None, BadSerial(), " EV1 "]) == {
        "EV1"
    }  # noqa: SLF001

    coord._session_history_cache_shim = []  # noqa: SLF001
    monkeypatch.setattr(
        coord_mod.dt_util,
        "now",
        lambda: datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(coord_mod.dt_util, "as_local", lambda value: value)
    coord._prune_session_history_cache_shim(  # noqa: SLF001
        active_serials=None,
        keep_day_keys={"2025-01-01"},
    )
    assert coord._session_history_cache_shim == {}  # noqa: SLF001

    coord._configured_serials = set()  # noqa: SLF001
    coord.serials = None
    coord._serial_order = None  # noqa: SLF001
    coord.last_set_amps = []
    coord._operating_v = {}  # noqa: SLF001
    coord._charge_mode_cache = {}  # noqa: SLF001
    coord._green_battery_cache = {}  # noqa: SLF001
    coord._auth_settings_cache = {}  # noqa: SLF001
    coord._last_charging = {}  # noqa: SLF001
    coord._last_actual_charging = {}  # noqa: SLF001
    coord._pending_charging = {}  # noqa: SLF001
    coord._desired_charging = {}  # noqa: SLF001
    coord._auto_resume_attempts = {}  # noqa: SLF001
    coord._session_end_fix = {}  # noqa: SLF001
    coord._evse_transition_snapshots = {}  # noqa: SLF001
    coord._streaming_targets = {}  # noqa: SLF001

    keep = coord._prune_serial_runtime_state(["EV1"])  # noqa: SLF001
    assert keep == {"EV1"}
    assert coord.serials == {"EV1"}
    assert coord._serial_order == ["EV1"]  # noqa: SLF001


def test_evse_diagnostics_payloads_include_runtime_sources_and_history(
    coordinator_factory,
):
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord.data = {
        SERIAL_ONE: {
            "charge_mode_supported_source": "feature_flag",
            "charge_mode_source": "cache_backoff",
            "charge_mode_pref_source": "cache_backoff",
            "charging_level_source": "last_set_amps",
            "charging_level": 32,
        }
    }
    coord.last_set_amps = {SERIAL_ONE: 16}
    coord._evse_transition_snapshots = {  # noqa: SLF001
        SERIAL_ONE: [
            {
                "from_connector_status": "SUSPENDED_EVSE",
                "to_connector_status": "CHARGING",
                "charge_mode_source": "schedule_type_fallback",
            }
        ]
    }

    diagnostics = coord.evse_diagnostics_payloads()

    assert diagnostics["charger_runtime_sources"] == [
        {
            "serial": SERIAL_ONE,
            "sources": {
                "charge_mode_source": "cache_backoff",
                "charge_mode_pref_source": "cache_backoff",
                "charging_level_source": "last_set_amps",
                "charging_level": 32,
                "last_set_amps": 16,
            },
        }
    ]
    assert diagnostics["charger_transition_history"] == [
        {
            "serial": SERIAL_ONE,
            "transitions": [
                {
                    "from_connector_status": "SUSPENDED_EVSE",
                    "to_connector_status": "CHARGING",
                    "charge_mode_source": "schedule_type_fallback",
                }
            ],
        }
    ]


def test_record_evse_transition_snapshot_ignores_missing_or_unchanged_status(
    coordinator_factory,
):
    coord = coordinator_factory(serials=[SERIAL_ONE])

    coord._record_evse_transition_snapshot(  # noqa: SLF001
        SERIAL_ONE,
        None,
        {"connector_status": "CHARGING"},
    )
    coord._record_evse_transition_snapshot(  # noqa: SLF001
        SERIAL_ONE,
        {"connector_status": "CHARGING"},
        {"connector_status": "CHARGING"},
    )

    assert coord._evse_transition_snapshots == {}  # noqa: SLF001


def test_snapshot_text_handles_bad_str(coordinator_factory):
    coord = coordinator_factory(serials=[SERIAL_ONE])

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert coord._snapshot_text(BadStr()) is None  # noqa: SLF001


def test_charge_mode_resolution_parts_supports_legacy_string(coordinator_factory):
    coord = coordinator_factory(serials=[SERIAL_ONE])

    assert coord._charge_mode_resolution_parts("SCHEDULED") == (  # noqa: SLF001
        "SCHEDULED",
        None,
    )


@pytest.mark.asyncio
async def test_async_update_data_records_evse_transition_snapshot(
    coordinator_factory, monkeypatch, caplog
):
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord.data = {
        SERIAL_ONE: {
            "sn": SERIAL_ONE,
            "name": "Charger",
            "connector_status": "SUSPENDED_EVSE",
            "charging": False,
        }
    }
    coord.last_set_amps = {SERIAL_ONE: 16}
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": SERIAL_ONE,
                    "name": "Garage EV",
                    "pluggedIn": True,
                    "charging": False,
                    "chargingLevel": 32,
                    "connectors": [{"connectorStatusType": "CHARGING"}],
                    "sch_d": {"info": [{"type": "greencharging"}]},
                }
            ]
        }
    )
    timestamp = datetime(2026, 4, 3, 1, 2, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: timestamp)

    with caplog.at_level(logging.DEBUG):
        result = await coord._async_update_data()

    assert result[SERIAL_ONE]["charge_mode"] == "GREEN_CHARGING"
    assert result[SERIAL_ONE]["charge_mode_source"] == "schedule_type_fallback"
    assert result[SERIAL_ONE]["charge_mode_pref_source"] == "schedule_type_fallback"
    assert result[SERIAL_ONE]["charging_level"] == 32
    assert result[SERIAL_ONE]["charging_level_source"] == "status_payload"

    assert coord._evse_transition_snapshots[SERIAL_ONE] == [  # noqa: SLF001
        {
            "recorded_at_utc": timestamp.isoformat(),
            "from_connector_status": "SUSPENDED_EVSE",
            "to_connector_status": "CHARGING",
            "charging": True,
            "previous_charging": False,
            "charge_mode": "GREEN_CHARGING",
            "charge_mode_source": "schedule_type_fallback",
            "charge_mode_pref": "GREEN_CHARGING",
            "charge_mode_pref_source": "schedule_type_fallback",
            "charging_level": 32,
            "charging_level_source": "status_payload",
            "last_set_amps": 16,
            "green_battery_enabled": None,
            "safe_limit_state": None,
            "schedule_type": "greencharging",
            "sampled_at_utc": None,
            "fetched_at_utc": result[SERIAL_ONE]["fetched_at_utc"],
            "scheduler_available": True,
            "scheduler_backoff_active": False,
        }
    ]
    assert "EVSE connector transition for charger" in caplog.text


@pytest.mark.asyncio
async def test_async_update_data_invalid_charging_level_uses_last_set_amps(
    coordinator_factory,
):
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord.last_set_amps = {SERIAL_ONE: 16}
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": SERIAL_ONE,
                    "name": "Garage EV",
                    "pluggedIn": True,
                    "charging": False,
                    "chargingLevel": [],
                    "connectors": [{"connectorStatusType": "SUSPENDED_EVSE"}],
                }
            ]
        }
    )

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["charging_level"] == 16
    assert result[SERIAL_ONE]["charging_level_source"] == "last_set_amps"


def _make_minimal_history() -> SimpleNamespace:
    return SimpleNamespace(
        cache_ttl=60,
        get_cache_view=lambda *_args, **_kwargs: SimpleNamespace(
            sessions=[],
            needs_refresh=False,
            blocked=False,
        ),
        async_enrich=AsyncMock(return_value={}),
        schedule_enrichment=lambda *_args, **_kwargs: None,
        sum_energy=lambda *_args, **_kwargs: 0.0,
        prune=MagicMock(),
    )


def _prepare_minimal_success_update(coord, sn: str) -> None:
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": sn,
                    "name": "Driveway",
                    "charging": False,
                    "pluggedIn": True,
                    "faulted": False,
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {"status": "enabled", "info": [{}]},
                }
            ],
            "ts": 1700000000,
        }
    )
    coord._async_resolve_charge_modes = AsyncMock(return_value={})
    coord._async_resolve_green_battery_settings = AsyncMock(return_value={})
    coord._async_resolve_auth_settings = AsyncMock(return_value={})
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_kwargs: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=lambda: None,
    )
    coord.evse_timeseries = SimpleNamespace(
        refresh_due=MagicMock(return_value=True),
        async_refresh=AsyncMock(),
        merge_charger_payloads=MagicMock(),
        diagnostics=lambda: {},
    )
    coord.session_history = _make_minimal_history()
    coord.energy._async_refresh_site_energy = AsyncMock()
    coord._sync_site_energy_issue = MagicMock()
    coord._sync_battery_profile_pending_issue = MagicMock()
    coord._async_refresh_inverters = AsyncMock()
    coord._async_refresh_heatpump_power = AsyncMock()
    coord._async_refresh_battery_site_settings = AsyncMock()
    coord._async_refresh_battery_status = AsyncMock()
    coord._async_refresh_battery_backup_history = AsyncMock()
    coord._async_refresh_battery_settings = AsyncMock()
    coord._async_refresh_storm_guard_profile = AsyncMock()
    coord._async_refresh_storm_alert = AsyncMock()
    coord._async_refresh_grid_control_check = AsyncMock()
    coord._async_refresh_devices_inventory = AsyncMock()
    coord._async_refresh_hems_devices = AsyncMock()


@pytest.mark.asyncio
async def test_async_update_data_merges_evse_timeseries(
    coordinator_factory, monkeypatch
):
    sn = "EV-TS"
    coord = coordinator_factory(serials=[sn])
    coord._has_successful_refresh = True  # noqa: SLF001
    _prepare_minimal_success_update(coord, sn)
    day_local = datetime(2026, 3, 11, 9, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "now", lambda: day_local)
    original_merge = coord.evse_timeseries.merge_charger_payloads

    def _merge(payloads, *, day_local):
        payloads[sn]["evse_daily_energy_kwh"] = 4.5
        payloads[sn]["evse_lifetime_energy_kwh"] = 120.0
        payloads[sn]["evse_timeseries_source"] = "evse_timeseries"
        original_merge(payloads, day_local=day_local)

    coord.evse_timeseries.merge_charger_payloads = MagicMock(side_effect=_merge)

    result = await coord._async_update_data()

    coord.evse_timeseries.async_refresh.assert_awaited_once()
    coord.evse_timeseries.merge_charger_payloads.assert_called_once()
    assert result[sn]["evse_daily_energy_kwh"] == pytest.approx(4.5)
    assert result[sn]["evse_lifetime_energy_kwh"] == pytest.approx(120.0)
    assert result[sn]["evse_timeseries_source"] == "evse_timeseries"


@pytest.mark.asyncio
async def test_async_update_data_ignores_evse_timeseries_exception(
    coordinator_factory, monkeypatch
):
    sn = "EV-TS-ERR"
    coord = coordinator_factory(serials=[sn])
    _prepare_minimal_success_update(coord, sn)
    monkeypatch.setattr(
        coord_mod.dt_util,
        "now",
        lambda: datetime(2026, 3, 11, 9, 0, 0, tzinfo=timezone.utc),
    )
    coord.evse_timeseries.async_refresh = AsyncMock(side_effect=RuntimeError("boom"))

    result = await coord._async_update_data()

    assert sn in result


@pytest.mark.asyncio
async def test_async_update_data_session_day_handles_now_error(
    coordinator_factory, monkeypatch
):
    sn = "EVX"
    coord = coordinator_factory(serials=[sn])
    _prepare_minimal_success_update(coord, sn)
    monkeypatch.setattr(
        coord_mod.dt_util,
        "now",
        MagicMock(side_effect=RuntimeError("boom")),
    )

    result = await coord._async_update_data()
    assert sn in result


@pytest.mark.asyncio
async def test_async_update_data_session_day_handles_naive_now(
    coordinator_factory, monkeypatch
):
    sn = "EVY"
    coord = coordinator_factory(serials=[sn])
    _prepare_minimal_success_update(coord, sn)
    coord._prune_runtime_caches = MagicMock()  # noqa: SLF001
    naive_now = datetime(2025, 1, 2, 9, 0, 0)
    monkeypatch.setattr(coord_mod.dt_util, "now", lambda: naive_now)
    original_as_local = coord_mod.dt_util.as_local
    raised = {"value": False}

    def _fake_as_local(value):
        if (
            isinstance(value, datetime)
            and value == naive_now
            and value.tzinfo is None
            and not raised["value"]
        ):
            raised["value"] = True
            raise ValueError("boom")
        return original_as_local(value)

    monkeypatch.setattr(coord_mod.dt_util, "as_local", _fake_as_local)

    result = await coord._async_update_data()
    assert sn in result
    assert raised["value"] is True
    coord._prune_runtime_caches.assert_called_once()  # noqa: SLF001
    prune_kwargs = coord._prune_runtime_caches.call_args.kwargs  # noqa: SLF001
    assert prune_kwargs.get("active_serials") == result.keys()
    assert isinstance(prune_kwargs.get("keep_day_keys"), dict)
    assert prune_kwargs["keep_day_keys"]


def test_sum_session_energy_handles_conversion_error(coordinator_factory):
    coord = coordinator_factory()
    coord.__dict__.pop("session_history", None)

    class UnfriendlyInt(int):
        def __float__(self):
            raise ValueError("boom")

    total = coord._sum_session_energy([{"energy_kwh": UnfriendlyInt(5)}])
    assert total == 0.0


def test_collect_site_metrics_serializes_dates(coordinator_factory):
    coord = coordinator_factory()

    class BadDate:
        def __init__(self, label: str) -> None:
            self._label = label

        def isoformat(self) -> str:
            raise ValueError("fail")

        def __str__(self) -> str:
            return self._label

    coord.site_name = "Garage"
    coord.last_success_utc = BadDate("success")
    coord.last_failure_utc = BadDate("failure")
    coord.backoff_ends_utc = BadDate("backoff")
    coord.last_failure_status = 500
    coord.last_failure_description = "boom"
    coord.last_failure_source = "http"
    coord.last_failure_response = '{"error":"boom"}'
    coord._backoff_until = coord_mod.time.monotonic() + 10

    metrics = coord.collect_site_metrics()
    assert metrics["last_success"] == "success"
    assert metrics["last_failure"] == "failure"
    assert metrics["backoff_ends_utc"] == "backoff"

    placeholders = coord._issue_translation_placeholders(metrics)
    assert placeholders == {
        "site_id": coord.site_id,
        "site_name": "Garage",
        "last_error": "boom",
        "last_status": "500",
        "backoff_ends": "backoff",
        "failure_count": "0",
        "hems_failure_count": "0",
    }


def test_collect_site_metrics_handles_empty_and_invalid_type_buckets(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._type_device_order = ["empty", "broken"]  # noqa: SLF001
    coord._selected_type_keys = {"empty", "broken"}  # noqa: SLF001
    coord._type_device_buckets = {  # noqa: SLF001
        "empty": None,
        "broken": {"count": "not-an-int"},
    }

    metrics = coord.collect_site_metrics()

    assert "empty" not in metrics["type_device_counts"]
    assert metrics["type_device_counts"]["broken"] == 0


@pytest.mark.asyncio
async def test_async_update_data_http_error_creates_cloud_issue(
    coordinator_factory, mock_issue_registry, monkeypatch
):
    coord = coordinator_factory()
    headers = CIMultiDictProxy(
        CIMultiDict({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    )
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=503,
        message='{"error":{"displayMessage":"scheduled maintenance"}}',
        headers=headers,
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._http_errors = 2
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None
    assert coord.last_failure_description == "scheduled maintenance"
    assert any(issue[1] == ISSUE_CLOUD_ERRORS for issue in mock_issue_registry.created)


@pytest.mark.asyncio
async def test_async_update_data_network_dns_issue(
    coordinator_factory, mock_issue_registry, monkeypatch
):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(
        side_effect=aiohttp.ClientError("dns failure in name resolution")
    )
    coord._network_errors = 2
    coord._dns_failures = 1
    coord._schedule_backoff_timer = MagicMock()
    monkeypatch.setattr(coord, "_slow_interval_floor", lambda: 30)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._backoff_until is not None
    assert any(
        issue[1] == ISSUE_NETWORK_UNREACHABLE for issue in mock_issue_registry.created
    )
    assert any(
        issue[1] == ISSUE_DNS_RESOLUTION for issue in mock_issue_registry.created
    )


@pytest.mark.asyncio
async def test_async_update_data_network_error_keeps_existing_dns_issue(
    coordinator_factory, mock_issue_registry, monkeypatch
):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(side_effect=aiohttp.ClientError("boom"))
    coord._schedule_backoff_timer = MagicMock()
    coord._network_errors = 3
    coord._dns_issue_reported = True
    coord._dns_failures = 2

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._dns_issue_reported is True
    assert coord._dns_failures == 2


def test_sync_desired_charging_schedules_auto_resume(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    now = coord_mod.time.monotonic()
    coord._desired_charging = {sn: True}
    coord._auto_resume_attempts = {}
    coord.data = {
        sn: {
            "sn": sn,
            "charging": False,
            "plugged": True,
            "connector_status": coord_mod.SUSPENDED_EVSE_STATUS,
        }
    }
    created = []

    def fake_create_task(coro, *, name=None):
        created.append((coro, name))
        return None

    monkeypatch.setattr(coord.hass, "async_create_task", fake_create_task)

    coord._sync_desired_charging(coord.data)

    assert len(created) == 1
    coro, name = created[0]
    assert name == f"enphase_ev_auto_resume_{sn}"
    coro.close()
    assert coord._auto_resume_attempts[sn] >= now


@pytest.mark.asyncio
async def test_async_auto_resume_respects_preferences(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.pick_start_amps = MagicMock(return_value=24)
    prefs = ChargeModeStartPreferences(
        mode="SCHEDULED_CHARGING",
        include_level=True,
        strict=True,
        enforce_mode="SCHEDULED_CHARGING",
    )
    coord._charge_mode_start_preferences = MagicMock(return_value=prefs)
    coord._ensure_charge_mode = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.data = {sn: {"plugged": True}}

    await coord._async_auto_resume(sn, {"plugged": True})

    coord.client.start_charging.assert_awaited_once_with(
        sn, 24, 1, include_level=True, strict_preference=True
    )
    coord._ensure_charge_mode.assert_awaited_once_with(sn, "SCHEDULED_CHARGING")


@pytest.mark.asyncio
async def test_async_auto_resume_aborts_when_unplugged(coordinator_factory):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    coord.client.start_charging = AsyncMock()
    coord.data = {sn: {"plugged": False}}

    await coord._async_auto_resume(sn, {"plugged": False})
    coord.client.start_charging.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_auto_resume_not_ready_breaks_loop(coordinator_factory):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    coord.client.start_charging = AsyncMock(return_value={"status": "not_ready"})
    coord.pick_start_amps = MagicMock(return_value=32)
    coord._charge_mode_start_preferences = MagicMock(
        return_value=ChargeModeStartPreferences(
            mode="SCHEDULED_CHARGING",
            include_level=True,
            strict=True,
            enforce_mode="SCHEDULED_CHARGING",
        )
    )
    await coord._async_auto_resume(sn, {"plugged": True})
    coord._charge_mode_start_preferences.assert_called()
    coord.client.start_charging.assert_awaited_once()


def test_apply_lifetime_guard_confirms_resets(monkeypatch):
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.summary = SimpleNamespace(invalidate=MagicMock())
    coord.energy._lifetime_guard = {}

    sn = "EV1"
    first = coord.energy._apply_lifetime_guard(sn, 15000, {"lifetime_kwh": 12.0})
    assert first == pytest.approx(15.0)

    # Drop to trigger pending reset detection
    with monkeypatch.context() as ctx:
        ticker = deque([1_000.0, 1_005.0, 1_020.0])
        ctx.setattr(coord_mod.time, "monotonic", lambda: ticker[0])
        drop = coord.energy._apply_lifetime_guard(sn, 2000, None)
        assert drop == pytest.approx(15.0)
        ticker.popleft()
        confirmed = coord.energy._apply_lifetime_guard(sn, 2000, None)
        assert confirmed == pytest.approx(2.0)

    coord.summary.invalidate.assert_called_once()


@pytest.mark.asyncio
async def test_async_update_data_success_enriches_payload(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory(serials=["SN1"])
    sn = "SN1"
    original_round = builtins.round

    def fake_round(value, ndigits=None):
        if value == 5.0 and ndigits == 3:
            raise ValueError("round boom")
        if ndigits is None:
            return original_round(value)
        return original_round(value, ndigits)

    monkeypatch.setattr(builtins, "round", fake_round)
    coord.last_set_amps[sn] = 32
    status_payload = {
        "evChargerData": [
            {},
            {
                "sn": sn,
                "name": "Garage",
                "charging": False,
                "pluggedIn": True,
                "faulted": False,
                "chargeMode": None,
                "chargingLevel": None,
                "session_d": {
                    "sessionId": "abc",
                    "start_time": 100,
                    "plg_in_at": "2025-10-30T05:00:00Z",
                    "plg_out_at": "2025-10-30T06:00:00Z",
                    "e_c": 100,
                    "miles": "5",
                    "sessionCost": "1.5",
                    "chargeProfileStackLevel": "1",
                },
                "connectors": [
                    {
                        "connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS,
                        "commissioned": "true",
                    }
                ],
                "sch_d": {
                    "status": "enabled",
                    "info": [{"type": "smart", "startTime": "1", "endTime": "2"}],
                },
                "lst_rpt_at": None,
                "session_energy_wh": "50",
                "commissioned": None,
                "operating_v": 240,
                "displayName": " Display ",
            },
        ],
        "ts": "2025-10-30T10:00:00Z[UTC]",
    }
    coord.client.status = AsyncMock(return_value=status_payload)
    coord._async_resolve_charge_modes = AsyncMock(return_value={})
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: True,
        async_fetch=AsyncMock(
            return_value=[
                {
                    "serialNumber": sn,
                    "maxCurrent": 48,
                    "chargeLevelDetails": {"min": "16", "max": "40"},
                    "phaseMode": "split",
                    "status": "online",
                    "activeConnection": "wifi",
                    "networkConfig": '[{"ipaddr":"1.2.3.4","connectionStatus":"1"}]',
                    "reportingInterval": "60",
                    "dlbEnabled": "true",
                    "commissioningStatus": True,
                    "lastReportedAt": "2025-10-30T09:59:00Z",
                    "operatingVoltage": "240",
                    "lifeTimeConsumption": 10000,
                    "firmwareVersion": "1.0",
                    "hardwareVersion": "revA",
                    "displayName": "Friendly",
                }
            ]
        ),
        invalidate=lambda: None,
    )

    class _DummyView:
        def __init__(self):
            self.sessions = [{"energy_kwh": 1.0}]
            self.needs_refresh = True
            self.blocked = False

    class _DummyHistory:
        cache_ttl = 60

        def get_cache_view(self, *_args, **_kwargs):
            return _DummyView()

        async def async_enrich(self, *_args, **_kwargs):
            return {sn: [{"energy_kwh": 1.0}]}

        def schedule_enrichment(self, *_args, **_kwargs):
            return None

        def sum_energy(self, sessions):
            return 2.0

    coord.session_history = _DummyHistory()

    result = await coord._async_update_data()
    assert sn in result
    entry = result[sn]
    assert entry["charge_mode"] == "IDLE"
    assert entry["session_energy_raw"] == 100
    assert entry["session_energy_unit"] == "Wh"
    assert entry["session_energy_wh"] == pytest.approx(100.0)
    assert entry["session_kwh"] == pytest.approx(0.1)
    assert entry["energy_today_sessions_kwh"] == 2.0


@pytest.mark.asyncio
async def test_async_update_data_normalizes_small_iq_evse_session_wh(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1"])
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "EV1",
                    "name": "Garage",
                    "charging": False,
                    "pluggedIn": True,
                    "faulted": False,
                    "session_d": {
                        "start_time": 1781370420,
                        "e_c": 140.038,
                    },
                    "connectors": [
                        {"connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS}
                    ],
                }
            ],
            "ts": "2026-06-13T17:15:58Z[UTC]",
        }
    )
    coord._async_resolve_charge_modes = AsyncMock(return_value={})
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: True,
        async_fetch=AsyncMock(
            return_value=[
                {
                    "serialNumber": "EV1",
                    "modelId": "IQ-EVSE-NA-1080-0100-0100",
                    "modelName": "IQ-EVSE-80R",
                }
            ]
        ),
        invalidate=lambda: None,
    )

    class _DummyHistory:
        cache_ttl = 60

        def get_cache_view(self, *_args, **_kwargs):
            return SimpleNamespace(sessions=[], needs_refresh=False, blocked=False)

        def schedule_enrichment(self, *_args, **_kwargs):
            return None

        def sum_energy(self, sessions):
            return 0.0

    coord.session_history = _DummyHistory()

    result = await coord._async_update_data()

    entry = result["EV1"]
    assert entry["session_energy_raw"] == pytest.approx(140.038)
    assert entry["session_energy_unit"] == "Wh"
    assert entry["session_energy_wh"] == pytest.approx(140.038)
    assert entry["session_kwh"] == pytest.approx(0.14)


@pytest.mark.asyncio
async def test_async_update_data_preserves_unparseable_session_energy(
    coordinator_factory,
):
    class BadEnergy:
        def __float__(self):
            raise ValueError("bad energy")

    bad_energy = BadEnergy()
    coord = coordinator_factory(serials=["EV1"])
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "EV1",
                    "name": "Garage",
                    "charging": False,
                    "pluggedIn": True,
                    "faulted": False,
                    "session_d": {"e_c": bad_energy},
                    "connectors": [
                        {"connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS}
                    ],
                }
            ],
            "ts": "2026-06-13T17:15:58Z[UTC]",
        }
    )
    coord._async_resolve_charge_modes = AsyncMock(return_value={})
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=lambda: None,
    )

    class _DummyHistory:
        cache_ttl = 60

        def get_cache_view(self, *_args, **_kwargs):
            return SimpleNamespace(sessions=[], needs_refresh=False, blocked=False)

        def schedule_enrichment(self, *_args, **_kwargs):
            return None

        def sum_energy(self, sessions):
            return 0.0

    coord.session_history = _DummyHistory()

    result = await coord._async_update_data()

    entry = result["EV1"]
    assert entry["session_energy_raw"] is bad_energy
    assert entry["session_energy_unit"] is None
    assert entry["session_energy_wh"] is None
    assert entry["session_kwh"] is bad_energy


@pytest.mark.asyncio
async def test_async_update_data_preserves_known_charge_preference_when_status_only_has_custom_schedule(
    coordinator_factory,
):
    coord = coordinator_factory(
        serials=["EV1"],
        data={
            "EV1": {
                "sn": "EV1",
                "name": "Garage EV",
                "charge_mode_pref": "GREEN_CHARGING",
                "charge_mode": "GREEN_CHARGING",
            }
        },
    )
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_active = lambda: False  # type: ignore[assignment]  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "EV1",
                    "name": "Garage EV",
                    "connectors": [
                        {
                            "connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS,
                            "connectorStatusReason": "INSUFFICIENT_SOLAR",
                        }
                    ],
                    "sch_d": {"status": 1, "info": [{"type": "CUSTOM"}]},
                    "session_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=lambda: None,
    )
    coord.evse_timeseries.async_refresh = AsyncMock(return_value=None)  # noqa: SLF001
    coord.evse_timeseries.merge_charger_payloads = MagicMock(
        return_value=None
    )  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_dry_contact_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock()  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._get_charge_mode = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert result["EV1"]["charge_mode_pref"] is None
    assert result["EV1"]["charge_mode"] == "IDLE"


@pytest.mark.asyncio
async def test_async_update_data_uses_cached_charge_preference_when_status_only_has_custom_schedule(
    coordinator_factory,
):
    coord = coordinator_factory(
        serials=["EV1"],
        data={
            "EV1": {
                "sn": "EV1",
                "name": "Garage EV",
                "charge_mode": "IDLE",
            }
        },
    )
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_active = lambda: False  # type: ignore[assignment]  # noqa: SLF001
    coord._charge_mode_cache["EV1"] = (
        "GREEN_CHARGING",
        coord_mod.time.monotonic(),
    )
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "EV1",
                    "name": "Garage EV",
                    "connectors": [
                        {
                            "connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS,
                            "connectorStatusReason": "INSUFFICIENT_SOLAR",
                        }
                    ],
                    "sch_d": {"status": 1, "info": [{"type": "CUSTOM"}]},
                    "session_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=lambda: None,
    )
    coord.evse_timeseries.async_refresh = AsyncMock(return_value=None)  # noqa: SLF001
    coord.evse_timeseries.merge_charger_payloads = MagicMock(
        return_value=None
    )  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_dry_contact_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock()  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._get_charge_mode = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert result["EV1"]["charge_mode_pref"] == "GREEN_CHARGING"
    assert result["EV1"]["charge_mode"] == "GREEN_CHARGING"


@pytest.mark.asyncio
async def test_async_update_data_uses_status_mode_for_effective_charge_mode(
    coordinator_factory,
):
    coord = coordinator_factory(
        serials=["EV1"],
        data={
            "EV1": {
                "sn": "EV1",
                "name": "Garage EV",
                "charge_mode_pref": "GREEN_CHARGING",
                "charge_mode": "GREEN_CHARGING",
            }
        },
    )
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_active = lambda: False  # type: ignore[assignment]  # noqa: SLF001
    coord._charge_mode_cache["EV1"] = (
        "GREEN_CHARGING",
        coord_mod.time.monotonic(),
    )
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "EV1",
                    "name": "Garage EV",
                    "mode": 0,
                    "connectors": [
                        {
                            "connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS,
                            "connectorStatusReason": "INSUFFICIENT_SOLAR",
                        }
                    ],
                    "sch_d": {"status": 1, "info": [{"type": "greencharging"}]},
                    "session_d": {},
                    "charging": False,
                    "pluggedIn": True,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=lambda: None,
    )
    coord.evse_timeseries.async_refresh = AsyncMock(return_value=None)  # noqa: SLF001
    coord.evse_timeseries.merge_charger_payloads = MagicMock(
        return_value=None
    )  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_dry_contact_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock()  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._get_charge_mode = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert result["EV1"]["charge_mode_pref"] == "GREEN_CHARGING"
    assert result["EV1"]["charge_mode"] == "MANUAL_CHARGING"
    assert result["EV1"]["charge_mode_source"] == "explicit_status"


@pytest.mark.asyncio
async def test_async_update_data_drops_expired_cached_charge_preference_when_status_only_has_custom_schedule(
    coordinator_factory,
):
    coord = coordinator_factory(
        serials=["EV1"],
        data={
            "EV1": {
                "sn": "EV1",
                "name": "Garage EV",
                "charge_mode_pref": "GREEN_CHARGING",
                "charge_mode": "IDLE",
            }
        },
    )
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_active = lambda: False  # type: ignore[assignment]  # noqa: SLF001
    coord._charge_mode_cache["EV1"] = (
        "GREEN_CHARGING",
        coord_mod.time.monotonic() - CHARGE_MODE_CACHE_TTL - 1,
    )
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "EV1",
                    "name": "Garage EV",
                    "connectors": [
                        {
                            "connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS,
                            "connectorStatusReason": "INSUFFICIENT_SOLAR",
                        }
                    ],
                    "sch_d": {"status": 1, "info": [{"type": "CUSTOM"}]},
                    "session_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=lambda: None,
    )
    coord.evse_timeseries.async_refresh = AsyncMock(return_value=None)  # noqa: SLF001
    coord.evse_timeseries.merge_charger_payloads = MagicMock(
        return_value=None
    )  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_dry_contact_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock()  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._get_charge_mode = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert result["EV1"]["charge_mode_pref"] is None
    assert result["EV1"]["charge_mode"] == "IDLE"


@pytest.mark.asyncio
async def test_async_update_data_uses_battery_profile_charge_mode_when_scheduler_pref_missing(
    coordinator_factory,
):
    coord = coordinator_factory(
        serials=["EV1"],
        data={
            "EV1": {
                "sn": "EV1",
                "name": "Garage EV",
                "charge_mode": "IDLE",
            }
        },
    )
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_active = lambda: False  # type: ignore[assignment]  # noqa: SLF001
    coord._charge_mode_cache["EV1"] = (
        "GREEN_CHARGING",
        coord_mod.time.monotonic() - CHARGE_MODE_CACHE_TTL - 1,
    )
    coord._storm_guard_cache_until = coord_mod.time.monotonic() + 300  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "GREEN", "enable": True}
    ]
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "EV1",
                    "name": "Garage EV",
                    "connectors": [
                        {
                            "connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS,
                            "connectorStatusReason": "INSUFFICIENT_SOLAR",
                        }
                    ],
                    "sch_d": {"status": 1, "info": [{"type": "CUSTOM"}]},
                    "session_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=lambda: None,
    )
    coord.evse_timeseries.async_refresh = AsyncMock(return_value=None)  # noqa: SLF001
    coord.evse_timeseries.merge_charger_payloads = MagicMock(
        return_value=None
    )  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_dry_contact_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock()  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._get_charge_mode = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert result["EV1"]["charge_mode_pref"] == "GREEN_CHARGING"
    assert result["EV1"]["charge_mode"] == "GREEN_CHARGING"


@pytest.mark.asyncio
async def test_async_update_data_uses_green_schedule_type_when_scheduler_pref_missing(
    coordinator_factory,
):
    coord = coordinator_factory(
        serials=["EV1"],
        data={
            "EV1": {
                "sn": "EV1",
                "name": "Garage EV",
                "charge_mode": "IDLE",
            }
        },
    )
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._scheduler_available = False  # noqa: SLF001
    coord._scheduler_backoff_active = lambda: False  # type: ignore[assignment]  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "EV1",
                    "name": "Garage EV",
                    "connectors": [
                        {
                            "connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS,
                            "connectorStatusReason": "INSUFFICIENT_SOLAR",
                        }
                    ],
                    "sch_d": {"status": 1, "info": [{"type": "greencharging"}]},
                    "session_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=lambda: None,
    )
    coord.evse_timeseries.async_refresh = AsyncMock(return_value=None)  # noqa: SLF001
    coord.evse_timeseries.merge_charger_payloads = MagicMock(
        return_value=None
    )  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_guard_profile = AsyncMock()  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock()  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock()  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock()  # noqa: SLF001
    coord._async_refresh_dry_contact_settings = AsyncMock()  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock()  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock()  # noqa: SLF001
    coord._async_refresh_current_power_consumption = AsyncMock()  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock()  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord._get_charge_mode = AsyncMock(return_value=None)  # type: ignore[assignment]  # noqa: SLF001
    coord._sync_battery_profile_pending_issue = MagicMock()  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert result["EV1"]["schedule_type"] == "greencharging"
    assert result["EV1"]["charge_mode_pref"] == "GREEN_CHARGING"
    assert result["EV1"]["charge_mode"] == "GREEN_CHARGING"


@pytest.mark.asyncio
async def test_async_update_data_handles_numeric_ts(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["SN2"])
    sn = "SN2"
    coord.last_set_amps[sn] = 16
    status_payload = {
        "evChargerData": [
            {
                "sn": sn,
                "name": "Driveway",
                "charging": True,
                "pluggedIn": True,
                "faulted": False,
                "connectors": [
                    {"connectorStatusType": coord_mod.SUSPENDED_EVSE_STATUS}
                ],
                "sch_d": {"status": "enabled", "info": [{}]},
                "session_d": {"start_time": 1700000000, "plg_in_at": None},
            }
        ],
        "ts": 1_700_000_000_000,
    }
    coord.client.status = AsyncMock(return_value=status_payload)
    coord._async_resolve_charge_modes = AsyncMock(return_value={})

    class _MiniHistory:
        cache_ttl = 60

        def get_cache_view(self, *_):
            return SimpleNamespace(sessions=[], needs_refresh=False, blocked=False)

        async def async_enrich(self, *_args, **_kwargs):
            return {sn: []}

        def schedule_enrichment(self, *_args, **_kwargs):
            return None

        def sum_energy(self, *_args, **_kwargs):
            return 0.0

    coord.session_history = _MiniHistory()
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **kwargs: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=lambda: None,
    )

    result = await coord._async_update_data()
    assert "last_reported_at" in result[sn]


def test_determine_polling_state_handles_options(hass):
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.data = {"A": {"charging": False}}
    coord._fast_until = coord_mod.time.monotonic() + 5
    coord._streaming = True
    coord._streaming_until = coord_mod.time.monotonic() + 5
    coord.update_interval = timedelta(seconds=90)
    coord.config_entry = SimpleNamespace(
        options={
            OPT_FAST_POLL_INTERVAL: "not-a-number",
            coord_mod.OPT_SLOW_POLL_INTERVAL: "75",
            OPT_FAST_WHILE_STREAMING: False,
        }
    )

    state = coord._determine_polling_state({"A": {"charging": True}})
    assert state["want_fast"] is True
    assert state["fast"] == DEFAULT_FAST_POLL_INTERVAL
    assert state["slow"] == 75


def test_determine_polling_state_clamps_low_intervals(coordinator_factory):
    coord = coordinator_factory()
    coord.config_entry = SimpleNamespace(
        options={
            OPT_FAST_POLL_INTERVAL: 1,
            coord_mod.OPT_SLOW_POLL_INTERVAL: 1,
        }
    )

    state = coord._determine_polling_state({})

    assert state["fast"] == MIN_FAST_POLL_INTERVAL
    assert state["slow"] == MIN_SLOW_POLL_INTERVAL


@pytest.mark.asyncio
async def test_async_resolve_charge_modes_uses_cache_and_handles_errors(
    monkeypatch, coordinator_factory
):
    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord._charge_mode_cache = {
        "EV1": ("SCHEDULED_CHARGING", coord_mod.time.monotonic())
    }

    async def fake_get(sn: str):
        if sn == "EV2":
            return "GREEN_CHARGING"
        raise RuntimeError("boom")

    coord.evse_runtime.async_get_charge_mode = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_get
    )
    result = await coord.evse_runtime.async_resolve_charge_modes(["EV1", "EV2", "EV3"])
    assert result["EV1"] == ChargeModeResolution("SCHEDULED_CHARGING", "cache")
    assert result["EV2"] == ChargeModeResolution("GREEN_CHARGING", "scheduler_endpoint")
    assert result["EV3"] == ChargeModeResolution(None, "lookup_failed")


def test_amp_helpers_and_expectation_management(coordinator_factory, monkeypatch):
    coord = coordinator_factory(serials=["EV1"])
    coord.data["EV1"].update({"min_amp": "10", "max_amp": "40", "plugged": False})

    assert coord._coerce_amp(" 15 ") == 15
    assert coord._amp_limits("EV1") == (10, 40)
    assert coord._apply_amp_limits("EV1", 5) == 10
    coord.set_last_set_amps("EV1", 50)
    assert coord.last_set_amps["EV1"] == 40

    with pytest.raises(ServiceValidationError):
        coord.require_plugged("EV1")

    coord.serials = None
    coord._serial_order = None
    coord._devices_inventory_ready = False
    assert coord._ensure_serial_tracked("  EV2  ") is True
    assert "EV2" in coord.iter_serials()

    coord.set_desired_charging("EV1", True)
    assert coord.get_desired_charging("EV1") is True
    coord.set_desired_charging("EV1", None)
    assert coord.get_desired_charging("EV1") is None

    coord.set_charging_expectation("EV1", True, hold_for=0)
    coord.set_charging_expectation("EV1", True, hold_for=2)
    assert coord._pending_charging["EV1"][0] is True
    coord._pending_charging.clear()

    coord.config_entry = SimpleNamespace(
        options={coord_mod.OPT_SLOW_POLL_INTERVAL: "bad"}
    )
    assert coord._slow_interval_floor() >= DEFAULT_SLOW_POLL_INTERVAL

    coord.data["EV1"].update({"charging_level": "18"})
    assert coord.pick_start_amps("EV1", requested=None, fallback=30) == 40

    called = {"count": 0}

    def _cancel():
        called["count"] += 1
        raise RuntimeError("cancel fail")

    coord._backoff_cancel = _cancel
    coord._clear_backoff_timer()
    assert called["count"] == 1

    coord.hass.async_create_task = MagicMock(return_value=None)
    coord.async_request_refresh = MagicMock(return_value=None)
    coord._schedule_backoff_timer(0)

    coord._backoff_cancel = None
    coord._schedule_backoff_timer(1)


def test_charge_mode_preference_helpers(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    sn = "EV1"
    coord.data[sn]["charge_mode_pref"] = "MANUAL_CHARGING"
    prefs = coord._charge_mode_start_preferences(sn)
    assert prefs.include_level is True
    assert prefs.strict is False
    assert prefs.enforce_mode is None

    coord.data[sn]["charge_mode_pref"] = "SCHEDULED_CHARGING"
    prefs = coord._charge_mode_start_preferences(sn)
    assert prefs.include_level is True
    assert prefs.enforce_mode == "SCHEDULED_CHARGING"

    coord.data[sn]["charge_mode_pref"] = "GREEN_CHARGING"
    prefs = coord._charge_mode_start_preferences(sn)
    assert prefs.include_level is False
    assert prefs.strict is True

    coord.data[sn]["charge_mode_pref"] = None
    coord._charge_mode_cache[sn] = ("SMART", coord_mod.time.monotonic())
    assert coord._resolve_charge_mode_pref(sn) == "SMART_CHARGING"
    prefs = coord._charge_mode_start_preferences(sn)
    assert prefs.include_level is False
    assert prefs.strict is True

    coord._charge_mode_cache[sn] = ("SCHEDULED", coord_mod.time.monotonic())
    assert coord._resolve_charge_mode_pref(sn) == "SCHEDULED_CHARGING"

    coord._charge_mode_cache[sn] = (
        "GREEN_CHARGING",
        coord_mod.time.monotonic() - CHARGE_MODE_CACHE_TTL - 1,
    )
    coord.data[sn]["charge_mode"] = "IDLE"
    assert coord._resolve_charge_mode_pref(sn) is None

    coord.data[sn]["schedule_type"] = "greencharging"
    assert coord._resolve_charge_mode_pref(sn) == "GREEN_CHARGING"


def test_resolve_charge_mode_pref_handles_errors(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    sn = "EV1"

    class FaultyMapping:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    class BadStr:
        def __str__(self):
            raise ValueError("bad")

    coord.data = FaultyMapping()
    coord._charge_mode_cache[sn] = (BadStr(), coord_mod.time.monotonic())

    assert coord._resolve_charge_mode_pref(sn) is None


def test_charge_mode_normalizers_handle_invalid_values(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])

    class BadStr:
        def __str__(self):
            raise ValueError("bad")

    assert coord._normalize_effective_charge_mode(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_effective_charge_mode("   ") is None  # noqa: SLF001
    assert coord._normalize_effective_charge_mode("custom") is None  # noqa: SLF001
    assert coord._schedule_type_charge_mode_preference(BadStr()) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_ensure_charge_mode_updates_cache(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    sn = "EV1"
    coord.client.set_charge_mode = AsyncMock(return_value={"ok": True})
    await coord._ensure_charge_mode(sn, "SCHEDULED_CHARGING")
    coord.client.set_charge_mode.assert_awaited_once_with(sn, "SCHEDULED_CHARGING")
    assert coord._charge_mode_cache[sn][0] == "SCHEDULED_CHARGING"


@pytest.mark.asyncio
async def test_ensure_charge_mode_handles_errors(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord.client.set_charge_mode = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._ensure_charge_mode("EV1", "GREEN_CHARGING")


def test_set_charge_mode_cache_ignores_unknown_mode(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])

    coord.set_charge_mode_cache("EV1", "custom")

    assert "EV1" not in coord._charge_mode_cache


def test_has_embedded_charge_mode_detects_nested():
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    payload = {
        "sch_d": {"info": [{"status": "enabled"}]},
    }
    assert coord._has_embedded_charge_mode(payload) is True
    assert coord._has_embedded_charge_mode({"foo": "bar"}) is False


@pytest.mark.asyncio
async def test_attempt_auto_refresh_updates_tokens(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "pw"
    tokens = coord_mod.AuthTokens("cookie", "sess", "token", 123)
    monkeypatch.setattr(
        arr_mod, "async_authenticate", AsyncMock(return_value=(tokens, None))
    )
    coord.client.update_credentials = MagicMock()
    coord._persist_tokens = MagicMock()

    assert await coord._attempt_auto_refresh() is True
    coord.client.update_credentials.assert_called_once()
    coord._persist_tokens.assert_called_once_with(tokens)


def test_clear_current_power_consumption_delegates(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.current_power_runtime.clear = MagicMock()

    coord._clear_current_power_consumption()  # noqa: SLF001

    coord.current_power_runtime.clear.assert_called_once_with()


@pytest.mark.asyncio
async def test_async_run_auto_refresh_delegates(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.auth_refresh_runtime.async_run_auto_refresh = AsyncMock(return_value=True)

    assert await coord._async_run_auto_refresh() is True  # noqa: SLF001
    coord.auth_refresh_runtime.async_run_auto_refresh.assert_awaited_once_with()


def test_auth_refresh_recent_success_active_delegates(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.auth_refresh_runtime.auth_refresh_recent_success_active = MagicMock(
        return_value=True
    )

    assert coord._auth_refresh_recent_success_active() is True  # noqa: SLF001
    coord.auth_refresh_runtime.auth_refresh_recent_success_active.assert_called_once_with()


@pytest.mark.asyncio
async def test_clear_auth_refresh_task_delegates(coordinator_factory) -> None:
    coord = coordinator_factory()
    task = asyncio.create_task(asyncio.sleep(0))
    coord.auth_refresh_runtime.clear_auth_refresh_task = MagicMock()

    try:
        coord._clear_auth_refresh_task(task)  # noqa: SLF001
    finally:
        await task

    coord.auth_refresh_runtime.clear_auth_refresh_task.assert_called_once_with(task)


@pytest.mark.asyncio
async def test_get_charge_mode_uses_cache(coordinator_factory):
    coord = coordinator_factory(serials=["SN1"])
    coord._charge_mode_cache["SN1"] = ("GREEN_CHARGING", coord_mod.time.monotonic())
    assert await coord._get_charge_mode("SN1") == "GREEN_CHARGING"

    coord._charge_mode_cache.clear()
    coord.client.charge_mode = AsyncMock(return_value=None)
    assert await coord._get_charge_mode("SN1") is None


@pytest.mark.asyncio
async def test_get_green_battery_setting_parses_and_caches(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord._green_battery_cache.clear()
    coord.client.green_charging_settings = AsyncMock(
        return_value=[
            "invalid",
            {"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": "false"},
        ]
    )
    result = await coord._get_green_battery_setting("EV1")
    assert result == (False, True)
    result_cached = await coord._get_green_battery_setting("EV1")
    assert result_cached == (False, True)
    coord.client.green_charging_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_green_battery_setting_handles_missing_setting(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord._green_battery_cache.clear()
    coord.client.green_charging_settings = AsyncMock(
        return_value=[{"chargerSettingName": "OTHER_SETTING", "enabled": True}]
    )
    result = await coord._get_green_battery_setting("EV1")
    assert result == (None, False)


@pytest.mark.asyncio
async def test_async_resolve_green_battery_settings_uses_cached_fallback(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1", "EV2", "EV3", "EV4"])
    now = coord_mod.time.monotonic()
    expired = now - GREEN_BATTERY_CACHE_TTL - 1
    coord._green_battery_cache["EV1"] = (True, True, now)
    coord._green_battery_cache["EV2"] = (False, True, expired)
    coord._green_battery_cache["EV3"] = (None, False, expired)
    coord._green_battery_cache["EV4"] = (True, True, expired)
    coord.evse_runtime.async_get_green_battery_setting = AsyncMock(  # type: ignore[method-assign]
        side_effect=[RuntimeError("boom"), None, (True, True)]
    )

    result = await coord._async_resolve_green_battery_settings(
        ["EV1", "EV2", "EV3", "EV4", ""]
    )

    assert result == {
        "EV1": (True, True),
        "EV2": (False, True),
        "EV3": (None, False),
        "EV4": (True, True),
    }


@pytest.mark.asyncio
async def test_get_green_battery_setting_coercion_paths(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    cases = [
        (None, None),
        (True, True),
        (0, False),
        ("true", True),
        ("maybe", None),
    ]
    for enabled, expected in cases:
        coord._green_battery_cache.clear()
        coord.client.green_charging_settings = AsyncMock(
            return_value=[
                {"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": enabled}
            ]
        )
        result = await coord._get_green_battery_setting("EV1")
        assert result == (expected, True)


@pytest.mark.asyncio
async def test_get_auth_settings_parses_and_caches(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord._auth_settings_cache.clear()
    coord.client.charger_auth_settings = AsyncMock(
        return_value=[
            {"key": AUTH_APP_SETTING, "value": "enabled"},
            {"key": AUTH_RFID_SETTING, "value": "disabled"},
            "invalid",
        ]
    )
    result = await coord._get_auth_settings("EV1")
    assert result == (True, False, True, True)
    result_cached = await coord._get_auth_settings("EV1")
    assert result_cached == (True, False, True, True)
    coord.client.charger_auth_settings.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_auth_settings_handles_missing_setting(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord._auth_settings_cache.clear()
    coord.client.charger_auth_settings = AsyncMock(
        return_value=[{"key": "OTHER_SETTING", "value": "enabled"}]
    )
    assert await coord._get_auth_settings("EV1") is None


@pytest.mark.asyncio
async def test_get_auth_settings_coercion_paths(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    cases = [
        (None, None),
        (True, True),
        (0, False),
        ("true", True),
        ("maybe", None),
    ]
    for raw, expected in cases:
        coord._auth_settings_cache.clear()
        coord.client.charger_auth_settings = AsyncMock(
            return_value=[{"key": AUTH_APP_SETTING, "value": raw}]
        )
        result = await coord._get_auth_settings("EV1")
        assert result == (expected, None, True, False)


@pytest.mark.asyncio
async def test_get_auth_settings_treats_null_values_as_unknown_supported(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1"])
    coord._auth_settings_cache.clear()
    coord.client.charger_auth_settings = AsyncMock(
        return_value=[
            {"key": AUTH_APP_SETTING, "value": None, "reqValue": None},
            {"key": AUTH_RFID_SETTING, "value": None, "reqValue": None},
        ]
    )

    result = await coord._get_auth_settings("EV1")

    assert result == (None, None, True, True)


@pytest.mark.asyncio
async def test_async_resolve_auth_settings_uses_cached_fallback(coordinator_factory):
    coord = coordinator_factory(serials=["EV1", "EV2", "EV3", "EV4"])
    now = coord_mod.time.monotonic()
    expired = now - AUTH_SETTINGS_CACHE_TTL - 1
    coord._auth_settings_cache["EV1"] = (True, False, True, True, now)
    coord._auth_settings_cache["EV2"] = (False, None, True, False, expired)
    coord._auth_settings_cache["EV3"] = (None, None, False, False, expired)
    coord._auth_settings_cache["EV4"] = (True, True, True, True, expired)
    coord.evse_runtime.async_get_auth_settings = AsyncMock(  # type: ignore[method-assign]
        side_effect=[RuntimeError("boom"), None, (False, True, True, True)]
    )

    result = await coord._async_resolve_auth_settings(["EV1", "EV2", "EV3", "EV4", ""])

    assert result == {
        "EV1": (True, False, True, True),
        "EV2": (False, None, True, False),
        "EV3": (None, None, False, False),
        "EV4": (False, True, True, True),
    }


@pytest.mark.asyncio
async def test_async_update_data_includes_auth_settings(coordinator_factory):
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._has_successful_refresh = True  # noqa: SLF001
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "chargeMode": "IDLE",
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.charger_auth_settings = AsyncMock(
        return_value=[
            {"key": AUTH_APP_SETTING, "value": "enabled"},
            {"key": AUTH_RFID_SETTING, "value": "disabled"},
        ]
    )
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.energy._async_refresh_site_energy = AsyncMock()
    await coord._async_refresh_evse_feature_flags(force=True)

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["app_auth_supported"] is True
    assert result[SERIAL_ONE]["rfid_auth_supported"] is True
    assert result[SERIAL_ONE]["app_auth_enabled"] is True
    assert result[SERIAL_ONE]["rfid_auth_enabled"] is False
    assert result[SERIAL_ONE]["auth_required"] is True


@pytest.mark.asyncio
async def test_async_update_data_keeps_null_auth_settings_unknown(coordinator_factory):
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._has_successful_refresh = True  # noqa: SLF001
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "chargeMode": "IDLE",
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.charger_auth_settings = AsyncMock(
        return_value=[
            {"key": AUTH_APP_SETTING, "value": None, "reqValue": None},
            {"key": AUTH_RFID_SETTING, "value": None, "reqValue": None},
        ]
    )
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.energy._async_refresh_site_energy = AsyncMock()
    await coord._async_refresh_evse_feature_flags(force=True)

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["app_auth_supported"] is True
    assert result[SERIAL_ONE]["rfid_auth_supported"] is True
    assert result[SERIAL_ONE]["app_auth_enabled"] is None
    assert result[SERIAL_ONE]["rfid_auth_enabled"] is None
    assert result[SERIAL_ONE]["auth_required"] is None


@pytest.mark.asyncio
async def test_async_update_data_uses_feature_flags_as_advisory_fallbacks(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._has_successful_refresh = True  # noqa: SLF001
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.evse_feature_flags = AsyncMock(
        return_value={
            "meta": {"serverTimeStamp": "2026-03-08T09:40:02.917+00:00"},
            "data": {
                "evse_charging_mode": False,
                SERIAL_ONE: {
                    "evse_authentication": False,
                    "iqevse_rfid": False,
                    "max_current_config_support": False,
                    "evse_storm_guard": False,
                },
            },
            "error": {},
        }
    )
    coord.client.charger_auth_settings = AsyncMock(
        return_value=[
            {"key": AUTH_APP_SETTING, "value": "enabled"},
            {"key": AUTH_RFID_SETTING, "value": "disabled"},
        ]
    )
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.energy._async_refresh_site_energy = AsyncMock()
    await coord._async_refresh_evse_feature_flags(force=True)

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["charge_mode_supported"] is False
    assert result[SERIAL_ONE]["charge_mode_supported_source"] == "feature_flag"
    assert result[SERIAL_ONE]["charging_amps_supported"] is False
    assert result[SERIAL_ONE]["charging_amps_supported_source"] == "feature_flag"
    assert result[SERIAL_ONE]["storm_guard_supported"] is False
    assert result[SERIAL_ONE]["storm_guard_supported_source"] == "feature_flag"
    assert result[SERIAL_ONE]["auth_feature_supported"] is True
    assert result[SERIAL_ONE]["auth_feature_supported_source"] == "runtime"
    assert result[SERIAL_ONE]["rfid_feature_supported"] is True
    assert result[SERIAL_ONE]["rfid_feature_supported_source"] == "runtime"
    coord.client.charger_auth_settings.assert_awaited_once()
    assert coord.evse_diagnostics_payloads()["site_feature_flags"] == {
        "evse_charging_mode": False
    }


@pytest.mark.asyncio
async def test_async_update_data_treats_embedded_charge_mode_as_runtime_support(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "chargeMode": "IDLE",
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.evse_feature_flags = AsyncMock(
        return_value={
            "meta": {"serverTimeStamp": "2026-03-08T09:40:02.917+00:00"},
            "data": {"evse_charging_mode": False},
            "error": {},
        }
    )
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["charge_mode_supported"] is True
    assert result[SERIAL_ONE]["charge_mode_supported_source"] == "runtime"


@pytest.mark.asyncio
async def test_async_update_data_merges_charger_config_fallback_fields(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._has_successful_refresh = True  # noqa: SLF001
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.charger_config = AsyncMock(
        return_value=[
            {"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": None},
            {"key": PHASE_SWITCH_CONFIG_SETTING, "value": "auto"},
        ]
    )
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert "default_charge_level" in result[SERIAL_ONE]
    assert result[SERIAL_ONE]["default_charge_level"] is None
    assert result[SERIAL_ONE]["default_charge_level_supported"] is True
    assert result[SERIAL_ONE]["default_charge_level_supported_source"] == (
        "charger_config"
    )
    assert result[SERIAL_ONE]["phase_switch_config"] == "auto"
    coord.client.charger_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_update_data_refetches_summary_when_amp_bounds_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._has_successful_refresh = True  # noqa: SLF001
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "chargeMode": "MANUAL",
                "session_d": {"chargeLevel": "30", "e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.charger_config = AsyncMock(return_value=[])
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(
        side_effect=[
            [{"serialNumber": SERIAL_ONE, "maxCurrent": 30}],
            [
                {
                    "serialNumber": SERIAL_ONE,
                    "maxCurrent": 48,
                    "chargeLevelDetails": {
                        "min": "6",
                        "max": "40",
                        "granularity": "1",
                    },
                }
            ],
            [
                {
                    "serialNumber": SERIAL_ONE,
                    "maxCurrent": 48,
                    "chargeLevelDetails": {
                        "min": "6",
                        "max": "40",
                        "granularity": "1",
                    },
                }
            ],
        ]
    )
    coord.energy._async_refresh_site_energy = AsyncMock()

    first = await coord._async_update_data()
    second = await coord._async_update_data()
    coord.data = second
    third = await coord._async_update_data()

    assert first[SERIAL_ONE]["min_amp"] is None
    assert first[SERIAL_ONE]["max_amp"] is None
    assert second[SERIAL_ONE]["min_amp"] == 6
    assert second[SERIAL_ONE]["max_amp"] == 40
    assert third[SERIAL_ONE]["min_amp"] == 6
    assert third[SERIAL_ONE]["max_amp"] == 40
    assert [
        call.kwargs["force"] for call in coord.summary.async_fetch.await_args_list
    ] == [True, True, False]


def test_summary_refresh_needed_for_amp_bounds_skips_invalid_serial_snapshots(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord.iter_serials = lambda: ["", "NOT-A-DICT", SERIAL_ONE]  # type: ignore[method-assign]

    assert (
        coord._summary_refresh_needed_for_amp_bounds(  # noqa: SLF001
            {
                "NOT-A-DICT": "bad",
                SERIAL_ONE: {"charging_amps_supported": True},
            }
        )
        is True
    )


@pytest.mark.asyncio
async def test_async_update_data_preserves_known_amp_bounds_when_summary_omits_them(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(
        serials=[SERIAL_ONE],
        data={
            SERIAL_ONE: {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "min_amp": "6",
                "max_amp": "40",
                "amp_granularity": "1",
                "max_current": "48",
            }
        },
    )
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": SERIAL_ONE,
                    "name": "Garage",
                    "connectors": [{}],
                    "pluggedIn": True,
                    "charging": False,
                    "faulted": False,
                    "chargeMode": "MANUAL",
                    "session_d": {"chargeLevel": "30", "e_c": 0},
                }
            ],
            "ts": 0,
        }
    )
    coord.client.charger_config = AsyncMock(return_value=[])
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(
        return_value=[{"serialNumber": SERIAL_ONE, "chargeLevelDetails": "bad"}]
    )
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["min_amp"] == 6
    assert result[SERIAL_ONE]["max_amp"] == 40
    assert result[SERIAL_ONE]["amp_granularity"] == 1
    assert result[SERIAL_ONE]["max_current"] == "48"


@pytest.mark.asyncio
async def test_async_update_data_keeps_default_charge_level_unsupported_when_omitted(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._has_successful_refresh = True  # noqa: SLF001
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.charger_config = AsyncMock(
        return_value=[{"key": PHASE_SWITCH_CONFIG_SETTING, "value": "auto"}]
    )
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(
        return_value=[
            {
                "serialNumber": SERIAL_ONE,
                "chargeLevelDetails": {"defaultChargeLevel": "30"},
            }
        ]
    )
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["default_charge_level"] == "30"
    assert result[SERIAL_ONE]["default_charge_level_supported"] is False
    assert result[SERIAL_ONE]["default_charge_level_supported_source"] == (
        "charger_config"
    )
    assert result[SERIAL_ONE]["phase_switch_config"] == "auto"


@pytest.mark.asyncio
async def test_async_update_data_keeps_default_charge_level_unsupported_when_empty(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._has_successful_refresh = True  # noqa: SLF001
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.charger_config = AsyncMock(return_value=[])
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(
        return_value=[
            {
                "serialNumber": SERIAL_ONE,
                "chargeLevelDetails": {"defaultChargeLevel": "30"},
            }
        ]
    )
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["default_charge_level"] == "30"
    assert result[SERIAL_ONE]["default_charge_level_supported"] is False
    assert result[SERIAL_ONE]["default_charge_level_supported_source"] == (
        "charger_config"
    )


@pytest.mark.asyncio
async def test_async_resolve_charger_config_honors_backoff(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._charger_config_backoff_until[SERIAL_ONE] = time.monotonic() + 60
    coord.client.charger_config = AsyncMock()

    result = await coord.evse_runtime.async_resolve_charger_config(
        [SERIAL_ONE],
        keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
    )

    assert result == {}
    coord.client.charger_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_get_charger_config_covers_normalization_and_reqvalue(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])

    class _BadKey:
        def __str__(self) -> str:
            raise ValueError("boom")

    class _BadResponseKey:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord.client.charger_config = AsyncMock(
        return_value=[
            "invalid",
            {"key": _BadResponseKey(), "value": "ignored"},
            {"key": "unrequested", "value": "ignored"},
            {"key": DEFAULT_CHARGE_LEVEL_SETTING, "reqValue": "disabled"},
            {"key": PHASE_SWITCH_CONFIG_SETTING, "value": "auto"},
        ]
    )

    result = await coord.evse_runtime.async_get_charger_config(
        SERIAL_ONE,
        keys=(
            "",
            DEFAULT_CHARGE_LEVEL_SETTING,
            DEFAULT_CHARGE_LEVEL_SETTING,
            _BadKey(),
            PHASE_SWITCH_CONFIG_SETTING,
        ),
    )

    assert result == {
        DEFAULT_CHARGE_LEVEL_SETTING: "disabled",
        PHASE_SWITCH_CONFIG_SETTING: "auto",
    }
    coord.client.charger_config.assert_awaited_once_with(
        SERIAL_ONE,
        [DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING],
    )


@pytest.mark.asyncio
async def test_async_get_charger_config_returns_empty_with_no_valid_keys(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])

    class _BadKey:
        def __str__(self) -> str:
            raise ValueError("boom")

    result = await coord.evse_runtime.async_get_charger_config(
        SERIAL_ONE,
        keys=("", _BadKey()),
    )

    assert result == {}


@pytest.mark.asyncio
async def test_async_set_default_charge_level_caches_accepted_value(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord.client.set_default_charge_level = AsyncMock(
        return_value={
            "data": [{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": "30", "status": 2}]
        }
    )
    coord.async_request_refresh = AsyncMock()

    await coord.evse_runtime.async_set_default_charge_level(SERIAL_ONE, 24)

    coord.client.set_default_charge_level.assert_awaited_once_with(SERIAL_ONE, 24)
    assert coord._charger_config_cache[SERIAL_ONE][0][DEFAULT_CHARGE_LEVEL_SETTING] == (
        30
    )
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_set_default_charge_level_uses_reqvalue_and_ignores_noise(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord.client.set_default_charge_level = AsyncMock(
        return_value={
            "data": [
                "invalid",
                {"key": "Other", "value": "99"},
                {"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": None, "reqValue": "32"},
            ]
        }
    )
    coord.async_request_refresh = AsyncMock()

    await coord.evse_runtime.async_set_default_charge_level(SERIAL_ONE, 24)

    assert coord._charger_config_cache[SERIAL_ONE][0][DEFAULT_CHARGE_LEVEL_SETTING] == (
        32
    )


@pytest.mark.asyncio
async def test_async_set_default_charge_level_keeps_requested_value_for_bad_response(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord.client.set_default_charge_level = AsyncMock(
        return_value={"data": [{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": "bad"}]}
    )
    coord.async_request_refresh = AsyncMock()

    await coord.evse_runtime.async_set_default_charge_level(SERIAL_ONE, 24)

    assert coord._charger_config_cache[SERIAL_ONE][0][DEFAULT_CHARGE_LEVEL_SETTING] == (
        24
    )
    assert coord.evse_runtime._coerce_charge_level(None) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_set_default_charge_level_wraps_unavailable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord.client.set_default_charge_level = AsyncMock(
        side_effect=ChargerConfigUnavailable("down")
    )

    with pytest.raises(ServiceValidationError):
        await coord.evse_runtime.async_set_default_charge_level(SERIAL_ONE, 24)


@pytest.mark.asyncio
async def test_async_get_charger_config_returns_full_fresh_cache(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._charger_config_cache[SERIAL_ONE] = (
        {
            DEFAULT_CHARGE_LEVEL_SETTING: "disabled",
            PHASE_SWITCH_CONFIG_SETTING: "auto",
        },
        time.monotonic(),
    )
    coord.client.charger_config = AsyncMock()

    result = await coord.evse_runtime.async_get_charger_config(
        SERIAL_ONE,
        keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
    )

    assert result == {
        DEFAULT_CHARGE_LEVEL_SETTING: "disabled",
        PHASE_SWITCH_CONFIG_SETTING: "auto",
    }
    coord.client.charger_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_get_charger_config_caches_missing_requested_key(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord.client.charger_config = AsyncMock(
        return_value=[{"key": PHASE_SWITCH_CONFIG_SETTING, "value": "auto"}]
    )

    result = await coord.evse_runtime.async_get_charger_config(
        SERIAL_ONE,
        keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
    )

    assert result == {PHASE_SWITCH_CONFIG_SETTING: "auto"}
    coord.client.charger_config.assert_awaited_once()

    coord.client.charger_config.reset_mock()
    result = await coord.evse_runtime.async_get_charger_config(
        SERIAL_ONE,
        keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
    )

    assert result == {PHASE_SWITCH_CONFIG_SETTING: "auto"}
    coord.client.charger_config.assert_not_awaited()
    assert (
        coord.evse_runtime.charger_config_lookup_candidates(
            [SERIAL_ONE],
            keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
        )
        == []
    )


@pytest.mark.asyncio
async def test_async_get_charger_config_uses_cache_for_backoff_and_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._charger_config_cache[SERIAL_ONE] = (
        {DEFAULT_CHARGE_LEVEL_SETTING: "disabled"},
        time.monotonic(),
    )
    coord._charger_config_backoff_until[SERIAL_ONE] = time.monotonic() + 60
    coord.client.charger_config = AsyncMock()

    result = await coord.evse_runtime.async_get_charger_config(
        SERIAL_ONE,
        keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
    )

    assert result == {DEFAULT_CHARGE_LEVEL_SETTING: "disabled"}
    coord.client.charger_config.assert_not_awaited()

    coord._charger_config_backoff_until.clear()
    coord.client.charger_config = AsyncMock(side_effect=RuntimeError("boom"))

    result = await coord.evse_runtime.async_get_charger_config(
        SERIAL_ONE,
        keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
    )

    assert result == {DEFAULT_CHARGE_LEVEL_SETTING: "disabled"}
    assert coord._charger_config_backoff_until[SERIAL_ONE] > time.monotonic()


@pytest.mark.asyncio
async def test_async_resolve_charger_config_uses_cached_and_exception_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._charger_config_cache[SERIAL_ONE] = (
        {DEFAULT_CHARGE_LEVEL_SETTING: "disabled"},
        time.monotonic(),
    )

    async def _boom(sn: str, *, keys) -> dict[str, object] | None:
        raise RuntimeError(f"boom:{sn}")

    coord.evse_runtime.async_get_charger_config = _boom  # type: ignore[assignment]

    class _BadKey:
        def __str__(self) -> str:
            raise ValueError("boom")

    result = await coord.evse_runtime.async_resolve_charger_config(
        ["", SERIAL_ONE],
        keys=(
            DEFAULT_CHARGE_LEVEL_SETTING,
            DEFAULT_CHARGE_LEVEL_SETTING,
            "",
            _BadKey(),
            PHASE_SWITCH_CONFIG_SETTING,
        ),
    )

    assert result == {SERIAL_ONE: {DEFAULT_CHARGE_LEVEL_SETTING: "disabled"}}


@pytest.mark.asyncio
async def test_async_resolve_charger_config_returns_empty_with_no_valid_keys(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])

    class _BadKey:
        def __str__(self) -> str:
            raise ValueError("boom")

    result = await coord.evse_runtime.async_resolve_charger_config(
        [SERIAL_ONE],
        keys=("", _BadKey()),
    )

    assert result == {}


@pytest.mark.asyncio
async def test_async_resolve_charger_config_uses_full_fresh_cache(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._charger_config_cache[SERIAL_ONE] = (
        {
            DEFAULT_CHARGE_LEVEL_SETTING: "disabled",
            PHASE_SWITCH_CONFIG_SETTING: "auto",
        },
        time.monotonic(),
    )
    coord.client.charger_config = AsyncMock()

    result = await coord.evse_runtime.async_resolve_charger_config(
        [SERIAL_ONE],
        keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
    )

    assert result == {
        SERIAL_ONE: {
            DEFAULT_CHARGE_LEVEL_SETTING: "disabled",
            PHASE_SWITCH_CONFIG_SETTING: "auto",
        }
    }
    coord.client.charger_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_update_data_drops_expired_charger_config_after_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(
        serials=[SERIAL_ONE],
        data={
            SERIAL_ONE: {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "default_charge_level": "disabled",
                "phase_switch_config": "auto",
            }
        },
    )
    coord._has_successful_refresh = True  # noqa: SLF001
    coord._charger_config_cache[SERIAL_ONE] = (
        {
            DEFAULT_CHARGE_LEVEL_SETTING: "disabled",
            PHASE_SWITCH_CONFIG_SETTING: "auto",
        },
        time.monotonic() - CHARGER_CONFIG_CACHE_TTL - 1,
    )
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.charger_config = AsyncMock(side_effect=RuntimeError("boom"))
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert "default_charge_level" not in result[SERIAL_ONE]
    assert "phase_switch_config" not in result[SERIAL_ONE]
    assert coord._charger_config_backoff_until[SERIAL_ONE] > time.monotonic()


@pytest.mark.asyncio
async def test_async_update_data_ignores_feature_flag_refresh_failures(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": SERIAL_ONE,
                    "name": "Garage",
                    "connectors": [{}],
                    "session_d": {"e_c": 0},
                }
            ],
            "ts": 0,
        }
    )
    coord._async_refresh_evse_feature_flags = AsyncMock(
        side_effect=RuntimeError("boom")
    )  # noqa: SLF001
    coord._async_resolve_green_battery_settings = AsyncMock(
        return_value={}
    )  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock(return_value={})  # noqa: SLF001
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert SERIAL_ONE in result


def test_set_app_auth_cache_updates_existing(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    now = coord_mod.time.monotonic()
    coord._auth_settings_cache["EV1"] = (False, True, False, True, now)
    coord.set_app_auth_cache("EV1", True)

    updated = coord._auth_settings_cache["EV1"]
    assert updated[0] is True
    assert updated[1] is True
    assert updated[2] is True
    assert updated[3] is True


@pytest.mark.asyncio
async def test_async_update_data_includes_green_battery_settings(
    coordinator_factory,
):
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._has_successful_refresh = True  # noqa: SLF001
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "chargeMode": "IDLE",
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.green_charging_settings = AsyncMock(
        return_value=[{"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": True}]
    )
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["green_battery_supported"] is True
    assert result[SERIAL_ONE]["green_battery_enabled"] is True


@pytest.mark.asyncio
async def test_async_update_data_includes_storm_guard(
    coordinator_factory,
):
    coord = coordinator_factory(serials=[SERIAL_ONE])
    coord._has_successful_refresh = True  # noqa: SLF001
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "chargeMode": "IDLE",
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.client.storm_guard_profile = AsyncMock(
        return_value={"data": {"stormGuardState": "enabled", "evseStormEnabled": True}}
    )
    coord.client.storm_guard_alert = AsyncMock(
        return_value={"criticalAlertActive": True, "stormAlerts": []}
    )
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["storm_guard_state"] == "enabled"
    assert result[SERIAL_ONE]["storm_evse_enabled"] is True
    assert coord.storm_alert_active is True


@pytest.mark.asyncio
async def test_async_update_data_summary_supports_use_battery_overrides(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[SERIAL_ONE])
    payload = {
        "evChargerData": [
            {
                "sn": SERIAL_ONE,
                "name": "Garage",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "chargeMode": "IDLE",
                "session_d": {"e_c": 0},
            }
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.green_charging_settings = AsyncMock(
        return_value=[{"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": True}]
    )
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(
        return_value=[{"serialNumber": SERIAL_ONE, "supportsUseBattery": False}]
    )
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert result[SERIAL_ONE]["green_battery_supported"] is False
    assert "green_battery_enabled" not in result[SERIAL_ONE]


@pytest.mark.asyncio
async def test_async_update_data_summary_supports_use_battery_coercions(
    coordinator_factory,
) -> None:
    serials = ["EV1", "EV2", "EV3", "EV4"]
    coord = coordinator_factory(serials=serials)
    coord._has_successful_refresh = True  # noqa: SLF001
    payload = {
        "evChargerData": [
            {
                "sn": serial,
                "name": f"Charger {serial}",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "chargeMode": "IDLE",
                "session_d": {"e_c": 0},
            }
            for serial in serials
        ],
        "ts": 0,
    }
    coord.client.status = AsyncMock(return_value=payload)
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(
        return_value=[
            {"serialNumber": "EV1", "supportsUseBattery": 1},
            {"serialNumber": "EV2", "supportsUseBattery": "true"},
            {"serialNumber": "EV3", "supportsUseBattery": "false"},
            {"serialNumber": "EV4", "supportsUseBattery": "maybe"},
        ]
    )
    coord.energy._async_refresh_site_energy = AsyncMock()

    result = await coord._async_update_data()

    assert result["EV1"]["green_battery_supported"] is True
    assert result["EV2"]["green_battery_supported"] is True
    assert result["EV3"]["green_battery_supported"] is False
    assert result["EV4"]["green_battery_supported"] is False


@pytest.mark.asyncio
async def test_handle_client_unauthorized_paths(
    coordinator_factory, mock_issue_registry
):
    coord = coordinator_factory()

    async def _auto_refresh_success() -> bool:
        return True

    coord._attempt_auto_refresh = _auto_refresh_success  # type: ignore[assignment]
    assert await coord._handle_client_unauthorized() is True

    async def _auto_refresh_failure() -> bool:
        return False

    coord._attempt_auto_refresh = _auto_refresh_failure  # type: ignore[assignment]
    coord._unauth_errors = 1
    with pytest.raises(coord_mod.ConfigEntryAuthFailed):
        await coord._handle_client_unauthorized()
    assert not any(
        issue[1] == "reauth_required" for issue in mock_issue_registry.created
    )
    assert any(issue[1] == "reauth_required" for issue in mock_issue_registry.deleted)


def test_create_reauth_issue_clears_auth_block_repairs(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    coord._auth_block_issue_reported = True
    coord.diagnostics.create_reauth_issue()

    deleted_issue_ids = {issue_id for _domain, issue_id in mock_issue_registry.deleted}
    assert ISSUE_AUTH_BLOCKED in deleted_issue_ids
    assert ISSUE_TOO_MANY_ACTIVE_SESSIONS in deleted_issue_ids

    domain, issue_id, payload = mock_issue_registry.created[-1]
    assert domain == coord_mod.DOMAIN
    assert issue_id == ISSUE_REAUTH_REQUIRED
    assert payload["severity"] is coord_diag_mod.ir.IssueSeverity.ERROR
    assert payload["translation_key"] == ISSUE_REAUTH_REQUIRED
    assert "site_metrics" in payload["data"]


def test_persist_tokens_updates_entry(coordinator_factory, config_entry):
    coord = coordinator_factory()
    coord.config_entry = config_entry
    object.__setattr__(
        config_entry,
        "runtime_data",
        SimpleNamespace(reload_suppression_count=0),
    )

    def _fake_update_entry(entry, *, data=None, options=None):
        changed = False
        if data is not None:
            changed = entry.data != data
            object.__setattr__(entry, "data", MappingProxyType(dict(data)))
        if options is not None:
            changed = changed or entry.options != options
            object.__setattr__(entry, "options", MappingProxyType(dict(options)))
        return changed

    coord.hass.config_entries.async_update_entry = _fake_update_entry  # type: ignore[assignment]

    tokens = coord_mod.AuthTokens(
        cookie="c", session_id="s", access_token="t", token_expires_at=123
    )
    coord._persist_tokens(tokens)
    assert config_entry.data[coord_mod.CONF_COOKIE] == "c"
    assert config_entry.data[coord_mod.CONF_ACCESS_TOKEN] == "t"
    assert config_entry.runtime_data.reload_suppression_count == 1


def test_persist_tokens_does_not_mark_reload_skip_for_noop_update(
    coordinator_factory, config_entry
):
    coord = coordinator_factory()
    coord.config_entry = config_entry
    object.__setattr__(
        config_entry,
        "runtime_data",
        SimpleNamespace(reload_suppression_count=0),
    )

    coord.hass.config_entries.async_update_entry = MagicMock(return_value=False)

    tokens = coord_mod.AuthTokens(
        cookie="c", session_id="s", access_token="t", token_expires_at=123
    )
    coord._persist_tokens(tokens)

    assert config_entry.runtime_data.reload_suppression_count == 0


def test_hems_auth_circuit_is_coordinator_backed(
    coordinator_factory,
    mock_issue_registry,
):
    coord = coordinator_factory()
    coord._selected_type_keys = {"heatpump"}  # noqa: SLF001

    assert coord._hems_auth_circuit_active() is False  # noqa: SLF001
    assert coord._note_hems_auth_failure(  # noqa: SLF001
        coord_mod.Unauthorized(),
        endpoint="hems_devices",
    )

    assert coord._hems_auth_circuit_active() is True  # noqa: SLF001
    metrics = coord.collect_site_metrics()
    assert metrics["hems_auth_circuit_active"] is True
    assert metrics["hems_auth_backoff_remaining_s"] is not None
    assert metrics["hems_auth_failure_count"] == 1
    assert metrics["hems_auth_last_endpoint"] == "hems_devices"
    assert metrics["hems_auth_last_reason"] == "unauthorized"
    assert "hems_auth" in metrics["degraded_services"]
    first_issue = mock_issue_registry.created[-1]
    assert first_issue[1] == "hems_auth_degraded"
    assert first_issue[2]["translation_placeholders"]["failure_count"] == "1"

    assert coord._note_hems_auth_failure(  # noqa: SLF001
        coord_mod.Unauthorized(),
        endpoint="hems_devices",
    )
    second_issue = mock_issue_registry.created[-1]
    assert second_issue[1] == "hems_auth_degraded"
    assert second_issue[2]["translation_placeholders"]["failure_count"] == "2"

    coord._hems_auth_backoff_until = time.monotonic() - 1  # noqa: SLF001
    assert coord._hems_auth_circuit_active() is False  # noqa: SLF001
    assert coord._hems_auth_backoff_until is None  # noqa: SLF001
    assert coord._hems_auth_last_reason == "unauthorized"  # noqa: SLF001
    assert coord._hems_auth_failure_count == 2  # noqa: SLF001


def test_hems_auth_circuit_suppresses_repair_without_heatpump_context(
    coordinator_factory,
    mock_issue_registry,
):
    coord = coordinator_factory()

    assert coord._note_hems_auth_failure(  # noqa: SLF001
        coord_mod.Unauthorized(),
        endpoint="hems_devices",
    )

    assert coord._hems_auth_circuit_active() is True  # noqa: SLF001
    assert coord._hems_auth_failure_count == 1  # noqa: SLF001
    assert not mock_issue_registry.created


def test_hems_auth_repair_context_respects_disabled_heatpump_group(
    coordinator_factory,
    mock_issue_registry,
):
    coord = coordinator_factory()
    coord._selected_type_keys = {"envoy", "encharge"}  # noqa: SLF001
    coord.inventory_view.has_type = MagicMock(return_value=True)
    coord._heatpump_known_present = True  # noqa: SLF001

    assert coord._hems_auth_repair_context_available() is False  # noqa: SLF001
    assert coord._note_hems_auth_failure(  # noqa: SLF001
        coord_mod.Unauthorized(),
        endpoint="hems_devices",
    )
    assert not mock_issue_registry.created


def test_hems_auth_repair_context_handles_defensive_fallbacks(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord.inventory_view.has_type = MagicMock(side_effect=RuntimeError("bad"))
    coord._heatpump_known_present = True  # noqa: SLF001

    assert coord._hems_auth_repair_context_available() is True  # noqa: SLF001

    coord = coordinator_factory()
    coord.inventory_view.has_type = MagicMock(side_effect=RuntimeError("bad"))
    coord.heatpump_runtime.heatpump_entities_established = MagicMock(
        side_effect=RuntimeError("bad")
    )

    assert coord._hems_auth_repair_context_available() is False  # noqa: SLF001

    coord = coordinator_factory()
    coord.heatpump_runtime.heatpump_entities_established = None

    assert coord._hems_auth_repair_context_available() is False  # noqa: SLF001


def test_hems_auth_circuit_restores_and_persists_config_entry_state(
    coordinator_factory,
    config_entry,
):
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    config = {
        coord_mod.CONF_SITE_ID: "9633674",
        coord_mod.CONF_SERIALS: ["111122223333"],
        coord_mod.CONF_EAUTH: "EAUTH",
        coord_mod.CONF_COOKIE: "COOKIE",
        coord_mod.CONF_SCAN_INTERVAL: 15,
        coord_mod.CONF_SITE_ONLY: False,
        coord_mod.CONF_HEMS_AUTH_BACKOFF_UNTIL: future.isoformat(),
        coord_mod.CONF_HEMS_AUTH_FAILURE_COUNT: "bad",
        coord_mod.CONF_HEMS_AUTH_LAST_STATUS: "bad",
        coord_mod.CONF_HEMS_AUTH_LAST_ENDPOINT: " hems_devices ",
        coord_mod.CONF_HEMS_AUTH_LAST_REASON: " unauthorized ",
    }
    coord = coordinator_factory(config=config)

    assert coord._hems_auth_failure_count == 0  # noqa: SLF001
    assert coord._hems_auth_last_status is None  # noqa: SLF001
    assert coord._hems_auth_backoff_until is not None  # noqa: SLF001

    coord.config_entry = config_entry
    object.__setattr__(
        config_entry,
        "runtime_data",
        SimpleNamespace(reload_suppression_count=0),
    )

    def _update_entry(entry, *, data=None, options=None):
        assert entry is config_entry
        object.__setattr__(entry, "data", MappingProxyType(dict(data or {})))
        return True

    coord.hass.config_entries.async_update_entry = _update_entry  # type: ignore[assignment]
    coord._hems_auth_failure_count = 2  # noqa: SLF001
    coord._hems_auth_last_failure_utc = future  # noqa: SLF001
    coord._hems_auth_last_success_utc = future  # noqa: SLF001
    coord._hems_auth_last_endpoint = "hems_devices"  # noqa: SLF001
    coord._hems_auth_last_status = 401  # noqa: SLF001
    coord._hems_auth_last_reason = "unauthorized"  # noqa: SLF001

    coord._persist_hems_auth_circuit_state()  # noqa: SLF001

    assert (
        config_entry.data[coord_mod.CONF_HEMS_AUTH_BACKOFF_UNTIL] == future.isoformat()
    )
    assert config_entry.data[coord_mod.CONF_HEMS_AUTH_FAILURE_COUNT] == 2
    assert config_entry.data[coord_mod.CONF_HEMS_AUTH_LAST_STATUS] == 401

    coord._clear_hems_auth_circuit(persist=True)  # noqa: SLF001

    assert coord_mod.CONF_HEMS_AUTH_BACKOFF_UNTIL not in config_entry.data
    assert coord_mod.CONF_HEMS_AUTH_FAILURE_COUNT not in config_entry.data


def test_internal_config_entry_update_guard_edges(coordinator_factory, config_entry):
    coord = coordinator_factory()

    assert (
        EnphaseCoordinator.__new__(
            EnphaseCoordinator
        )._async_update_config_entry_data_internal({}, reason="missing_entry")
        is False
    )

    coord.config_entry = config_entry
    assert (
        coord._async_update_config_entry_data_internal(
            dict(config_entry.data),
            reason="noop",
        )
        is False
    )

    object.__setattr__(
        config_entry,
        "runtime_data",
        SimpleNamespace(reload_suppression_count=0),
    )
    coord.hass.config_entries.async_update_entry = MagicMock(
        side_effect=RuntimeError("boom")
    )
    changed = dict(config_entry.data)
    changed["new_internal_key"] = "value"

    with pytest.raises(RuntimeError, match="boom"):
        coord._async_update_config_entry_data_internal(
            changed,
            reason="raises",
        )

    assert config_entry.runtime_data.reload_suppression_count == 0


def test_hems_auth_exception_and_manual_cooldown_edges(coordinator_factory):
    coord = coordinator_factory()
    login_wall = coord_mod.EnphaseLoginWallUnauthorized(
        endpoint="/systems/123/hems",
        request_label="GET /systems/[site]/hems",
        status=200,
    )
    forbidden = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=403,
        message="Forbidden",
    )

    assert coord._hems_auth_exception_details(login_wall) == (  # noqa: SLF001
        200,
        "login_wall",
    )
    assert coord._hems_auth_exception_details(forbidden) == (  # noqa: SLF001
        403,
        "forbidden",
    )

    coord._hems_auth_backoff_until = time.monotonic() + 60  # noqa: SLF001
    assert coord._skip_hems_polling_due_to_auth_circuit(  # noqa: SLF001
        endpoint="hems_devices"
    )

    coord._hems_auth_manual_clear_until = time.monotonic() - 1  # noqa: SLF001
    assert coord._hems_auth_manual_clear_retry_after_seconds() is None  # noqa: SLF001
    assert coord._hems_auth_manual_clear_until is None  # noqa: SLF001


def test_hems_auth_success_clears_circuit(coordinator_factory):
    coord = coordinator_factory()

    coord._note_hems_auth_failure(
        coord_mod.Unauthorized(), endpoint="hems_devices"
    )  # noqa: SLF001
    coord._note_hems_auth_success(endpoint="hems_devices")  # noqa: SLF001

    metrics = coord.collect_site_metrics()
    assert metrics["hems_auth_circuit_active"] is False
    assert metrics["hems_auth_failure_count"] == 0
    assert metrics["hems_auth_last_reason"] is None
    assert metrics["hems_auth_last_status"] is None
    assert metrics["hems_auth_last_success_utc"] is not None


@pytest.mark.asyncio
async def test_clear_hems_auth_backoff_clears_only_hems_state(coordinator_factory):
    coord = coordinator_factory()
    coord.async_request_refresh = AsyncMock()
    coord._auth_blocked_until_utc = datetime.now(timezone.utc) + timedelta(
        hours=1
    )  # noqa: SLF001
    coord._auth_block_reason = "login_wall_after_refresh_reject"  # noqa: SLF001
    coord._note_hems_auth_failure(
        coord_mod.Unauthorized(), endpoint="hems_devices"
    )  # noqa: SLF001

    response = await coord.async_clear_hems_auth_backoff()

    assert response["success"] is True
    assert response["reason"] == "cleared"
    assert response["hems_backoff_until"]
    assert coord._hems_auth_circuit_active() is False  # noqa: SLF001
    assert coord._hems_auth_failure_count == 1  # noqa: SLF001
    assert coord._hems_auth_last_reason == "unauthorized"  # noqa: SLF001
    assert coord._hems_auth_last_status == 401  # noqa: SLF001
    assert coord._auth_blocked_until_utc is not None  # noqa: SLF001
    coord.async_request_refresh.assert_awaited_once_with()

    coord._hems_auth_backoff_ends_utc = datetime.now(
        timezone.utc
    ) + timedelta(  # noqa: SLF001
        minutes=15
    )
    retry = await coord.async_clear_hems_auth_backoff()

    assert retry["success"] is False
    assert retry["reason"] == "manual_clear_cooldown_active"
    assert retry["hems_backoff_until"]
    assert retry["retry_after_seconds"] > 0


def test_collect_site_metrics_handles_unavailable_auth_recent_success_helper(
    coordinator_factory,
):
    coord = coordinator_factory()

    coord._auth_refresh_recent_success_active = None  # type: ignore[method-assign]  # noqa: SLF001
    metrics = coord.collect_site_metrics()
    assert metrics["auth_refresh_recent_success_active"] is False

    coord._auth_refresh_recent_success_active = MagicMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=RuntimeError("boom")
    )

    metrics = coord.collect_site_metrics()

    assert metrics["auth_refresh_recent_success_active"] is False


@pytest.mark.asyncio
async def test_async_start_charging_handles_not_ready(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    coord.data[sn]["plugged"] = True
    coord.require_plugged = MagicMock()
    coord.pick_start_amps = MagicMock(return_value=32)
    coord.client.start_charging = AsyncMock(return_value={"status": "not_ready"})
    coord.async_request_refresh = AsyncMock()

    result = await coord.async_start_charging(sn, hold_seconds=10)
    assert result["status"] == "not_ready"
    coord.set_desired_charging(sn, False)


@pytest.mark.asyncio
async def test_async_start_charging_handles_bad_data(coordinator_factory):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))

    class BadData:
        def __bool__(self):
            raise RuntimeError("boom")

    coord.data = BadData()
    coord.pick_start_amps = MagicMock(return_value=32)
    coord._charge_mode_start_preferences = MagicMock(
        return_value=ChargeModeStartPreferences()
    )
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord.set_charging_expectation = MagicMock()
    coord.kick_fast = MagicMock()
    coord.set_desired_charging = MagicMock()

    result = await coord.async_start_charging(sn, allow_unplugged=True)

    assert result == {"status": "ok"}
    coord.client.start_charging.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_stop_charging_respects_allow_flag(coordinator_factory):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    coord.require_plugged = MagicMock()
    coord.client.stop_charging = AsyncMock(return_value={"ok": True})
    coord.async_request_refresh = AsyncMock()

    await coord.async_stop_charging(sn, allow_unplugged=False)
    coord.require_plugged.assert_called_once_with(sn)


def test_fast_poll_helpers(coordinator_factory):
    coord = coordinator_factory()
    before = coord_mod.time.monotonic()
    coord.kick_fast("bad")
    assert coord._fast_until > before

    coord._last_actual_charging = {"EV1": False}
    coord._record_actual_charging("EV1", True)
    coord._record_actual_charging("EV1", None)
    assert "EV1" not in coord._last_actual_charging


def test_streaming_active_expires(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_manual = True
    coord._streaming_targets = {"EV1": True}
    coord._streaming_until = coord_mod.time.monotonic() - 1

    assert coord._streaming_active() is False
    assert coord._streaming is False
    assert coord._streaming_manual is False
    assert coord._streaming_targets == {}


def test_streaming_active_without_expiry(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_manual = True
    coord._streaming_until = None

    assert coord._streaming_active() is True
    assert coord._streaming_manual is True


def test_streaming_response_ok_variants(coordinator_factory):
    coord = coordinator_factory()
    assert coord._streaming_response_ok("ok") is True
    assert coord._streaming_response_ok({"status": None}) is True


def test_streaming_duration_invalid_uses_default(coordinator_factory):
    coord = coordinator_factory()
    duration = coord._streaming_duration_s({"duration_s": "bad"})
    assert duration == STREAMING_DEFAULT_DURATION_S


@pytest.mark.asyncio
async def test_async_start_streaming_tracks_targets(coordinator_factory):
    coord = coordinator_factory()
    coord.client.start_live_stream = AsyncMock(
        return_value={"status": "accepted", "duration_s": 900}
    )
    await coord.async_start_streaming(serial="EV1", expected_state=True)

    assert coord._streaming is True
    assert coord._streaming_manual is False
    assert coord._streaming_targets["EV1"] is True
    assert coord._streaming_until is not None


@pytest.mark.asyncio
async def test_async_start_streaming_respects_manual_lock(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming_manual = True
    coord.client.start_live_stream = AsyncMock(return_value={"status": "accepted"})

    await coord.async_start_streaming(serial="EV1", expected_state=True)

    coord.client.start_live_stream.assert_not_awaited()
    assert coord._streaming_targets == {}


@pytest.mark.asyncio
async def test_async_start_streaming_existing_stream_handles_error(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord.client.start_live_stream = AsyncMock(side_effect=RuntimeError("boom"))

    await coord.async_start_streaming(serial="EV1", expected_state=False)

    assert coord._streaming_targets["EV1"] is False


@pytest.mark.asyncio
async def test_async_start_streaming_rejects_error(coordinator_factory):
    coord = coordinator_factory()
    coord.client.start_live_stream = AsyncMock(return_value={"status": "error"})
    await coord.async_start_streaming(serial="EV1", expected_state=True)

    assert coord._streaming is False
    assert coord._streaming_targets == {}


@pytest.mark.asyncio
async def test_async_start_streaming_manual_clears_targets(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming_targets = {"EV1": True}
    coord.client.start_live_stream = AsyncMock(
        return_value={"status": "accepted", "duration_s": 900}
    )

    await coord.async_start_streaming(manual=True)

    assert coord._streaming is True
    assert coord._streaming_manual is True
    assert coord._streaming_targets == {}


@pytest.mark.asyncio
async def test_async_stop_streaming_manual_clears_state(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = coord_mod.time.monotonic() + 60
    coord._streaming_manual = True
    coord._streaming_targets = {"EV1": True}
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "accepted"})

    await coord.async_stop_streaming(manual=True)

    coord.client.stop_live_stream.assert_awaited_once()
    assert coord._streaming is False
    assert coord._streaming_manual is False
    assert coord._streaming_targets == {}


@pytest.mark.asyncio
async def test_async_stop_streaming_skips_manual_lock(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord._streaming_manual = True
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "accepted"})

    await coord.async_stop_streaming(manual=False)

    coord.client.stop_live_stream.assert_not_awaited()
    assert coord._streaming is True


@pytest.mark.asyncio
async def test_async_stop_streaming_skips_inactive(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = False
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "accepted"})

    await coord.async_stop_streaming(manual=False)

    coord.client.stop_live_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_stop_streaming_handles_error(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord.client.stop_live_stream = AsyncMock(side_effect=RuntimeError("boom"))

    await coord.async_stop_streaming(manual=True)

    coord.client.stop_live_stream.assert_awaited_once()
    assert coord._streaming is False


def test_auto_streaming_stops_on_expected_state(coordinator_factory):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = coord_mod.time.monotonic() + 60
    coord._streaming_targets = {"EV1": True}

    called = {}

    def _capture(force=False):
        called["force"] = force

    coord._schedule_stream_stop = _capture  # type: ignore[assignment]
    coord._record_actual_charging("EV1", True)

    assert called["force"] is True
    assert coord._streaming is False
    assert coord._streaming_targets == {}


def test_schedule_stream_stop_skips_when_task_active(coordinator_factory, monkeypatch):
    coord = coordinator_factory()

    class DummyTask:
        def done(self):
            return False

    coord._streaming_stop_task = DummyTask()
    capture = MagicMock()
    monkeypatch.setattr(coord.hass, "async_create_task", capture)

    coord._schedule_stream_stop()

    capture.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_stream_stop_force_runs(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "accepted"})

    tasks: list[asyncio.Task] = []

    def _create_task(coro, name=None):
        if name is not None:
            coro.close()
            raise TypeError("no name")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(coord.hass, "async_create_task", _create_task)

    coord._schedule_stream_stop(force=True)

    await tasks[0]
    coord.client.stop_live_stream.assert_awaited_once()
    assert coord._streaming is False


@pytest.mark.asyncio
async def test_schedule_stream_stop_runs_async_stop(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord.client.stop_live_stream = AsyncMock(return_value={"status": "accepted"})

    tasks: list[asyncio.Task] = []

    def _create_task(coro, name=None):
        if name is not None:
            coro.close()
            raise TypeError("no name")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(coord.hass, "async_create_task", _create_task)

    coord._schedule_stream_stop(force=False)

    await tasks[0]
    coord.client.stop_live_stream.assert_awaited_once()
    assert coord._streaming is False


@pytest.mark.asyncio
async def test_schedule_stream_stop_force_handles_error(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    coord._streaming = True
    coord._streaming_until = None
    coord.client.stop_live_stream = AsyncMock(side_effect=RuntimeError("boom"))

    tasks: list[asyncio.Task] = []

    def _create_task(coro, name=None):
        if name is not None:
            coro.close()
            raise TypeError("no name")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(coord.hass, "async_create_task", _create_task)

    coord._schedule_stream_stop(force=True)

    await tasks[0]
    coord.client.stop_live_stream.assert_awaited_once()
    assert coord._streaming is False


def test_schedule_amp_restart_replaces_existing_task(coordinator_factory, hass):
    coord = coordinator_factory()
    sn = next(iter(coord.serials))
    stored = {}

    class DummyTask:
        def __init__(self):
            self._done = False
            self.callbacks = []

        def cancel(self):
            self._done = True

        def done(self):
            return self._done

        def add_done_callback(self, cb):
            self.callbacks.append(cb)

    def fake_task(coro, *, name=None):
        coro.close()
        task = DummyTask()
        stored[name] = task
        return task

    hass.async_create_task = fake_task
    coord.schedule_amp_restart(sn, delay=1)
    coord.schedule_amp_restart(sn, delay=2)
    assert len(coord._amp_restart_tasks) == 1


@pytest.mark.asyncio
async def test_async_restart_after_amp_change_runs_sequence(monkeypatch):
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.async_stop_charging = AsyncMock()
    coord.async_start_charging = AsyncMock()
    monkeypatch.setattr(coord_mod.asyncio, "sleep", AsyncMock())

    await coord._async_restart_after_amp_change("EV1", "invalid")
    coord.async_stop_charging.assert_awaited()
    coord.async_start_charging.assert_awaited()


def test_persist_tokens_updates_entry_calls_hass_update(hass, config_entry):
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.config_entry = config_entry
    coord.hass = hass
    hass.config_entries.async_update_entry = MagicMock()
    tokens = coord_mod.AuthTokens("cookie", "sess", "token", 111)

    coord._persist_tokens(tokens)

    hass.config_entries.async_update_entry.assert_called_once()


def test_kick_fast_handles_invalid_seconds(coordinator_factory):
    coord = coordinator_factory()
    coord.kick_fast("invalid")
    assert coord._fast_until is not None


def test_record_actual_charging_triggers_fast(coordinator_factory):
    coord = coordinator_factory()
    coord.kick_fast = MagicMock()
    coord._record_actual_charging(SERIAL_ONE, True)
    coord._record_actual_charging(SERIAL_ONE, False)
    coord.kick_fast.assert_called_with(FAST_TOGGLE_POLL_HOLD_S)


def test_set_charging_expectation_handles_zero(coordinator_factory):
    coord = coordinator_factory()
    coord.set_charging_expectation(SERIAL_ONE, True, hold_for=0)
    assert SERIAL_ONE not in coord._pending_charging
    coord.set_charging_expectation(SERIAL_ONE, True, hold_for=10)
    assert SERIAL_ONE in coord._pending_charging


def test_clear_and_schedule_backoff_timer(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    cancelled = {"count": 0}

    def fake_cancel():
        cancelled["count"] += 1

    coord._backoff_cancel = fake_cancel
    coord._clear_backoff_timer()
    assert cancelled["count"] == 1

    created = {}

    coord.async_request_refresh = AsyncMock()

    def fake_async_call_later(_hass, delay, cb):
        created["callback"] = cb
        return lambda: created.setdefault("cancelled", True)

    monkeypatch.setattr(coord_mod, "async_call_later", fake_async_call_later)

    called = {}

    def fake_async_create_task(coro, *, name=None):
        called["coro"] = coro
        called["name"] = name
        return None

    monkeypatch.setattr(coord.hass, "async_create_task", fake_async_create_task)

    coord._schedule_backoff_timer(0)
    coro = called["coro"]
    coro.close()
    assert "coro" in called

    coord._schedule_backoff_timer(5)
    assert "callback" in created


def test_require_plugged_raises(monkeypatch):
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.data = {"EV1": {"name": "Garage", "plugged": False}}
    with pytest.raises(ServiceValidationError):
        coord.require_plugged("EV1")


def test_ensure_serial_tracked_discovers(monkeypatch):
    coord = _attach_evse_runtime(EnphaseCoordinator.__new__(EnphaseCoordinator))
    coord.serials = set()
    coord._serial_order = []
    assert coord._ensure_serial_tracked(" 12345 ") is True
    assert "12345" in coord.serials
    assert coord._ensure_serial_tracked("") is False
