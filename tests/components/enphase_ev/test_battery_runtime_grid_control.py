from __future__ import annotations

from types import SimpleNamespace
import time
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.const import GRID_OUTAGE_CONTEXT_CACHE_TTL


def test_grid_control_supported_is_unknown_before_first_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    assert coord.grid_control_supported is None
    assert coord.grid_toggle_allowed is None
    assert coord.grid_toggle_blocked_reasons == []


def test_parse_grid_control_check_payload_maps_flags_and_allows(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_pending is False
    assert coord.grid_toggle_blocked_reasons == []
    assert coord.grid_toggle_allowed is True


def test_parse_grid_control_check_payload_tracks_blocked_reasons(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": True,
            "activeDownload": True,
            "sunlightBackupSystemCheck": True,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_pending is False
    assert coord.grid_toggle_allowed is False
    assert coord.grid_toggle_blocked_reasons == [
        "disable_grid_control",
        "active_download",
        "sunlight_backup_system_check",
    ]


def test_parse_grid_control_check_payload_nested_data_and_grid_outage_reason(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "data": {
                "disableGridControl": False,
                "activeDownload": False,
                "sunlightBackupSystemCheck": False,
                "gridOutageCheck": True,
                "userInitiatedGridToggle": False,
            }
        }
    )

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_allowed is False
    assert coord.grid_toggle_blocked_reasons == ["grid_outage_check"]


def test_parse_grid_control_check_payload_pending_state(coordinator_factory) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": True,
        }
    )

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_pending is True
    assert coord.grid_toggle_allowed is False


def test_parse_grid_control_check_payload_partial_is_unknown_allowed(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
        }
    )

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_pending is False
    assert coord.grid_toggle_blocked_reasons == []
    assert coord.grid_toggle_allowed is None


def test_parse_grid_control_check_payload_missing_or_invalid_marks_unsupported(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_grid_control_check_payload({})
    assert coord.grid_control_supported is False
    assert coord.grid_toggle_allowed is None

    coord.battery_runtime.parse_grid_control_check_payload(["bad"])
    assert coord.grid_control_supported is False
    assert coord.grid_toggle_allowed is None


@pytest.mark.asyncio
async def test_refresh_grid_control_check_caches_and_redacts(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.grid_control_check = AsyncMock(
        return_value={
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
            "token": "secret-token",
        }
    )

    await coord.battery_runtime.async_refresh_grid_control_check(force=True)

    assert coord.grid_control_supported is True
    assert coord._grid_control_check_payload is not None  # noqa: SLF001
    assert coord._grid_control_check_payload["token"] == "[redacted]"  # noqa: SLF001

    coord._grid_control_check_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.grid_control_check.reset_mock()
    await coord.battery_runtime.async_refresh_grid_control_check()
    coord.client.grid_control_check.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_grid_control_check_wraps_non_dict_redaction(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.grid_control_check = AsyncMock(
        return_value={
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._redact_battery_payload = lambda _payload: "masked"  # type: ignore[method-assign]  # noqa: SLF001

    await coord.battery_runtime.async_refresh_grid_control_check(force=True)

    assert coord._grid_control_check_payload == {"value": "masked"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_grid_outage_context_caches_and_redacts(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.off_grid_due_to_grid_outage = AsyncMock(
        return_value={
            "is_grid_outage": True,
            "show_grid_connect": False,
            "has_battery": True,
            "is_sunlight_backup": False,
            "token": "secret-token",
        }
    )

    await coord.battery_runtime.async_refresh_grid_outage_context(force=True)

    assert coord.grid_outage_context_supported is True
    assert coord.grid_outage_is_grid_outage is True
    assert coord.grid_mode == "off_grid"
    assert coord._grid_outage_context_payload is not None  # noqa: SLF001
    assert coord._grid_outage_context_payload["token"] == "[redacted]"  # noqa: SLF001

    coord._grid_outage_context_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.off_grid_due_to_grid_outage.reset_mock()
    await coord.battery_runtime.async_refresh_grid_outage_context()
    coord.client.off_grid_due_to_grid_outage.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_grid_mode_status_uses_livestream_grid_relay(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    envoy_serial = coord.battery_runtime.grid_envoy_serial()
    assert envoy_serial is not None
    coord.client.site_livestream_payload = AsyncMock(
        return_value={
            "meters": {
                "gridRelay": "OPER_RELAY_CLOSED",
                "serial_number": "secret-meter",
            }
        }
    )

    await coord.battery_runtime.async_refresh_grid_mode_status(force=True)

    coord.client.site_livestream_payload.assert_awaited_once_with(envoy_serial)
    assert coord.grid_mode_status_supported is True
    assert coord.grid_mode_status_raw == "OPER_RELAY_CLOSED"
    assert coord.grid_mode == "on_grid"
    assert coord.grid_mode_source == "livestream_grid_relay"
    assert coord._grid_mode_status_payload is not None  # noqa: SLF001
    assert (
        coord._grid_mode_status_payload["meters"]["serial_number"] == "[redacted]"
    )  # noqa: SLF001

    coord._grid_mode_status_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.site_livestream_payload.reset_mock()
    await coord.battery_runtime.async_refresh_grid_mode_status()
    coord.client.site_livestream_payload.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_grid_mode_status_requires_gateway_serial(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._grid_mode_status = "on_grid"  # noqa: SLF001
    coord._grid_mode_status_raw = "OPER_RELAY_CLOSED"  # noqa: SLF001
    coord.battery_runtime.grid_envoy_serial = lambda: None  # type: ignore[method-assign]
    coord.client.site_livestream_payload = AsyncMock()

    await coord.battery_runtime.async_refresh_grid_mode_status(force=True)

    coord.client.site_livestream_payload.assert_not_called()
    assert coord.grid_mode_status_supported is None
    assert coord.grid_mode_status is None
    assert coord.grid_mode_status_raw is None


@pytest.mark.asyncio
async def test_refresh_grid_mode_status_missing_relay_uses_failure_retry(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    envoy_serial = coord.battery_runtime.grid_envoy_serial()
    assert envoy_serial is not None
    coord.client.site_livestream_payload = AsyncMock(
        return_value={"meters": {"serial_number": "secret-meter"}}
    )

    now = time.monotonic()
    await coord.battery_runtime.async_refresh_grid_mode_status(force=True)

    coord.client.site_livestream_payload.assert_awaited_once_with(envoy_serial)
    assert coord.grid_mode_status_supported is False
    assert coord.grid_mode_status is None
    assert coord.grid_mode_status_raw is None
    assert coord._grid_mode_status_failures == 1  # noqa: SLF001
    assert coord._grid_mode_status_last_success_mono is None  # noqa: SLF001
    assert (
        now < coord._grid_mode_status_cache_until <= time.monotonic() + 15.0
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_grid_mode_status_missing_relay_keeps_recent_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    envoy_serial = coord.battery_runtime.grid_envoy_serial()
    assert envoy_serial is not None
    coord.battery_runtime.parse_grid_mode_status_payload(
        {"meters": {"gridRelay": "OPER_RELAY_CLOSED"}}
    )
    coord._grid_mode_status_last_success_mono = time.monotonic()  # noqa: SLF001
    coord.client.site_livestream_payload = AsyncMock(
        return_value={"meters": {"serial_number": "secret-meter"}}
    )

    await coord.battery_runtime.async_refresh_grid_mode_status(force=True)

    coord.client.site_livestream_payload.assert_awaited_once_with(envoy_serial)
    assert coord.grid_mode_status_supported is True
    assert coord.grid_mode_status == "on_grid"
    assert coord.grid_mode_status_raw == "OPER_RELAY_CLOSED"
    assert coord._grid_mode_status_failures == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_grid_mode_status_clears_state_during_stale_cooldown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    family_state = coord._endpoint_family_state("grid_mode_status")  # noqa: SLF001
    family_state.cooldown_active = True
    coord._endpoint_family_should_run = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    coord._endpoint_family_can_use_stale = lambda *_args: False  # type: ignore[method-assign]  # noqa: SLF001

    assert coord.battery_runtime.grid_mode_status_refresh_due() is False

    coord._grid_mode_status_supported = True  # noqa: SLF001
    coord._grid_mode_status = "on_grid"  # noqa: SLF001
    coord._grid_mode_status_raw = "OPER_RELAY_CLOSED"  # noqa: SLF001
    assert coord.battery_runtime.grid_mode_status_refresh_due() is True

    await coord.battery_runtime.async_refresh_grid_mode_status()

    assert coord._grid_mode_status_supported is None  # noqa: SLF001
    assert coord._grid_mode_status is None  # noqa: SLF001
    assert coord._grid_mode_status_raw is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_grid_mode_status_wraps_non_dict_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.site_livestream_payload = AsyncMock(return_value=None)

    await coord.battery_runtime.async_refresh_grid_mode_status(force=True)

    assert coord._grid_mode_status_payload == {"value": None}  # noqa: SLF001
    assert coord.grid_mode_status_supported is False


@pytest.mark.asyncio
async def test_refresh_grid_outage_context_family_ttl_matches_cache(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.off_grid_due_to_grid_outage = AsyncMock(
        return_value={
            "is_grid_outage": False,
            "show_grid_connect": True,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )

    await coord.battery_runtime.async_refresh_grid_outage_context(force=True)

    family_state = coord._endpoint_family_state("grid_outage_context")  # noqa: SLF001
    assert family_state.last_success_mono is not None
    assert family_state.next_retry_mono is not None
    assert (
        family_state.next_retry_mono - family_state.last_success_mono
        == pytest.approx(GRID_OUTAGE_CONTEXT_CACHE_TTL)
    )


@pytest.mark.asyncio
async def test_refresh_grid_outage_context_failure_marks_unknown_when_stale(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": False,
            "show_grid_connect": True,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    coord._grid_outage_context_last_success_mono = (
        time.monotonic() - 999
    )  # noqa: SLF001
    coord.client.off_grid_due_to_grid_outage = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    await coord.battery_runtime.async_refresh_grid_outage_context(force=True)

    assert coord.grid_outage_context_supported is None
    assert coord.grid_outage_is_grid_outage is None
    assert coord.grid_mode is None
    assert coord._grid_outage_context_failures == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_grid_outage_context_keeps_recent_state_on_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": False,
            "show_grid_connect": True,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    coord._grid_outage_context_last_success_mono = time.monotonic()  # noqa: SLF001
    coord.client.off_grid_due_to_grid_outage = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    await coord.battery_runtime.async_refresh_grid_outage_context(force=True)

    assert coord.grid_outage_context_supported is True
    assert coord.grid_mode == "off_grid"
    assert coord._grid_outage_context_failures == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_grid_outage_context_clears_when_endpoint_in_cooldown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": False,
            "show_grid_connect": True,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    coord._endpoint_family_should_run = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    coord._endpoint_family_state = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]  # noqa: SLF001
        cooldown_active=True
    )
    coord._endpoint_family_can_use_stale = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001

    await coord.battery_runtime.async_refresh_grid_outage_context()

    assert coord.grid_outage_context_supported is None
    assert coord.grid_outage_is_grid_outage is None


@pytest.mark.asyncio
async def test_refresh_grid_outage_context_missing_fetcher_clears_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": True,
            "show_grid_connect": False,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    coord.client.off_grid_due_to_grid_outage = None

    await coord.battery_runtime.async_refresh_grid_outage_context(force=True)

    assert coord.grid_outage_context_supported is None
    assert coord.grid_mode is None


@pytest.mark.asyncio
async def test_refresh_grid_outage_context_wraps_non_dict_redaction(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.off_grid_due_to_grid_outage = AsyncMock(
        return_value={
            "is_grid_outage": False,
            "show_grid_connect": True,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    coord._redact_battery_payload = lambda _payload: "masked"  # type: ignore[method-assign]  # noqa: SLF001

    await coord.battery_runtime.async_refresh_grid_outage_context(force=True)

    assert coord._grid_outage_context_payload == {"value": "masked"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_grid_control_check_failure_marks_unknown_when_stale(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._grid_control_check_last_success_mono = time.monotonic() - 999  # noqa: SLF001
    coord.client.grid_control_check = AsyncMock(side_effect=RuntimeError("boom"))

    await coord.battery_runtime.async_refresh_grid_control_check(force=True)

    assert coord.grid_control_supported is None
    assert coord.grid_control_disable is None
    assert coord.grid_toggle_allowed is None
    assert coord._grid_control_check_failures == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_grid_control_check_failure_keeps_recent_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._grid_control_check_last_success_mono = time.monotonic()  # noqa: SLF001
    coord.client.grid_control_check = AsyncMock(side_effect=RuntimeError("boom"))

    await coord.battery_runtime.async_refresh_grid_control_check(force=True)

    assert coord.grid_control_supported is True
    assert coord.grid_toggle_allowed is True
    assert coord._grid_control_check_failures == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_update_data_ignores_grid_control_refresh_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.site_only = True
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )

    result = await coord._async_update_data()  # noqa: SLF001

    assert result == {}

    coord = coordinator_factory()
    coord.client.status = AsyncMock(return_value={"evChargerData": [], "ts": 0})
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )

    await coord._async_update_data()  # noqa: SLF001


def test_grid_control_staleness_and_support_properties(coordinator_factory) -> None:
    coord = coordinator_factory()

    assert coord._grid_control_is_stale() is True  # noqa: SLF001

    coord._grid_control_supported = True  # noqa: SLF001
    coord._grid_control_check_last_success_mono = time.monotonic() + 5  # noqa: SLF001
    assert coord._grid_control_is_stale() is False  # noqa: SLF001

    coord._grid_control_check_last_success_mono = time.monotonic() - 999  # noqa: SLF001
    assert coord.grid_control_supported is None


def test_collect_site_metrics_includes_grid_control_fields(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": True,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )

    metrics = coord.collect_site_metrics()

    assert metrics["grid_control_supported"] is True
    assert metrics["grid_toggle_allowed"] is False
    assert metrics["grid_toggle_pending"] is False
    assert metrics["grid_toggle_blocked_reasons"] == ["active_download"]
    assert metrics["grid_control_data_stale"] is False
    assert metrics["grid_control_fetch_failures"] == 0


def test_collect_site_metrics_includes_grid_control_last_success_age(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._grid_control_check_last_success_mono = time.monotonic() - 1.0  # noqa: SLF001

    metrics = coord.collect_site_metrics()

    assert "grid_control_last_success_age_s" in metrics
    assert isinstance(metrics["grid_control_last_success_age_s"], float)


def test_grid_mode_status_staleness_and_metrics(coordinator_factory) -> None:
    coord = coordinator_factory()

    assert coord._grid_mode_status_is_stale() is True  # noqa: SLF001

    coord.battery_runtime.parse_grid_mode_status_payload(
        {"meters": {"gridRelay": "OPER_RELAY_CLOSED"}}
    )
    coord._grid_mode_status_last_success_mono = time.monotonic() + 5  # noqa: SLF001
    assert coord._grid_mode_status_is_stale() is False  # noqa: SLF001

    coord._grid_mode_status_last_success_mono = time.monotonic() - 999  # noqa: SLF001
    assert coord.grid_mode_status_supported is None

    coord._grid_mode_status_last_success_mono = time.monotonic() - 1  # noqa: SLF001
    metrics = coord.collect_site_metrics()
    assert "grid_mode_status_last_success_age_s" in metrics
    assert isinstance(metrics["grid_mode_status_last_success_age_s"], float)


def test_grid_mode_uses_grid_outage_context(coordinator_factory) -> None:
    coord = coordinator_factory()
    assert coord._normalize_grid_mode_value(None) is None  # noqa: SLF001

    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": False,
            "show_grid_connect": True,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    assert coord.grid_mode_raw_states == [
        "is_grid_outage:false",
        "show_grid_connect:true",
    ]
    assert coord.grid_mode == "off_grid"
    assert coord.grid_mode_source == "grid_outage_context"

    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": False,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    assert coord.grid_mode_raw_states == ["is_grid_outage:false"]
    assert coord.grid_mode == "on_grid"
    assert coord.grid_mode_source == "grid_outage_context"

    coord.battery_runtime.parse_grid_mode_status_payload(
        {
            "meters": {"gridRelay": "OPER_RELAY_CLOSED"},
        }
    )
    assert coord.grid_mode_raw_states == [
        "grid_relay:OPER_RELAY_CLOSED",
        "is_grid_outage:false",
    ]
    assert coord.grid_mode == "on_grid"
    assert coord.grid_mode_source == "livestream_grid_relay"

    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "data": {
                "is_grid_outage": True,
                "show_grid_connect": False,
                "has_battery": True,
                "is_sunlight_backup": False,
            }
        }
    )
    assert coord.grid_mode_raw_states == [
        "grid_relay:OPER_RELAY_CLOSED",
        "is_grid_outage:true",
        "show_grid_connect:false",
    ]
    assert coord.grid_mode == "on_grid"

    coord.battery_runtime.parse_grid_mode_status_payload(
        {"meters": {"gridRelay": "OPER_RELAY_OPEN"}}
    )
    assert coord.grid_mode == "off_grid"
    assert coord.grid_mode_source == "livestream_grid_relay"

    coord.battery_runtime.parse_grid_mode_status_payload({})
    assert coord.grid_mode_status_supported is False

    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": False,
            "show_grid_connect": False,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    assert coord.grid_mode_raw_states == [
        "is_grid_outage:false",
        "show_grid_connect:false",
    ]
    assert coord.grid_mode == "on_grid"

    coord.battery_runtime.parse_grid_outage_context_payload({})
    assert coord.grid_mode_raw_states == []
    assert coord.grid_mode is None

    coord.data = {
        "A": {"off_grid_state": "ON_GRID"},
        "B": {"off_grid_state": "ON_GRID"},
    }
    assert coord.grid_mode is None
    assert coord.grid_mode_raw_states == []


def test_grid_mode_keeps_live_relay_status_when_site_lacks_enpower_hint(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_mode_status_payload(
        {"meters": {"gridRelay": "OPER_RELAY_CLOSED"}}
    )
    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": True,
            "show_grid_connect": False,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )

    coord._battery_has_enpower = False  # noqa: SLF001

    assert coord.grid_mode_status_supported is True
    assert coord.grid_mode_status == "on_grid"
    assert coord.grid_mode_status_raw == "OPER_RELAY_CLOSED"
    assert coord.grid_mode_raw_states == [
        "grid_relay:OPER_RELAY_CLOSED",
        "is_grid_outage:true",
        "show_grid_connect:false",
    ]
    assert coord.grid_mode == "on_grid"
    assert coord.grid_mode_source == "livestream_grid_relay"


@pytest.mark.parametrize(
    ("relay", "expected"),
    [
        ("OPER_RELAY_OPEN", "off_grid"),
        ("OPER_RELAY_OFFGRID_AC_GRID_PRESENT", "off_grid"),
        ("OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD", "off_grid"),
        ("OPER_RELAY_CLOSED", "on_grid"),
        ("OPER_RELAY_WAITING_TO_INITIALIZE_ON_GRID", "on_grid"),
    ],
)
def test_grid_mode_maps_live_relay_enum(
    coordinator_factory,
    relay: str,
    expected: str,
) -> None:
    coord = coordinator_factory()

    assert coord.battery_runtime.parse_grid_mode_status_payload(
        {"meters": {"gridRelay": relay}}
    )
    assert coord.grid_mode == expected


def test_grid_mode_parser_handles_nested_relay_shapes(coordinator_factory) -> None:
    coord = coordinator_factory()

    assert coord.battery_runtime._grid_relay_candidates("invalid") == []  # noqa: SLF001
    assert not coord.battery_runtime.parse_grid_mode_status_payload([])
    assert coord.battery_runtime.parse_grid_mode_status_payload(
        {
            "meters": [{"gridRelay": None}, {"gridRelay": "unexpected"}],
            "data": [
                {
                    "payload": {
                        "message": [
                            {"grid_relay": "OPER_RELAY_OFFGRID_AC_GRID_PRESENT"}
                        ]
                    }
                }
            ],
        }
    )
    assert coord.grid_mode == "off_grid"


def test_parse_grid_outage_context_payload_tracks_fields(coordinator_factory) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": "true",
            "show_grid_connect": "false",
            "has_battery": 1,
            "is_sunlight_backup": 0,
        }
    )

    assert coord.grid_outage_context_supported is True
    assert coord.grid_outage_is_grid_outage is True
    assert coord.grid_outage_show_grid_connect is False
    assert coord.grid_outage_has_battery is True
    assert coord.grid_outage_is_sunlight_backup is False

    coord.battery_runtime.parse_grid_outage_context_payload(["bad"])

    assert coord.grid_outage_context_supported is False
    assert coord.grid_outage_is_grid_outage is None


def test_grid_outage_context_metrics(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": True,
            "show_grid_connect": False,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    coord._grid_outage_context_last_success_mono = (
        time.monotonic() - 1.0
    )  # noqa: SLF001

    metrics = coord.collect_site_metrics()

    assert metrics["grid_outage_context_supported"] is True
    assert metrics["grid_outage_is_grid_outage"] is True
    assert metrics["grid_outage_show_grid_connect"] is False
    assert metrics["grid_outage_context_fetch_failures"] == 0
    assert metrics["grid_outage_context_data_stale"] is False
    assert "grid_outage_context_last_success_age_s" in metrics


def test_grid_outage_context_refresh_due_uses_cooldown_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": False,
            "show_grid_connect": True,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    coord._endpoint_family_should_run = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    coord._endpoint_family_state = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]  # noqa: SLF001
        cooldown_active=True
    )
    coord._endpoint_family_can_use_stale = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001

    assert coord.battery_runtime.grid_outage_context_refresh_due() is True


def test_grid_outage_context_staleness_edges(coordinator_factory) -> None:
    coord = coordinator_factory()

    assert coord._grid_outage_context_is_stale() is True  # noqa: SLF001

    coord.battery_runtime.parse_grid_outage_context_payload(
        {
            "is_grid_outage": False,
            "show_grid_connect": True,
            "has_battery": True,
            "is_sunlight_backup": False,
        }
    )
    coord._grid_outage_context_last_success_mono = time.monotonic() + 5  # noqa: SLF001
    assert coord._grid_outage_context_is_stale() is False  # noqa: SLF001

    coord._grid_outage_context_last_success_mono = (
        time.monotonic() - 999
    )  # noqa: SLF001
    assert coord.grid_outage_context_supported is None


def test_grid_mode_legacy_normalizer_is_retained(coordinator_factory) -> None:
    coord = coordinator_factory()

    assert coord._normalize_grid_mode_value("OFF_GRID") == "off_grid"  # noqa: SLF001
    assert coord._normalize_grid_mode_value("ON_GRID") == "on_grid"  # noqa: SLF001
    assert coord._normalize_grid_mode_value("unexpected") is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_coordinator_refresh_grid_outage_context_proxy(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.async_refresh_grid_outage_context = AsyncMock()  # type: ignore[method-assign]

    await coord._async_refresh_grid_outage_context(force=True)  # noqa: SLF001

    coord.battery_runtime.async_refresh_grid_outage_context.assert_awaited_once_with(
        force=True
    )


@pytest.mark.asyncio
async def test_async_request_grid_toggle_otp_success(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord.client.request_grid_toggle_otp = AsyncMock(
        return_value={"success": "email sent successfully"}
    )
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001

    await coord.battery_runtime.async_request_grid_toggle_otp()

    coord.client.request_grid_toggle_otp.assert_awaited_once()
    coord.battery_runtime.async_refresh_grid_control_check.assert_awaited_once_with(
        force=True
    )


@pytest.mark.asyncio
async def test_async_request_grid_toggle_otp_blocked_or_unsupported(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._grid_control_supported = None  # noqa: SLF001
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(ServiceValidationError):
        await coord.async_request_grid_toggle_otp()

    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": True,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_request_grid_toggle_otp()


@pytest.mark.asyncio
async def test_async_request_grid_toggle_otp_client_paths(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001

    coord.client.request_grid_toggle_otp = None
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_request_grid_toggle_otp()

    coord.client.request_grid_toggle_otp = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_request_grid_toggle_otp()


@pytest.mark.asyncio
async def test_async_set_grid_mode_success_logs_and_refreshes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(  # noqa: SLF001
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "122447007044"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord.client.set_grid_state = AsyncMock(return_value={"request_id": "x"})
    coord.client.log_grid_change = AsyncMock(return_value={"status": "ok"})
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()

    await coord.battery_runtime.async_set_grid_mode("off_grid", "1234")

    coord.client.validate_grid_toggle_otp.assert_awaited_once_with("1234")
    coord.client.set_grid_state.assert_awaited_once_with("122447007044", 1)
    coord.client.log_grid_change.assert_awaited_once_with(
        "122447007044",
        "OPER_RELAY_CLOSED",
        "OPER_RELAY_OFFGRID_AC_GRID_PRESENT",
    )
    coord.async_request_refresh.assert_awaited_once()
    assert coord.battery_runtime.async_refresh_grid_control_check.await_count == 2


@pytest.mark.asyncio
async def test_async_set_grid_mode_best_effort_log_failure(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "122447007044"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord.client.set_grid_state = AsyncMock(return_value={"request_id": "x"})
    coord.client.log_grid_change = AsyncMock(side_effect=RuntimeError("nope"))
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()

    await coord.battery_runtime.async_set_grid_mode("on_grid", "1234")

    coord.client.set_grid_state.assert_awaited_once_with("122447007044", 2)
    coord.client.log_grid_change.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_set_grid_mode_setter_failure_raises_validation(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [{"serial_number": "122447007044"}],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord.client.set_grid_state = AsyncMock(side_effect=RuntimeError("setter down"))
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()

    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_mode("on_grid", "1234")


@pytest.mark.asyncio
async def test_async_set_grid_mode_validation_paths(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {"count": 1, "devices": [{"serial_number": "122447007044"}]}
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord.client.set_grid_state = AsyncMock(return_value={"request_id": "x"})
    coord.client.log_grid_change = AsyncMock(return_value={"status": "ok"})
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()

    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_mode("bad", "1234")
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_mode("off_grid", "")
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_mode("off_grid", "12ab")

    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=False)
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_mode("off_grid", "1234")

    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord._type_device_buckets = {
        "envoy": {"count": 1, "devices": [{}]}
    }  # noqa: SLF001
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_mode("off_grid", "1234")


@pytest.mark.asyncio
async def test_async_set_grid_connection_maps_bool_and_requires_otp(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.battery_runtime.async_set_grid_mode = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_connection(True)

    await coord.battery_runtime.async_set_grid_connection(True, otp="1234")
    coord.battery_runtime.async_set_grid_mode.assert_awaited_once_with(
        "on_grid", "1234"
    )


@pytest.mark.asyncio
async def test_async_set_grid_mode_additional_error_paths(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.battery_runtime.parse_grid_control_check_payload(
        {
            "disableGridControl": False,
            "activeDownload": False,
            "sunlightBackupSystemCheck": False,
            "gridOutageCheck": False,
            "userInitiatedGridToggle": False,
        }
    )
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {"count": 1, "devices": [{"serial_number": "122447007044"}]}
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.battery_runtime.async_refresh_grid_control_check = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001

    class _BadStr:
        def __str__(self):
            raise RuntimeError("bad string")

    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_mode(_BadStr(), "1234")

    coord.client.validate_grid_toggle_otp = None
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_mode("off_grid", "1234")

    coord.client.validate_grid_toggle_otp = AsyncMock(side_effect=RuntimeError("nope"))
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_mode("off_grid", "1234")

    coord.client.validate_grid_toggle_otp = AsyncMock(return_value=True)
    coord.client.set_grid_state = None
    with pytest.raises(ServiceValidationError):
        await coord.battery_runtime.async_set_grid_mode("off_grid", "1234")

    with pytest.raises(ServiceValidationError, match="bad mode"):
        coord._raise_grid_validation(
            "grid_mode_invalid", message="bad mode"
        )  # noqa: SLF001


def test_grid_envoy_serial_edge_paths(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.inventory_view.type_bucket = lambda _key: None  # type: ignore[method-assign]
    assert coord._grid_envoy_serial() is None  # noqa: SLF001

    coord.inventory_view.type_bucket = lambda _key: {"devices": "bad"}  # type: ignore[method-assign]
    assert coord._grid_envoy_serial() is None  # noqa: SLF001

    coord.inventory_view.type_bucket = lambda _key: {"devices": ["bad"]}  # type: ignore[method-assign]
    assert coord._grid_envoy_serial() is None  # noqa: SLF001
