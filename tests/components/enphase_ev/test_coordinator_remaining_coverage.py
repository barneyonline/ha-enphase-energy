"""Extra coverage for rarely hit coordinator branches."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import logging

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from aiohttp.client_reqrep import RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from custom_components.enphase_ev import coordinator as coord_mod
from custom_components.enphase_ev.coordinator import (
    ChargeModeStartPreferences,
    EnphaseCoordinator,
    ServiceValidationError,
)
from custom_components.enphase_ev.const import (
    CONF_COOKIE,
    DEFAULT_SLOW_POLL_INTERVAL,
    DOMAIN,
    ISSUE_DNS_RESOLUTION,
    ISSUE_NETWORK_UNREACHABLE,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_SLOW_POLL_INTERVAL,
)
from custom_components.enphase_ev.api import (
    AuthTokens,
    InvalidPayloadError,
    OptionalEndpointUnavailable,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

pytest.importorskip("homeassistant")


class _BadStr:
    def __str__(self) -> str:
        raise ValueError("boom")


def _request_info() -> RequestInfo:
    return RequestInfo(
        url=URL("https://enphase.example/status"),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL("https://enphase.example/status"),
    )


@pytest.mark.asyncio
async def test_async_request_refresh_marks_manual_bypass_for_single_cycle(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()

    async def _fake_super_refresh(self):
        assert self.endpoint_manual_bypass_active() is True

    monkeypatch.setattr(
        coord_mod.DataUpdateCoordinator,
        "async_request_refresh",
        _fake_super_refresh,
    )

    assert coord.endpoint_manual_bypass_active() is False
    await coord.async_request_refresh()
    assert coord.endpoint_manual_bypass_active() is False


def test_endpoint_family_failure_classification_and_core_backoff(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(coord_mod.random, "uniform", lambda _a, _b: 1.0)

    assert (
        coord._endpoint_family_policy("storm_alert").success_ttl_s == 300.0
    )  # noqa: SLF001
    assert (
        coord._endpoint_family_policy("inverter_production").success_ttl_s == 600.0
    )  # noqa: SLF001

    err_404 = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=404,
        message="missing",
    )
    err_invalid = InvalidPayloadError(
        "Invalid payload",
        status=429,
        endpoint="/systems/1/inverter_status_x.json",
        failure_kind="content_type",
    )

    assert coord._endpoint_family_status_from_error(err_404) == 404  # noqa: SLF001
    assert coord._endpoint_family_status_from_error(err_invalid) == 429  # noqa: SLF001
    assert (
        coord._endpoint_family_failure_is_cooldown_worthy(  # noqa: SLF001
            "core_realtime", err_404
        )
        is False
    )
    assert (
        coord._endpoint_family_failure_is_cooldown_worthy(  # noqa: SLF001
            "grid_control_check", err_404
        )
        is True
    )
    assert (
        coord._endpoint_family_failure_is_cooldown_worthy(  # noqa: SLF001
            "core_realtime", ValueError("bad")
        )
        is True
    )
    assert (
        coord._note_endpoint_family_failure("missing", ValueError("bad")) is False
    )  # noqa: SLF001

    err_406 = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=406,
        message="Not Acceptable",
    )
    assert (
        coord._note_endpoint_family_failure("core_realtime", err_406) is True
    )  # noqa: SLF001
    health = coord._endpoint_family_state("core_realtime")  # noqa: SLF001
    assert health.consecutive_failures == 1
    assert health.support_state == "unknown"
    assert health.cooldown_active is True
    assert health.next_retry_utc is not None
    assert coord._endpoint_family_should_run("core_realtime") is False  # noqa: SLF001


def test_endpoint_family_retry_after_numeric_overrides_policy_cap(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: now)
    monkeypatch.setattr(coord_mod.random, "uniform", lambda _a, _b: 1.0)

    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=429,
        message="Too Many Requests",
        headers={"Retry-After": "120"},
    )

    assert coord._note_endpoint_family_failure("tariff", err) is True  # noqa: SLF001

    health = coord._endpoint_family_state("tariff")  # noqa: SLF001
    assert health.next_retry_mono == pytest.approx(1_120.0)
    assert health.next_retry_utc == now + timedelta(seconds=120)


def test_endpoint_family_retry_after_http_date_and_invalid_fallback(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.time, "monotonic", lambda: 2_000.0)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: now)
    monkeypatch.setattr(coord_mod.random, "uniform", lambda _a, _b: 1.0)

    dated_err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=503,
        message="Service Unavailable",
        headers={"Retry-After": "Wed, 01 Jan 2025 12:01:30 GMT"},
    )

    assert (
        coord._note_endpoint_family_failure("core_realtime", dated_err) is True
    )  # noqa: SLF001
    health = coord._endpoint_family_state("core_realtime")  # noqa: SLF001
    assert health.next_retry_mono == pytest.approx(2_090.0)
    assert health.next_retry_utc == now + timedelta(seconds=90)

    invalid_err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=503,
        message="Service Unavailable",
        headers={"Retry-After": "invalid"},
    )
    missing_retry_after_err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=503,
        message="Service Unavailable",
        headers={"Date": "Wed, 01 Jan 2025 12:00:00 GMT"},
    )
    assert coord._retry_after_delay(missing_retry_after_err) is None  # noqa: SLF001
    assert (
        coord._note_endpoint_family_failure("battery_status", invalid_err) is True
    )  # noqa: SLF001
    battery_health = coord._endpoint_family_state("battery_status")  # noqa: SLF001
    assert battery_health.next_retry_mono == pytest.approx(2_300.0)


def test_endpoint_family_suppression_recovery_and_metrics(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(coord_mod.random, "uniform", lambda _a, _b: 1.0)

    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=406,
        message="Not Acceptable",
    )
    for _ in range(3):
        assert (
            coord._note_endpoint_family_failure("grid_control_check", err) is True
        )  # noqa: SLF001

    health = coord._endpoint_family_state("grid_control_check")  # noqa: SLF001
    assert health.consecutive_failures == 3
    assert health.support_state == "suppressed"
    assert health.cooldown_active is True
    assert (
        coord._endpoint_family_can_use_stale("grid_control_check") is False
    )  # noqa: SLF001

    metrics = coord.collect_site_metrics()
    family = metrics["endpoint_family_health"]["grid_control_check"]
    assert family["family"] == "grid_control_check"
    assert family["consecutive_failures"] == 3
    assert family["request_count"] == 3
    assert family["last_request_utc"] is not None
    assert family["last_status"] == 406
    assert family["suppressed"] is True
    assert family["next_retry_utc"] is not None
    assert family["last_failure_utc"] is not None

    coord._note_endpoint_family_success("grid_control_check")  # noqa: SLF001
    recovered = coord._endpoint_family_state("grid_control_check")  # noqa: SLF001
    assert recovered.consecutive_failures == 0
    assert recovered.request_count == 4
    assert recovered.support_state == "supported"
    assert recovered.last_success_utc is not None
    assert recovered.last_request_utc == recovered.last_success_utc
    assert recovered.cooldown_active is False

    recovered.next_retry_mono = coord_mod.time.monotonic() - 1
    recovered.next_retry_utc = datetime.now(timezone.utc) - timedelta(seconds=1)
    recovered.cooldown_active = True
    assert (
        coord._endpoint_family_wait_active("grid_control_check") is False
    )  # noqa: SLF001
    assert recovered.next_retry_utc is None


def test_tariff_endpoint_family_failure_reports_degraded_service(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(coord_mod.random, "uniform", lambda _a, _b: 1.0)

    err = OptionalEndpointUnavailable("Tariff payload did not include data")

    assert coord._note_endpoint_family_failure("tariff", err) is True  # noqa: SLF001

    metrics = coord.collect_site_metrics()
    tariff_health = metrics["endpoint_family_health"]["tariff"]
    assert tariff_health["consecutive_failures"] == 1
    assert tariff_health["last_error"] == "Tariff payload did not include data"
    assert tariff_health["next_retry_utc"] is not None
    assert metrics["tariff_available"] is False
    assert metrics["tariff_service_status"] == "degraded"
    assert metrics["tariff_failures"] == 1
    assert metrics["tariff_last_error"] == "Tariff payload did not include data"
    assert metrics["tariff_backoff_active"] is True
    assert metrics["tariff_backoff_ends_utc"] == tariff_health["next_retry_utc"]
    assert metrics["degraded_endpoint_families"] == ["tariff"]
    assert "tariff" in metrics["degraded_services"]


def test_battery_endpoint_family_failure_reports_degraded_service(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(coord_mod.random, "uniform", lambda _a, _b: 1.0)

    err = OptionalEndpointUnavailable("battery status temporarily unavailable")

    assert (
        coord._note_endpoint_family_failure("battery_status", err) is True
    )  # noqa: SLF001

    metrics = coord.collect_site_metrics()
    assert metrics["degraded_endpoint_families"] == ["battery_status"]
    assert "battery_status" in metrics["degraded_services"]


def test_degraded_endpoint_family_rollup_tolerates_unexpected_health(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(
        coord.diagnostics,
        "endpoint_family_health_diagnostics",
        lambda: {
            "invalid": object(),
            "suppressed": {
                "consecutive_failures": "bad",
                "cooldown_active": False,
                "suppressed": True,
            },
        },
    )

    metrics = coord.collect_site_metrics()

    assert metrics["degraded_endpoint_families"] == ["suppressed"]
    assert "suppressed" in metrics["degraded_services"]


def test_endpoint_family_misc_branches_and_diagnostics(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(coord_mod.random, "uniform", lambda _a, _b: 1.0)

    coord._endpoint_manual_bypass_requested = True  # noqa: SLF001
    assert coord._consume_endpoint_manual_bypass() is True  # noqa: SLF001
    assert coord.endpoint_manual_bypass_active() is True
    coord._clear_endpoint_manual_bypass()  # noqa: SLF001
    assert coord.endpoint_manual_bypass_active() is False

    assert coord._endpoint_family_should_run("unknown-family") is True  # noqa: SLF001
    assert (
        coord._endpoint_family_next_retry_mono("battery_status") is None
    )  # noqa: SLF001
    assert (
        coord._endpoint_family_can_use_stale("core_realtime") is False
    )  # noqa: SLF001
    assert (
        coord._endpoint_family_backoff_delay("unknown-family", 3) == 0.0
    )  # noqa: SLF001
    assert (
        coord._endpoint_family_failure_is_cooldown_worthy(  # noqa: SLF001
            "core_realtime",
            aiohttp.ClientConnectionError("boom"),
        )
        is True
    )
    assert (
        coord._endpoint_family_failure_is_cooldown_worthy(  # noqa: SLF001
            "grid_control_check",
            InvalidPayloadError("bad payload"),
        )
        is True
    )
    assert coord._note_endpoint_family_success("unknown-family") is None  # noqa: SLF001

    monkeypatch.setattr(
        coord_mod,
        "timedelta",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    coord._note_endpoint_family_success("battery_status")  # noqa: SLF001
    battery_health = coord._endpoint_family_state("battery_status")  # noqa: SLF001
    assert battery_health.next_retry_utc is None
    assert battery_health.request_count == 1
    assert battery_health.last_request_utc is not None
    coord._note_endpoint_family_success(
        "inverter_production", success_ttl_s=0.0
    )  # noqa: SLF001
    production_health = coord._endpoint_family_state(
        "inverter_production"
    )  # noqa: SLF001
    assert production_health.next_retry_mono is None
    assert production_health.next_retry_utc is None

    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=406,
        message="Not Acceptable",
    )
    coord._note_endpoint_family_failure("grid_control_check", err)  # noqa: SLF001
    grid_health = coord._endpoint_family_state("grid_control_check")  # noqa: SLF001
    assert grid_health.next_retry_utc is None
    assert grid_health.request_count == 1

    class BadDate:
        def isoformat(self) -> str:
            raise RuntimeError("boom")

        def __str__(self) -> str:
            return "bad-date"

    battery_health.last_success_utc = BadDate()  # type: ignore[assignment]
    battery_health.last_request_utc = BadDate()  # type: ignore[assignment]
    coord._endpoint_family_health[123] = grid_health  # type: ignore[index]  # noqa: SLF001
    coord._endpoint_family_health["none"] = None  # type: ignore[index]  # noqa: SLF001
    diagnostics = coord.diagnostics.endpoint_family_health_diagnostics()
    assert diagnostics["battery_status"]["last_success_utc"] == "bad-date"
    assert diagnostics["battery_status"]["last_request_utc"] == "bad-date"
    assert diagnostics["battery_status"]["request_count"] == 1
    assert "none" not in diagnostics
    assert 123 not in diagnostics

    coord._endpoint_family_health = []  # type: ignore[assignment]  # noqa: SLF001
    assert coord.diagnostics.endpoint_family_health_diagnostics() == {}


@pytest.mark.asyncio
async def test_coordinator_runtime_delegate_helpers_cover_direct_runtime_calls(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock()
    coord.heatpump_runtime.async_ensure_heatpump_runtime_diagnostics = AsyncMock()
    coord.heatpump_runtime._heatpump_primary_device_uid = MagicMock(
        return_value="HP-PRIMARY"
    )
    coord.heatpump_runtime._heatpump_daily_window = MagicMock(
        return_value=("2026-01-01", "2026-01-02", "Australia/Melbourne", ("a", "b"))
    )
    coord.heatpump_runtime._build_heatpump_daily_consumption_snapshot = MagicMock(
        return_value={"daily_energy_wh": 123.0}
    )
    coord.heatpump_runtime._heatpump_member_for_uid = MagicMock(
        return_value={"device_uid": "HP-PRIMARY"}
    )
    coord.heatpump_runtime._heatpump_member_alias_map = MagicMock(
        return_value={"HP-PRIMARY": "HP-PRIMARY"}
    )
    coord.heatpump_runtime._heatpump_power_candidate_is_recommended = MagicMock(
        return_value=True
    )
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()

    await coord._async_refresh_hems_support_preflight(force=True)  # noqa: SLF001
    await coord.async_ensure_heatpump_runtime_diagnostics(force=True)
    await coord._async_refresh_grid_control_check(force=True)  # noqa: SLF001

    assert coord._heatpump_primary_device_uid() == "HP-PRIMARY"  # noqa: SLF001
    assert coord._heatpump_daily_window() == (  # noqa: SLF001
        "2026-01-01",
        "2026-01-02",
        "Australia/Melbourne",
        ("a", "b"),
    )
    assert coord._build_heatpump_daily_consumption_snapshot(
        {"a": 1}
    ) == {  # noqa: SLF001
        "daily_energy_wh": 123.0
    }
    assert coord._build_heatpump_daily_consumption_snapshot(  # noqa: SLF001
        {"a": 1},
        {"stats": [{"heatpump": [123.0]}]},
    ) == {"daily_energy_wh": 123.0}
    assert coord._heatpump_member_for_uid("HP-PRIMARY") == {  # noqa: SLF001
        "device_uid": "HP-PRIMARY"
    }
    assert coord._heatpump_member_alias_map() == {  # noqa: SLF001
        "HP-PRIMARY": "HP-PRIMARY"
    }
    assert (
        coord._heatpump_power_candidate_is_recommended("HP-PRIMARY") is True
    )  # noqa: SLF001
    coord._heatpump_daily_split_last_error = "split-error"  # noqa: SLF001
    coord._heatpump_daily_split_using_stale = True  # noqa: SLF001
    coord._heatpump_daily_split_last_success_utc = datetime(  # noqa: SLF001
        2026, 1, 1, tzinfo=timezone.utc
    )
    assert coord.heatpump_daily_split_last_error == "split-error"
    assert coord.heatpump_daily_split_using_stale is True
    assert coord.heatpump_daily_split_last_success_utc == datetime(
        2026, 1, 1, tzinfo=timezone.utc
    )

    coord.heatpump_runtime.async_refresh_hems_support_preflight.assert_awaited_once_with(
        force=True
    )
    coord.heatpump_runtime.async_ensure_heatpump_runtime_diagnostics.assert_awaited_once_with(
        force=True
    )
    coord.battery_runtime.async_refresh_grid_control_check.assert_awaited_once_with(
        force=True
    )


def test_coordinator_static_and_class_runtime_helpers_cover_delegate_paths():
    assert coord_mod._coerce_floatish(1) == 1.0  # noqa: SLF001
    assert EnphaseCoordinator._format_inverter_model_summary({"IQ8": 2}) == "IQ8 x2"
    assert (
        EnphaseCoordinator._heatpump_member_primary_id({"device_uid": "HP-1"}) == "HP-1"
    )  # noqa: SLF001
    assert (
        EnphaseCoordinator._heatpump_member_parent_id({"parent": "GW-1"}) == "GW-1"
    )  # noqa: SLF001


@pytest.fixture
def fake_summary(monkeypatch):
    """Provide a stub summary store that records calls."""

    class _Summary:
        def __init__(self):
            self.force_calls: list[dict] = []
            self._cache = None
            self._ttl = 0

        def prepare_refresh(self, *, want_fast: bool, target_interval: float):
            self.force_calls.append({"want_fast": want_fast, "target": target_interval})
            return False

        async def async_fetch(self, *, force: bool = False):
            return []

        def invalidate(self):
            self.force_calls.append({"invalidate": True})

    summary = _Summary()
    monkeypatch.setattr(
        coord_mod, "SummaryStore", lambda *_, **__: summary, raising=False
    )
    return summary


@pytest.mark.asyncio
async def test_async_update_data_invalid_http_status_blank_payload(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=799,
        message="   ",
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_description == "HTTP error"
    assert coord.last_failure_response == ""


@pytest.mark.asyncio
async def test_async_update_data_http_error_plain_string(coordinator_factory):
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        _request_info(),
        (),
        status=502,
        message='"temporary"',
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.last_failure_description == "temporary"
    assert coord.last_failure_response == '"temporary"'


@pytest.mark.asyncio
async def test_async_update_data_invalid_payload_uses_payload_source(
    coordinator_factory, mock_issue_registry
):
    from custom_components.enphase_ev.const import ISSUE_CLOUD_ERRORS

    coord = coordinator_factory()
    coord._payload_errors = 1
    err = InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=text/html, decode_error=ValueError)",
        status=200,
        content_type="text/html",
        endpoint="/service/evse_controller/SITE/ev_chargers/status",
        failure_kind="json_decode",
        decode_error="ValueError",
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed, match="Invalid API payload"):
        await coord._async_update_data()

    assert coord.last_failure_status is None
    assert coord.last_failure_source == "payload"
    assert coord.last_failure_endpoint == err.endpoint
    assert coord.payload_failure_kind == "json_decode"
    assert coord.last_failure_description == err.summary
    assert coord.last_failure_response == err.summary
    assert coord._payload_errors == 2
    assert coord._http_errors == 0
    assert coord._network_errors == 0
    assert any(issue[1] == ISSUE_CLOUD_ERRORS for issue in mock_issue_registry.created)


@pytest.mark.asyncio
async def test_async_update_data_invalid_payload_reuses_cached_status_within_stale_window(
    coordinator_factory, mock_issue_registry
) -> None:
    from custom_components.enphase_ev.const import ISSUE_CLOUD_ERRORS

    coord = coordinator_factory()
    coord._has_successful_refresh = True
    cached_status = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Cached Charger",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
            }
        ],
        "ts": "1700000123",
    }
    coord._status_payload_cache = dict(cached_status)
    coord._mark_payload_endpoint_success(
        "status",
        success_mono=coord_mod.time.monotonic(),
        success_utc=datetime.now(timezone.utc),
    )
    coord._cloud_issue_reported = True
    err = InvalidPayloadError(
        "Invalid JSON response (status=200, endpoint=/service/evse_controller/SITE/ev_chargers/status)",
        status=200,
        endpoint="/service/evse_controller/SITE/ev_chargers/status",
        content_type="application/json",
        failure_kind="json_decode",
        decode_error="JSONDecodeError",
        body_length=15,
        body_sha256="deadbeef",
        body_preview_redacted='{"bad":true}',
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    result = await coord._async_update_data()

    assert RANDOM_SERIAL in result
    assert result[RANDOM_SERIAL]["sn"] == RANDOM_SERIAL
    assert coord.payload_using_stale is True
    assert coord.payload_failure_kind == err.failure_kind
    assert coord.last_failure_source == "payload"
    assert coord.last_failure_endpoint == err.endpoint
    assert coord._payload_errors == 0
    assert coord._backoff_until is None
    assert coord._payload_health["status"]["using_stale"] is True  # noqa: SLF001
    assert (
        coord._payload_health["status"]["last_payload_signature"]["endpoint"]
        == err.endpoint
    )  # noqa: SLF001
    assert not any(
        issue[1] == ISSUE_CLOUD_ERRORS for issue in mock_issue_registry.created
    )
    assert any(issue[1] == ISSUE_CLOUD_ERRORS for issue in mock_issue_registry.deleted)


@pytest.mark.asyncio
async def test_async_update_data_network_error_reuses_cached_status_within_stale_window(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSiteLastErrorCodeSensor

    coord = coordinator_factory()
    coord._has_successful_refresh = True
    coord.data = {RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "name": "Cached Charger"}}
    coord._status_payload_cache = {
        "evChargerData": [{"sn": RANDOM_SERIAL, "connectors": [{}]}],
        "ts": "1700000123",
    }
    coord._mark_payload_endpoint_success(
        "status",
        success_mono=coord_mod.time.monotonic(),
        success_utc=datetime.now(timezone.utc),
    )
    coord.client.status = AsyncMock(side_effect=asyncio.TimeoutError())
    coord._schedule_backoff_timer = MagicMock()

    result = await coord._async_update_data()

    assert RANDOM_SERIAL in result
    assert coord.payload_using_stale is True
    assert coord.payload_failure_kind is None
    assert coord.last_failure_source == "network"
    assert EnphaseSiteLastErrorCodeSensor(coord).native_value == "network_error"
    assert coord._network_errors == 1
    assert coord._backoff_until is None
    coord._schedule_backoff_timer.assert_not_called()
    assert "total_s" in coord.phase_timings
    assert coord._payload_health["status"]["using_stale"] is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_update_data_dns_error_reuses_stale_status_and_reports_dns_issue(
    coordinator_factory,
    mock_issue_registry,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSiteLastErrorCodeSensor

    coord = coordinator_factory()
    coord._has_successful_refresh = True
    coord.data = {RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "name": "Cached Charger"}}
    coord._status_payload_cache = {
        "evChargerData": [{"sn": RANDOM_SERIAL, "connectors": [{}]}],
        "ts": "1700000123",
    }
    coord._mark_payload_endpoint_success(
        "status",
        success_mono=coord_mod.time.monotonic(),
        success_utc=datetime.now(timezone.utc),
    )
    coord.client.status = AsyncMock(
        side_effect=aiohttp.ClientError("Temporary failure in name resolution")
    )
    coord._schedule_backoff_timer = MagicMock()

    first_result = await coord._async_update_data()
    second_result = await coord._async_update_data()

    assert RANDOM_SERIAL in first_result
    assert RANDOM_SERIAL in second_result
    assert coord.payload_using_stale is True
    assert coord.last_failure_source == "network"
    assert EnphaseSiteLastErrorCodeSensor(coord).native_value == "dns_error"
    assert coord._network_errors == 2
    assert coord._dns_failures == 2
    assert coord._backoff_until is None
    coord._schedule_backoff_timer.assert_not_called()
    assert any(
        issue[1] == ISSUE_DNS_RESOLUTION for issue in mock_issue_registry.created
    )
    assert not any(
        issue[1] == ISSUE_NETWORK_UNREACHABLE for issue in mock_issue_registry.created
    )


@pytest.mark.asyncio
async def test_async_update_data_stale_timeout_keeps_existing_dns_issue(
    coordinator_factory,
    mock_issue_registry,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True
    coord.data = {RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "name": "Cached Charger"}}
    coord._status_payload_cache = {
        "evChargerData": [{"sn": RANDOM_SERIAL, "connectors": [{}]}],
        "ts": "1700000123",
    }
    coord._mark_payload_endpoint_success(
        "status",
        success_mono=coord_mod.time.monotonic(),
        success_utc=datetime.now(timezone.utc),
    )
    coord.client.status = AsyncMock(
        side_effect=[
            aiohttp.ClientError("Temporary failure in name resolution"),
            aiohttp.ClientError("Temporary failure in name resolution"),
            asyncio.TimeoutError(),
        ]
    )
    coord._schedule_backoff_timer = MagicMock()

    await coord._async_update_data()
    await coord._async_update_data()
    timeout_result = await coord._async_update_data()

    assert RANDOM_SERIAL in timeout_result
    assert coord.payload_using_stale is True
    assert coord.last_failure_source == "network"
    assert coord._network_errors == 3
    assert coord._dns_failures == 2
    assert coord._dns_issue_reported is True
    assert coord._backoff_until is None
    assert (DOMAIN, ISSUE_DNS_RESOLUTION) not in mock_issue_registry.deleted
    assert not any(
        issue[1] == ISSUE_NETWORK_UNREACHABLE for issue in mock_issue_registry.created
    )


@pytest.mark.asyncio
async def test_async_update_data_invalid_payload_fails_when_status_stale_window_expires(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True
    coord._status_payload_cache = {
        "evChargerData": [{"sn": RANDOM_SERIAL, "connectors": [{}]}],
        "ts": "1700000123",
    }
    stale_window = coord._status_stale_window_s()  # noqa: SLF001
    coord._mark_payload_endpoint_success(
        "status",
        success_mono=coord_mod.time.monotonic() - stale_window - 1,
        success_utc=datetime.now(timezone.utc) - timedelta(seconds=stale_window + 1),
    )
    err = InvalidPayloadError(
        "Invalid JSON response (status=200, endpoint=/service/evse_controller/SITE/ev_chargers/status)",
        status=200,
        endpoint="/service/evse_controller/SITE/ev_chargers/status",
        content_type="application/json",
        failure_kind="json_decode",
        decode_error="JSONDecodeError",
    )
    coord.client.status = AsyncMock(side_effect=err)
    coord._schedule_backoff_timer = MagicMock()

    with pytest.raises(UpdateFailed, match="Invalid API payload"):
        await coord._async_update_data()

    assert coord.payload_using_stale is False
    assert coord._payload_health["status"]["using_stale"] is False  # noqa: SLF001
    assert coord._payload_errors == 1


@pytest.mark.asyncio
async def test_async_update_data_success_clears_status_stale_flags(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True
    cached_status = {
        "evChargerData": [{"sn": RANDOM_SERIAL, "name": "Cached", "connectors": [{}]}],
        "ts": "1700000123",
    }
    coord._status_payload_cache = dict(cached_status)
    coord._mark_payload_endpoint_success(
        "status",
        success_mono=coord_mod.time.monotonic(),
        success_utc=datetime.now(timezone.utc),
    )
    coord.client.status = AsyncMock(
        side_effect=[
            InvalidPayloadError(
                "Invalid JSON response (status=200, endpoint=/service/evse_controller/SITE/ev_chargers/status)",
                status=200,
                endpoint="/service/evse_controller/SITE/ev_chargers/status",
                content_type="application/json",
                failure_kind="json_decode",
                decode_error="JSONDecodeError",
                body_length=15,
                body_sha256="deadbeef",
                body_preview_redacted='{"bad":true}',
            ),
            {
                "evChargerData": [
                    {
                        "sn": RANDOM_SERIAL,
                        "name": "Recovered Charger",
                        "connectors": [{}],
                        "pluggedIn": False,
                        "charging": False,
                    }
                ],
                "ts": "1700000223",
            },
        ]
    )
    coord._schedule_backoff_timer = MagicMock()

    stale_result = await coord._async_update_data()
    fresh_result = await coord._async_update_data()

    assert RANDOM_SERIAL in stale_result
    assert RANDOM_SERIAL in fresh_result
    assert coord.payload_using_stale is False
    assert coord.payload_failure_kind is None
    assert coord.last_failure_endpoint is None
    assert coord._payload_health["status"]["using_stale"] is False  # noqa: SLF001
    assert coord._payload_health["status"]["failures"] == 0  # noqa: SLF001
    assert coord._payload_health["status"]["available"] is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_update_data_optional_status_unavailable_reuses_cached_status(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True
    cached_status = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Cached Charger",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
            }
        ],
        "ts": "1700000123",
    }
    coord._status_payload_cache = dict(cached_status)
    coord._mark_payload_endpoint_success(
        "status",
        success_mono=coord_mod.time.monotonic(),
        success_utc=datetime.now(timezone.utc),
    )
    coord.client.status = AsyncMock(
        side_effect=OptionalEndpointUnavailable(
            "Invalid JSON response (status=200, endpoint=/service/evse_controller/SITE/ev_chargers/status)"
        )
    )

    result = await coord._async_update_data()

    assert RANDOM_SERIAL in result
    assert result[RANDOM_SERIAL]["sn"] == RANDOM_SERIAL
    assert coord.payload_using_stale is True
    assert coord._payload_health["status"]["using_stale"] is True  # noqa: SLF001
    assert (
        coord._payload_health["status"]["last_payload_signature"] is None
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_update_data_reraises_config_entry_auth_failed(coordinator_factory):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(side_effect=ConfigEntryAuthFailed)

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_data_success_handles_edge_payloads(
    coordinator_factory, mock_issue_registry, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    coord = coordinator_factory()
    coord._unauth_errors = 2
    coord._has_successful_refresh = True
    coord._session_history_cache_ttl = None
    coord._schedule_session_enrichment = MagicMock()
    coord._async_enrich_sessions = AsyncMock(return_value={"ghost": []})

    class _View(SimpleNamespace):
        sessions: list | None = None
        needs_refresh = True
        blocked = False

    coord.session_history = SimpleNamespace(
        get_cache_view=lambda sn, day, now: _View(
            sessions=[], needs_refresh=True, blocked=False
        ),
        sum_energy=lambda sessions: 0.0,
    )

    data_ts = "1700000123456"
    obj1 = {
        "sn": RANDOM_SERIAL,
        "name": "Edge",
        "connectors": [{"safeLimitState": 1}],
        "pluggedIn": True,
        "charging": False,
        "faulted": False,
        "chargingLevel": 16,
        "session_d": {
            "e_c": 500.0,
            "miles": " ",
            "chargeLevel": " ",
            "charge_level": "oops",
        },
    }
    obj2 = {
        "sn": "AUX",
        "name": "Aux",
        "connectors": [{"safeLimitState": True}],
        "pluggedIn": True,
        "charging": False,
        "faulted": False,
        "session_d": {
            "e_c": 0,
            "miles": object(),
            "charge_level": True,
        },
    }
    obj3 = {
        "sn": "THIRD",
        "name": "Third",
        "connectors": [{}],
        "pluggedIn": True,
        "charging": False,
        "faulted": False,
        "session_d": {
            "e_c": 0,
            "charge_level": "oops",
        },
    }
    obj4 = {
        "sn": "FOURTH",
        "name": "Fourth",
        "connectors": [{}],
        "pluggedIn": True,
        "charging": False,
        "faulted": False,
        "session_d": {
            "e_c": 0,
            "chargingLevel": [],
        },
    }
    payload = {"evChargerData": [obj1, obj2, obj3, obj4], "ts": data_ts}
    coord.client.status = AsyncMock(return_value=payload)

    original_round = getattr(coord_mod, "round", round)

    def fake_round(value, ndigits=None):
        if isinstance(value, float) and abs(value - 0.5) < 0.01:
            raise ValueError("boom")
        if ndigits is None:
            return original_round(value)
        return original_round(value, ndigits)

    monkeypatch.setattr(coord_mod, "round", fake_round, raising=False)

    bad_interval = type("BadInterval", (), {"__str__": lambda self: "oops"})()

    summary_entries = [
        {},
        {
            "serialNumber": RANDOM_SERIAL,
            "chargeLevelDetails": {
                "min": "bad",
                "max": object(),
                "granularity": object(),
            },
            "reportingInterval": bad_interval,
            "operatingVoltage": object(),
            "networkConfig": [
                {"ipaddr": "10.0.0.10", "connectionStatus": "0"},
                {"ip": "10.0.0.11", "connectionStatus": "true"},
            ],
            "commissioningStatus": True,
            "dlbEnabled": "yes",
            "lastReportedAt": "2025-01-01T00:00:00Z",
            "lifeTimeConsumption": 5.5,
            "maxCurrent": 32,
            "phaseMode": "SINGLE",
            "status": "ONLINE",
            "activeConnection": " wifi ",
        },
        {
            "serialNumber": "NEW2",
            "networkConfig": '[\n  "ipaddr=,connectionStatus=0",\n  "ipaddr=192.0.2.1,connectionStatus=1"\n',
            "reportingInterval": "bad",
            "operatingVoltage": "bad",
        },
    ]

    summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=summary_entries),
        invalidate=MagicMock(),
    )
    coord.summary = summary

    original_as_local = coord_mod.dt_util.as_local

    now_calls = {"count": 0}

    def fake_now():
        now_calls["count"] += 1
        if now_calls["count"] == 1:
            raise RuntimeError("boom")
        return datetime(2025, 1, 1, tzinfo=timezone.utc)

    as_local_calls = {"count": 0}

    def fake_as_local(value):
        as_local_calls["count"] += 1
        if as_local_calls["count"] == 1:
            raise RuntimeError("fail")
        return original_as_local(value)

    monkeypatch.setattr(coord_mod.dt_util, "now", fake_now)
    monkeypatch.setattr(coord_mod.dt_util, "as_local", fake_as_local)

    coord.config_entry = SimpleNamespace(
        options={
            OPT_FAST_POLL_INTERVAL: "bad",
            OPT_SLOW_POLL_INTERVAL: "oops",
            OPT_FAST_WHILE_STREAMING: object(),
        },
        data={"entry_id": "123"},
        entry_id="123",
    )
    coord.async_set_update_interval = MagicMock(side_effect=RuntimeError("legacy"))

    result = await coord._async_update_data()

    assert any(issue[1] == "reauth_required" for issue in mock_issue_registry.deleted)
    assert any(
        "Coordinator refresh timings" in record.message for record in caplog.records
    )
    snapshot_data = {key: result[key] for key in sorted(result.keys())}
    assert set(snapshot_data) == {
        RANDOM_SERIAL,
        "AUX",
        "FOURTH",
        "NEW2",
        "THIRD",
    }
    main = snapshot_data[RANDOM_SERIAL]
    assert main["ip_address"] == "10.0.0.11"
    assert main["lifetime_kwh"] == 5.5
    assert main["status"] == "ONLINE"
    assert main["charge_mode"] == "IDLE"
    assert main["energy_today_sessions_kwh"] == 0.0
    assert main["charging_level"] == 16
    assert main["safe_limit_state"] == 1
    assert RANDOM_SERIAL not in coord.last_set_amps
    aux = snapshot_data["AUX"]
    assert aux["last_reported_at"] is not None
    assert aux["session_energy_wh"] == 0.0
    new2 = snapshot_data["NEW2"]
    assert new2["ip_address"] == "192.0.2.1"
    assert new2["max_current"] is None


@pytest.mark.asyncio
async def test_async_update_data_resets_issues_and_network_config(
    coordinator_factory, mock_issue_registry, monkeypatch
):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord._unauth_errors = 1
    coord._network_issue_reported = True
    coord._cloud_issue_reported = True
    coord._dns_issue_reported = True
    coord._backoff_cancel = lambda: None
    coord._has_successful_refresh = True

    original_as_local = coord_mod.dt_util.as_local
    calls = {"count": 0}

    def fake_as_local(value):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return original_as_local(value)

    monkeypatch.setattr(coord_mod.dt_util, "now", lambda: datetime(2025, 1, 1))
    monkeypatch.setattr(coord_mod.dt_util, "as_local", fake_as_local)

    coord.async_set_update_interval = MagicMock(side_effect=TypeError("legacy"))
    coord.config_entry = SimpleNamespace(
        options={}, data={"entry_id": "123"}, entry_id="123"
    )

    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *args, **kwargs: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda *_: 0.0,
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(
            return_value=[
                {
                    "serialNumber": sn,
                    "networkConfig": ["", {"ipaddr": "", "connectionStatus": "0"}],
                    "reportingInterval": "oops",
                }
            ]
        ),
        invalidate=MagicMock(),
    )
    payload = {
        "evChargerData": [
            {
                "sn": sn,
                "name": "EV",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "session_d": {"chargeLevel": "bad", "miles": "oops"},
            }
        ],
        "ts": "2025-01-01T00:00:00Z",
    }
    coord.client.status = AsyncMock(return_value=payload)

    result = await coord._async_update_data()

    assert any(issue[1] == "reauth_required" for issue in mock_issue_registry.deleted)
    assert coord._dns_issue_reported is False
    assert result[sn].get("ip_address") is None


@pytest.mark.asyncio
async def test_async_update_data_session_end_fix_handles_invalid_timestamp(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    coord.tariff_runtime.async_refresh = AsyncMock()
    sn = RANDOM_SERIAL
    coord._last_charging[sn] = True

    calls = {"count": 0}

    def fake_time():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return 123456

    monkeypatch.setattr(coord_mod.time, "time", fake_time)
    coord.client.status = AsyncMock(
        return_value={
            "ts": float("nan"),
            "evChargerData": [
                {
                    "sn": sn,
                    "name": "EV",
                    "connectors": [{}],
                    "session_d": {},
                    "pluggedIn": True,
                    "charging": False,
                }
            ],
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *args, **kwargs: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda sessions: 0.0,
    )

    await coord._async_update_data()
    assert coord._session_end_fix[sn] == 123456


@pytest.mark.asyncio
async def test_async_update_data_session_end_fix_default_branch(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord.tariff_runtime.async_refresh = AsyncMock()
    sn = RANDOM_SERIAL
    coord._last_charging[sn] = True
    coord.client.status = AsyncMock(
        return_value={
            "ts": "bad",
            "evChargerData": [
                {
                    "sn": sn,
                    "name": "EV",
                    "connectors": [{}],
                    "session_d": {},
                    "pluggedIn": True,
                    "charging": False,
                }
            ],
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *args, **kwargs: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda sessions: 0.0,
    )

    await coord._async_update_data()
    assert coord._session_end_fix[sn]


@pytest.mark.asyncio
async def test_async_update_data_handles_invalid_global_timestamp(coordinator_factory):
    coord = coordinator_factory()
    coord.tariff_runtime.async_refresh = AsyncMock()
    sn = RANDOM_SERIAL
    coord.client.status = AsyncMock(
        return_value={
            "ts": "2025-13-99T00:00:00Z",
            "evChargerData": [
                {
                    "sn": sn,
                    "name": "EV",
                    "connectors": [{}],
                    "session_d": {},
                    "pluggedIn": True,
                    "charging": False,
                }
            ],
        }
    )
    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[]),
        invalidate=MagicMock(),
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *args, **kwargs: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda sessions: 0.0,
    )

    result = await coord._async_update_data()
    assert result[sn]["last_reported_at"] is None


def test_sync_desired_charging_handles_auto_resume_typeerror(coordinator_factory):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord._desired_charging[sn] = True

    def fake_create_task(coro, *, name=None):
        if name is not None:
            coro.close()
            raise TypeError("legacy")
        coro.close()
        return None

    coord.hass.async_create_task = MagicMock(side_effect=fake_create_task)
    info = {
        "charging": False,
        "plugged": True,
        "connector_status": "SUSPENDED_EVSE",
    }
    coord._sync_desired_charging(
        {sn: info, "other": {"charging": False, "plugged": False}}
    )
    coord.hass.async_create_task.assert_called()


def test_sync_desired_charging_skips_when_unplugged(coordinator_factory):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord._desired_charging[sn] = True
    coord.hass.async_create_task = MagicMock()
    coord._sync_desired_charging({sn: {"charging": False, "plugged": False}})
    coord.hass.async_create_task.assert_not_called()


def test_sync_desired_charging_skips_auto_resume_for_green_mode(coordinator_factory):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord._desired_charging[sn] = True
    coord.hass.async_create_task = MagicMock()
    coord._sync_desired_charging(
        {
            sn: {
                "charging": False,
                "plugged": True,
                "connector_status": "SUSPENDED_EVSE",
                "charge_mode_pref": "GREEN_CHARGING",
            }
        }
    )
    coord.hass.async_create_task.assert_not_called()


def test_sync_desired_charging_handles_unstringable_mode(coordinator_factory):
    coord = coordinator_factory()
    sn = RANDOM_SERIAL
    coord._desired_charging[sn] = True

    class BadMode:
        def __str__(self):
            raise ValueError("bad mode")

    def _fake_create_task(coro, *, name=None):
        coro.close()
        return None

    coord.hass.async_create_task = MagicMock(side_effect=_fake_create_task)
    coord._sync_desired_charging(
        {
            sn: {
                "charging": False,
                "plugged": True,
                "connector_status": "SUSPENDED_EVSE",
                "charge_mode_pref": BadMode(),
            }
        }
    )
    coord.hass.async_create_task.assert_called_once()


@pytest.mark.asyncio
async def test_async_update_data_covers_remaining_branches(
    coordinator_factory, mock_issue_registry
):
    coord = coordinator_factory()
    coord._unauth_errors = 1
    coord._has_successful_refresh = True
    coord._backoff_cancel = lambda: None

    class BadNumeric(float):
        def __int__(self):
            raise ValueError("bad int")

    payload = {
        "ts": "2025-01-01T00:00:00Z",
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "EV",
                "connectors": [{}],
                "pluggedIn": True,
                "charging": False,
                "faulted": False,
                "session_d": {"chargeLevel": BadNumeric()},
            }
        ],
    }

    summary_entry = {
        "serialNumber": RANDOM_SERIAL,
        "networkConfig": {"ipaddr": ""},
        "reportingInterval": "",
    }

    coord.summary = SimpleNamespace(
        prepare_refresh=lambda **_: False,
        async_fetch=AsyncMock(return_value=[summary_entry]),
        invalidate=lambda: None,
    )
    coord.session_history = SimpleNamespace(
        get_cache_view=lambda *_, **__: SimpleNamespace(
            sessions=[], needs_refresh=False, blocked=False
        ),
        sum_energy=lambda *_: 0.0,
    )
    coord.client.status = AsyncMock(return_value=payload)
    coord.config_entry = SimpleNamespace(
        options={}, data={"entry_id": "123"}, entry_id="123"
    )
    coord.async_set_update_interval = AsyncMock(side_effect=RuntimeError("boom"))

    result = await coord._async_update_data()

    assert any(issue[1] == "reauth_required" for issue in mock_issue_registry.deleted)
    assert result[RANDOM_SERIAL].get("reporting_interval") is None
    assert result[RANDOM_SERIAL].get("ip_address") is None


@pytest.mark.asyncio
async def test_async_auto_resume_handles_snapshot_and_state(coordinator_factory):
    coord = coordinator_factory()
    coord.data = object()
    coord.pick_start_amps = lambda *args, **kwargs: 16
    coord._charge_mode_start_preferences = lambda sn: ChargeModeStartPreferences()
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    snapshot = {"plugged": True}
    await coord._async_auto_resume(RANDOM_SERIAL, snapshot)
    coord.client.start_charging.assert_awaited_once()


def test_apply_lifetime_guard_handles_invalid_samples(coordinator_factory):
    coord = coordinator_factory()

    class ExplodingFloat(float):
        def __new__(cls, value):
            return super().__new__(cls, value)

        def __float__(self):
            raise ValueError("boom")

    prev = {"lifetime_kwh": ExplodingFloat(5.0)}
    assert coord.energy._apply_lifetime_guard("sn", "bad", prev) is None

    coord.energy._lifetime_guard["sn"].last = None
    assert coord.energy._apply_lifetime_guard("sn", -1.0, None) == 0.0

    coord.energy._lifetime_guard["sn"].last = 5.0
    assert (
        coord.energy._apply_lifetime_guard("sn", 4.7, None)
        == coord.energy._lifetime_guard["sn"].last
    )


def test_determine_polling_state_handles_bad_options(coordinator_factory):
    coord = coordinator_factory()

    class WeirdOptions(dict):
        def get(self, key, default=None):  # noqa: D401
            if key == OPT_FAST_WHILE_STREAMING:
                raise ValueError("bad")
            return super().get(key, default)

    coord.config_entry = SimpleNamespace(
        options=WeirdOptions(
            {
                OPT_FAST_POLL_INTERVAL: "bogus",
                OPT_SLOW_POLL_INTERVAL: "oops",
            }
        ),
        data={},
        entry_id="1",
    )
    coord.update_interval = timedelta(seconds=15)
    state = coord._determine_polling_state({})
    assert state["want_fast"] in (True, False)


def test_determine_polling_state_handles_invalid_configured_slow_interval(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord._configured_slow_poll_interval = "invalid"  # noqa: SLF001
    coord.config_entry = SimpleNamespace(options={})

    state = coord._determine_polling_state({})

    assert state["slow"] == DEFAULT_SLOW_POLL_INTERVAL


@pytest.mark.asyncio
async def test_async_resolve_charge_modes_skips_empty_serials(coordinator_factory):
    coord = coordinator_factory()
    coord._charge_mode_cache.clear()
    coord.evse_runtime.async_get_charge_mode = AsyncMock(  # type: ignore[method-assign]
        return_value="IDLE"
    )
    result = await coord.evse_runtime.async_resolve_charge_modes(["", RANDOM_SERIAL])
    assert result.get(RANDOM_SERIAL).mode == "IDLE"
    assert result.get(RANDOM_SERIAL).source == "scheduler_endpoint"


def test_has_embedded_charge_mode_for_non_dict(coordinator_factory):
    coord = coordinator_factory()
    assert coord._has_embedded_charge_mode([]) is False


def test_persist_tokens_handles_missing_entry(coordinator_factory):
    coord = coordinator_factory()
    coord.config_entry = None
    coord._persist_tokens(AuthTokens(None, None, None, None))


def test_persist_tokens_drops_none_fields(coordinator_factory):
    coord = coordinator_factory()
    merged = {}
    coord.config_entry = SimpleNamespace(data={}, entry_id="1")

    def _update(entry, *, data):
        merged.update(data)

    coord.hass.config_entries.async_update_entry = _update
    coord._persist_tokens(
        AuthTokens(
            cookie=None, access_token="tok", session_id=None, token_expires_at=None
        )
    )
    assert merged.get("access_token") == "tok"
    assert merged.get(CONF_COOKIE) == ""


def test_set_charging_expectation_handles_invalid_hold(coordinator_factory):
    coord = coordinator_factory()
    coord.set_charging_expectation(RANDOM_SERIAL, True, hold_for="bad")
    assert RANDOM_SERIAL in coord._pending_charging


def test_slow_interval_floor_handles_bad_update_interval(coordinator_factory):
    coord = coordinator_factory()
    coord.config_entry = SimpleNamespace(options={})

    class BadInterval:
        def total_seconds(self):
            raise ValueError("boom")

    coord.__dict__["update_interval"] = BadInterval()
    assert coord._slow_interval_floor() >= 1


@pytest.mark.asyncio
async def test_schedule_backoff_timer_handles_invalid_now(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(coord_mod.dt_util, "utcnow", _raise)
    coord.hass.async_create_task = MagicMock(
        side_effect=lambda coro: (coro.close(), None)[1]
    )
    coord._schedule_backoff_timer(1)


def test_require_plugged_handles_bad_data(coordinator_factory):
    coord = coordinator_factory()
    coord.data = object()
    with pytest.raises(ServiceValidationError):
        coord.require_plugged(RANDOM_SERIAL)


def test_ensure_serial_tracked_invalid_inputs(coordinator_factory):
    coord = coordinator_factory()
    assert coord._ensure_serial_tracked(None) is False

    class BadSerial:
        def __str__(self):
            raise ValueError("boom")

    assert coord._ensure_serial_tracked(BadSerial()) is False

    coord.serials = {RANDOM_SERIAL}
    coord._serial_order = []
    assert coord._ensure_serial_tracked(RANDOM_SERIAL) is False
    assert RANDOM_SERIAL in coord._serial_order


def test_iter_serials_falls_back_to_sorted(coordinator_factory):
    coord = coordinator_factory()
    coord._serial_order = []
    coord.serials = {"B", "A"}
    ordered = coord.iter_serials()
    assert ordered[:2] == ["A", "B"]


def test_iter_serials_empty_when_site_only(coordinator_factory):
    coord = coordinator_factory()
    coord.serials = {"A"}
    coord._serial_order = ["A"]
    coord.site_only = True
    assert coord.iter_serials() == []


def test_coerce_amp_and_amp_limits(coordinator_factory):
    coord = coordinator_factory()
    assert coord._coerce_amp("   ") is None
    assert coord._coerce_amp([]) is None
    coord.data = object()
    min_amp, max_amp = coord._amp_limits(RANDOM_SERIAL)
    assert min_amp is None and max_amp is None


def test_pick_start_amps_handles_invalid_data(coordinator_factory):
    coord = coordinator_factory()
    coord.data = object()
    coord.last_set_amps.clear()
    assert coord.pick_start_amps(RANDOM_SERIAL, requested=None, fallback=16) == 16


def test_pick_start_amps_uses_fallback_default(coordinator_factory):
    coord = coordinator_factory()
    coord.data = {}
    coord.last_set_amps.clear()
    assert coord.pick_start_amps(RANDOM_SERIAL, requested=None, fallback="bad") == 32
