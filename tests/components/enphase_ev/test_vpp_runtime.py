"""Tests for the read-only VPP/ELRP runtime."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.enphase_ev.api import (
    EnphaseLoginWallUnauthorized,
    OptionalEndpointUnavailable,
    Unauthorized,
)
from custom_components.enphase_ev.const import OPT_VPP_EVENTS_ENABLED
from custom_components.enphase_ev.state_models import EndpointFamilyHealth
from custom_components.enphase_ev.vpp_runtime import (
    VPP_EVENTS_ENDPOINT_FAMILY,
    VppRuntime,
    _datetime,
    _normalized,
    _text,
    parse_vpp_events,
)

ENROLLMENT_ID = "a" * 24
PROGRAM_ID = "b" * 24


class _BadString:
    def __str__(self) -> str:
        raise ValueError


def _event(
    *,
    event_id: str = "private-event-id",
    status: str = "scheduled",
    event_type: str = "battery_discharge",
    subtype: str = "Discharge_To_Load_Grid",
    start: datetime | None = None,
    end: datetime | None = None,
    **extra: object,
) -> dict[str, object]:
    start = start or datetime.now(UTC) + timedelta(hours=1)
    end = end or start + timedelta(hours=2)
    return {
        "id": event_id,
        "event_id": f"uuid-{event_id}",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "type": event_type,
        "subtype": subtype,
        "status": status,
        **extra,
    }


def _coordinator(*, events: object | None = None) -> SimpleNamespace:
    health: dict[str, EndpointFamilyHealth] = {}

    def state(family: str) -> EndpointFamilyHealth:
        return health.setdefault(family, EndpointFamilyHealth())

    def success(family: str) -> None:
        item = state(family)
        item.consecutive_failures = 0
        item.support_state = "supported"

    def failure(family: str, err: Exception) -> bool:
        item = state(family)
        item.consecutive_failures += 1
        item.last_error = str(err)
        return True

    coord = SimpleNamespace(
        config_entry=SimpleNamespace(options={OPT_VPP_EVENTS_ENABLED: True}),
        site_id="1234567",
        client=SimpleNamespace(
            vpp_enrollment_id=AsyncMock(return_value={"data": ENROLLMENT_ID}),
            vpp_enrollment_details=AsyncMock(
                return_value={"data": {"program_id": PROGRAM_ID}}
            ),
            vpp_events=AsyncMock(
                return_value={"data": []} if events is None else events
            ),
        ),
        _endpoint_family_should_run=MagicMock(return_value=True),
        _endpoint_family_state=state,
        _note_endpoint_family_success=MagicMock(side_effect=success),
        _note_endpoint_family_failure=MagicMock(side_effect=failure),
    )
    return coord


@pytest.mark.asyncio
async def test_runtime_is_default_off_and_makes_no_vpp_requests() -> None:
    coord = _coordinator()
    coord.config_entry.options = {}
    runtime = VppRuntime(coord)

    await runtime.async_refresh()

    coord.client.vpp_enrollment_id.assert_not_awaited()
    coord.client.vpp_enrollment_details.assert_not_awaited()
    coord.client.vpp_events.assert_not_awaited()
    assert runtime.snapshot.enrollment_state == "disabled"


def test_parse_vpp_events_preserves_statuses_and_redacts_identity() -> None:
    start = datetime(2026, 8, 4, 1, tzinfo=UTC)
    payload = {
        "data": [
            _event(start=start, end=start + timedelta(hours=1)),
            _event(
                event_id="cancelled-private",
                status="cancelled",
                event_type="battery_charge",
                subtype="Charge_From_PV_Grid",
                start=start + timedelta(hours=1),
                end=start + timedelta(hours=2),
                cancellation_timestamp="2026-08-03T22:00:00+00:00",
            ),
            _event(
                event_id="superseded-private",
                status="mystery",
                start=start + timedelta(hours=2),
                end=start + timedelta(hours=3),
                superceded=True,
            ),
            {"start_time": "invalid", "end_time": "invalid"},
            "invalid-row",
        ]
    }

    parsed = parse_vpp_events(payload)

    assert parsed is not None
    events, truncated = parsed
    assert truncated is False
    assert [item.status for item in events] == ["scheduled", "cancelled", "mystery"]
    assert events[1].cancelled is True
    assert events[2].superseded is True
    assert "private" not in repr(events)
    assert parse_vpp_events({"data": {}}) is None
    assert parse_vpp_events(None) is None


def test_vpp_scalar_normalizers_reject_unsafe_values() -> None:
    assert _text(_BadString()) is None
    assert _text("   ") is None
    assert _normalized(None) == ""
    assert _datetime(None) is None


def test_parse_vpp_events_deduplicates_and_bounds(monkeypatch) -> None:
    from custom_components.enphase_ev import vpp_runtime

    monkeypatch.setattr(vpp_runtime, "VPP_EVENT_LIMIT", 1)
    first = _event(event_id="same")
    duplicate = {**first, "status": "updated"}
    second = _event(event_id="other", start=datetime.now(UTC) + timedelta(hours=4))

    parsed = parse_vpp_events({"data": [first, duplicate, second]})

    assert parsed is not None
    events, truncated = parsed
    assert len(events) == 1
    assert truncated is True


def test_parse_vpp_events_trims_oldest_history_before_upcoming(monkeypatch) -> None:
    from custom_components.enphase_ev import vpp_runtime

    monkeypatch.setattr(vpp_runtime, "VPP_EVENT_LIMIT", 3)
    now = datetime.now(UTC)
    history = []
    for index in range(3):
        start = now - timedelta(days=4 - index)
        history.append(
            _event(
                event_id=f"history-{index}",
                status="completed",
                start=start,
                end=start + timedelta(hours=1),
            )
        )
    upcoming = _event(
        event_id="upcoming",
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
    )
    cancelled_upcoming = _event(
        event_id="cancelled-upcoming",
        status="cancelled",
        start=now + timedelta(hours=3),
        end=now + timedelta(hours=4),
    )

    parsed = parse_vpp_events({"data": [*history, upcoming, cancelled_upcoming]})

    assert parsed is not None
    events, truncated = parsed
    assert truncated is True
    assert len(events) == 3
    assert events[0].start == now - timedelta(days=2)
    retained_upcoming = next(
        event for event in events if event.start == now + timedelta(hours=1)
    )
    assert retained_upcoming.actionable(now) is True
    assert any(event.status == "cancelled" for event in events)


@pytest.mark.asyncio
async def test_runtime_resolves_single_program_and_refreshes_events() -> None:
    coord = _coordinator(events={"data": [_event()]})
    runtime = VppRuntime(coord)

    await runtime.async_refresh()

    assert runtime.enrollment_state == "enrolled"
    assert runtime.available is True
    assert len(runtime.events) == 1
    assert runtime.refresh_due() is True
    assert runtime.next_actionable() == runtime.events[0]
    coord.client.vpp_enrollment_details.assert_awaited_once_with(ENROLLMENT_ID)
    coord.client.vpp_events.assert_awaited_once_with(PROGRAM_ID)
    assert runtime.diagnostics()["actionable_count"] == 1
    assert ENROLLMENT_ID not in repr(runtime.diagnostics())
    assert PROGRAM_ID not in repr(runtime.diagnostics())

    coord._endpoint_family_should_run.side_effect = (
        lambda family: family == VPP_EVENTS_ENDPOINT_FAMILY
    )
    await runtime.async_refresh()
    assert coord.client.vpp_enrollment_id.await_count == 1
    assert coord.client.vpp_events.await_count == 2

    runtime._program_last_confirmed_mono = time.monotonic() - 604801  # noqa: SLF001
    coord._endpoint_family_should_run.side_effect = None
    coord._endpoint_family_should_run.return_value = True
    assert runtime.refresh_due() is True
    assert runtime._program_id is None  # noqa: SLF001
    assert runtime.enrollment_state == "unknown"


@pytest.mark.asyncio
async def test_runtime_handles_unenrolled_empty_and_ambiguous_responses() -> None:
    coord = _coordinator()
    runtime = VppRuntime(coord)

    coord.client.vpp_enrollment_id.return_value = {"data": None}
    await runtime.async_refresh()
    assert runtime.enrollment_state == "unenrolled"
    assert runtime.available is False
    coord.client.vpp_enrollment_details.assert_not_awaited()
    coord.client.vpp_events.assert_not_awaited()

    coord.client.vpp_enrollment_id.return_value = {"data": [ENROLLMENT_ID]}
    await runtime.async_refresh()
    assert runtime.enrollment_state == "ambiguous"
    assert runtime.available is False
    assert coord._note_endpoint_family_failure.call_count == 1

    coord.client.vpp_enrollment_id.return_value = {"data": ENROLLMENT_ID}
    coord.client.vpp_enrollment_details.return_value = {"data": {}}
    await runtime.async_refresh()
    assert runtime.enrollment_state == "ambiguous"
    assert coord._note_endpoint_family_failure.call_count == 2


@pytest.mark.asyncio
async def test_runtime_empty_events_are_available_and_terminal_events_not_next() -> (
    None
):
    coord = _coordinator(events={"data": []})
    runtime = VppRuntime(coord)
    await runtime.async_refresh()

    assert runtime.available is True
    assert runtime.events == ()
    assert runtime.next_actionable() is None

    coord.client.vpp_events.return_value = {
        "data": [
            _event(status="completed"),
            _event(event_id="failed", status="failed"),
            _event(event_id="unknown", status="new_future_status"),
        ]
    }
    await runtime._async_refresh_events()  # noqa: SLF001
    assert runtime.next_actionable() is not None
    assert runtime.next_actionable().status == "new_future_status"


@pytest.mark.asyncio
async def test_runtime_retains_stale_events_and_reresolves_invalid_program() -> None:
    coord = _coordinator(events={"data": [_event()]})
    runtime = VppRuntime(coord)
    await runtime.async_refresh()
    original = runtime.events

    coord.client.vpp_events.side_effect = RuntimeError("temporary outage")
    await runtime._async_refresh_events()  # noqa: SLF001
    assert runtime.events == original
    assert runtime.available is True

    runtime._events_last_success_mono = time.monotonic() - 3601  # noqa: SLF001
    assert runtime.available is False
    runtime._events_last_success_mono = time.monotonic()  # noqa: SLF001

    request_info = SimpleNamespace(real_url="https://gs.invalid/private")
    coord.client.vpp_events.side_effect = aiohttp.ClientResponseError(
        request_info=request_info,
        history=(),
        status=404,
        message="private program rejected",
    )
    await runtime._async_refresh_events()  # noqa: SLF001

    assert runtime.events == original
    assert runtime.available is False
    assert runtime.enrollment_state == "unknown"
    assert runtime._program_id is None  # noqa: SLF001
    assert runtime.refresh_due() is True
    failure = coord._note_endpoint_family_failure.call_args.args[1]
    assert isinstance(failure, OptionalEndpointUnavailable)
    assert "private" not in str(failure)

    coord._endpoint_family_should_run.return_value = False
    coord.client.vpp_events.side_effect = None
    coord.client.vpp_events.return_value = {"data": []}
    await runtime.async_refresh()

    assert coord.client.vpp_enrollment_id.await_count == 2
    assert runtime._program_id == PROGRAM_ID  # noqa: SLF001


@pytest.mark.asyncio
async def test_runtime_disabled_is_inert_and_clear_expires_availability() -> None:
    coord = _coordinator(events={"data": [_event()]})
    runtime = VppRuntime(coord)
    await runtime.async_refresh()
    coord.config_entry.options[OPT_VPP_EVENTS_ENABLED] = False

    await runtime.async_refresh()

    assert runtime.snapshot.enrollment_state == "disabled"
    assert runtime.events == ()
    assert runtime.refresh_due() is False
    assert runtime.diagnostics()["enabled"] is False

    coord.config_entry.options[OPT_VPP_EVENTS_ENABLED] = True
    assert runtime.refresh_due() is True
    assert runtime.next_actionable() is None


@pytest.mark.asyncio
async def test_runtime_handles_missing_fetchers_and_invalid_event_shape() -> None:
    coord = _coordinator()
    runtime = VppRuntime(coord)
    coord.client.vpp_enrollment_id = None
    with pytest.raises(OptionalEndpointUnavailable):
        await runtime._async_refresh_enrollment()  # noqa: SLF001

    runtime._program_id = PROGRAM_ID  # noqa: SLF001
    coord.client.vpp_events = AsyncMock(return_value={"data": {}})
    await runtime._async_refresh_events()  # noqa: SLF001
    assert coord._note_endpoint_family_failure.called

    coord.client.vpp_enrollment_id = AsyncMock(return_value={"unexpected": True})
    coord.client.vpp_enrollment_details = AsyncMock()
    await runtime._async_refresh_enrollment()  # noqa: SLF001

    runtime._program_id = PROGRAM_ID  # noqa: SLF001
    coord.client.vpp_events = None
    with pytest.raises(OptionalEndpointUnavailable):
        await runtime._async_refresh_events()  # noqa: SLF001


@pytest.mark.asyncio
async def test_runtime_propagates_authentication_failures() -> None:
    coord = _coordinator()
    runtime = VppRuntime(coord)
    coord.client.vpp_enrollment_id.side_effect = Unauthorized()

    with pytest.raises(ConfigEntryAuthFailed):
        await runtime._async_refresh_enrollment()  # noqa: SLF001

    runtime._program_id = PROGRAM_ID  # noqa: SLF001
    coord.client.vpp_events.side_effect = Unauthorized()
    with pytest.raises(ConfigEntryAuthFailed):
        await runtime._async_refresh_events()  # noqa: SLF001


@pytest.mark.asyncio
async def test_runtime_isolates_optional_grid_services_login_walls() -> None:
    coord = _coordinator(events={"data": [_event()]})
    runtime = VppRuntime(coord)
    await runtime.async_refresh()
    original = runtime.events
    login_wall = EnphaseLoginWallUnauthorized(
        endpoint="/login",
        request_label="GET /login",
        status=200,
        content_type="text/html",
    )

    coord.client.vpp_enrollment_id.side_effect = login_wall
    await runtime._async_refresh_enrollment()  # noqa: SLF001

    coord.client.vpp_events.side_effect = login_wall
    await runtime._async_refresh_events()  # noqa: SLF001

    assert runtime.events == original
    assert runtime.available is True
    failures = [
        call.args[1] for call in coord._note_endpoint_family_failure.call_args_list
    ]
    assert all(isinstance(failure, OptionalEndpointUnavailable) for failure in failures)
    assert all("/login" not in str(failure) for failure in failures)
