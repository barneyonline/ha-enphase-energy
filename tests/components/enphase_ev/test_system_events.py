"""Tests for read-only System Dashboard event monitoring."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.enphase_ev.api import (
    EnphaseLoginWallUnauthorized,
    OptionalEndpointUnavailable,
)
from custom_components.enphase_ev.const import DOMAIN, OPT_SYSTEM_EVENT_REPAIR_ISSUES
from custom_components.enphase_ev.system_events import (
    ACTIVE_EVENTS_ATTRIBUTE_LIMIT,
    SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY,
    SYSTEM_EVENTS_ENDPOINT_FAMILY,
    SYSTEM_EVENT_REPAIR_CHECKPOINT_INTERVAL,
    SYSTEM_EVENT_REPAIR_MISSING_GRACE,
    SYSTEM_EVENT_REPAIR_PREFIX,
    SystemEventsRuntime,
    _catalog_label,
    _event_fingerprint,
    _event_is_active,
    _event_is_informational,
    _history_epoch,
    _lookup_catalog,
    _normalized,
    _text,
    _timestamp,
    parse_active_system_events,
    parse_homeowner_event_history,
    parse_standing_alarms,
)


class _BadText:
    def __str__(self) -> str:
        raise ValueError("boom")


def _event_payload() -> dict[str, object]:
    return {
        "events": [
            {
                "id": None,
                "serial_number": "SERIAL-PRIVATE-1",
                "event_date": "2026-07-11T01:02:03Z",
                "updated_at": "2026-07-11T01:03:03Z",
                "cleared_date": None,
                "device_type": "IQ Gateway SERIAL-PRIVATE-1",
                "event_type": 10,
                "event_state": "open",
            },
            {
                "id": "closed-event",
                "serial_number": "SERIAL-PRIVATE-2",
                "event_date": "2026-07-11T00:00:00Z",
                "cleared_date": "2026-07-11T00:05:00Z",
                "device_type": "IQ Battery",
                "event_type": "Battery warning",
                "event_state": "open",
                "severity": "warning",
            },
            {
                "id": "resolved-event",
                "event_date": "2026-07-11T00:00:00Z",
                "cleared_date": None,
                "event_type": "Resolved event",
                "event_state": 2,
                "severity": "critical",
            },
            "not-a-row",
        ],
        "event_types": [
            {"id": 10, "name": "Gateway SERIAL-PRIVATE-1 fault", "severity_id": 5},
            None,
        ],
        "event_states": [
            {"id": 2, "name": "Resolved"},
            "not-a-state",
        ],
        "event_severities": [{"id": 5, "name": "Critical"}],
    }


def _standing_alarm_payload() -> dict[str, object]:
    return {
        "alarms": [
            {
                "id": "1234567.440.1770000000000",
                "severity": 4,
                "type": "Gateway SERIAL-PRIVATE-1",
                "serial_num": "SERIAL-PRIVATE-1",
                "device_link": "https://example.invalid/private-device",
                "description": "Private diagnostic details",
                "first_set": "2026/07/11 01:02:03 +0000 (UTC)",
            },
            {
                "id": "1234567.440.1770000000000",
                "severity": 4,
                "type": "duplicate",
            },
            "not-an-alarm",
        ]
    }


def test_event_parser_normalizes_redacts_and_filters() -> None:
    events = parse_active_system_events(_event_payload(), site_id="1234567")

    assert len(events) == 1
    event = events[0]
    assert len(event.fingerprint) == 16
    assert event.event_type == "Gateway SERI...TE-1 fault"
    assert event.device_type == "IQ Gateway SERI...TE-1"
    assert event.severity == "critical"
    assert event.state == "open"
    assert event.high_impact is True
    assert event.event_date == "2026-07-11T01:02:03+00:00"
    assert event.updated_at == "2026-07-11T01:03:03+00:00"
    assert "SERIAL-PRIVATE-1" not in repr(event)


def test_event_parser_handles_malformed_and_direct_severity_shapes() -> None:
    assert parse_active_system_events(None, site_id="1") == ()
    assert parse_active_system_events({"events": {}}, site_id="1") == ()

    payload = {
        "events": [
            {
                "id": "warning",
                "event_type": "Warning",
                "event_state": None,
                "event_severity": "Warning",
            },
            {
                "alarm_id": "alarm",
                "event_type": "Severe",
                "event_state": "set",
                "severity": "Severe",
            },
            {
                "event_type": _BadText(),
                "event_date": "date",
                "event_state": "closed",
            },
        ],
        "event_types": {},
        "event_states": None,
        "event_severities": [None, {"id": "warn", "name": "Warning"}],
    }
    events = parse_active_system_events(payload, site_id="1")

    assert [event.severity for event in events] == ["warning", "severe"]
    assert [event.high_impact for event in events] == [False, True]
    assert events[0].state == "unknown"
    assert events[1].device_type == "unknown"

    duplicate_payload = {
        "events": [
            {"id": "same", "event_type": "Unclassified", "event_state": "open"},
            {"id": "same", "event_type": "Duplicate", "event_state": "open"},
        ]
    }
    duplicate_events = parse_active_system_events(duplicate_payload, site_id="1")
    assert len(duplicate_events) == 1
    assert duplicate_events[0].severity == "unknown"

    state_severity = parse_active_system_events(
        {"events": [{"id": "state", "event_state": "Error"}]},
        site_id="1",
    )
    assert state_severity[0].high_impact is True


def test_event_parser_excludes_explicit_informational_rows() -> None:
    payload = {
        "events": [
            {
                "id": "severity-info",
                "event_type": "Routine update",
                "event_state": "open",
                "event_severity": 1,
            },
            {
                "id": "type-info",
                "event_type": "Informational",
                "event_state": "open",
            },
            {
                "id": "state-info",
                "event_type": "Routine update",
                "event_state": "Info",
                "severity": "critical",
            },
            {
                "id": "warning",
                "event_type": "Warning",
                "event_state": "open",
                "severity": "warning",
            },
            {
                "id": "critical",
                "event_type": "Critical fault",
                "event_state": "open",
                "severity": "critical",
            },
        ],
        "event_severities": [{"id": 1, "name": "Info"}],
    }

    events = parse_active_system_events(payload, site_id="1")

    assert [event.event_type for event in events] == ["Warning", "Critical fault"]
    assert [event.high_impact for event in events] == [False, True]
    assert _event_is_informational(
        {}, severity="informational", event_type="fault", state="open"
    )
    assert not _event_is_informational(
        {}, severity="warning", event_type="Information unavailable", state="open"
    )


def test_homeowner_history_parser_normalizes_redacts_and_preserves_all_classes() -> (
    None
):
    payload = {
        "events": [
            {
                "id": "info-event",
                "status": "Info",
                "type": "IQ EV Charger",
                "description": (
                    "Charging started on IQ EV Charger "
                    "(SNo. EV1234567890) at site 1234567."
                ),
                "event_start_date": 1770000100,
                "event_clear_date": 1770000100,
                "serial_num": "EV1234567890",
                "devices_impacted": ["IQ EV Charger (SNo. EV1234567890)"],
                "event_key": "evse_start_charging",
                "recommended_action": "Check EV1234567890 if charging stops.",
                "message_params": "private mode details",
            },
            {
                "id": "warning-event",
                "status": "Warning",
                "type": "IQ Gateway",
                "event_date": 1770000000,
                "event_clear_date": 1770000060,
                "event_key": "gateway_warning",
            },
            {
                "id": "warning-event",
                "event_date": 1770000000,
                "description": "duplicate",
            },
            {"id": "invalid"},
            "invalid-row",
        ]
    }

    events = parse_homeowner_event_history(payload, site_id="1234567")

    assert events is not None
    assert len(events) == 2
    assert [event.summary for event in events] == [
        "IQ Gateway",
        "Charging started on IQ EV Charger (SNo. EV12...7890) at site [site].",
    ]
    assert events[0].end - events[0].start == timedelta(minutes=1)
    assert events[1].end - events[1].start == timedelta(minutes=1)
    assert events[1].description == "Check EV12...7890 if charging stops."
    assert "EV1234567890" not in repr(events)
    assert "private mode details" not in repr(events)
    assert parse_homeowner_event_history(None, site_id="1") is None
    assert parse_homeowner_event_history({"events": {}}, site_id="1") is None
    assert _history_epoch(True) is None
    assert _history_epoch("not-a-time") is None
    assert _history_epoch(1770000000000) == datetime.fromtimestamp(
        1770000000, tz=timezone.utc
    )

    fallback = parse_homeowner_event_history(
        {
            "events": [
                {
                    "event_key": "fallback_event_key",
                    "event_date": 1770000000,
                    "serial_num": "SERIAL-1",
                    "devices_impacted": [None, "IQ Device without serial"],
                }
            ]
        },
        site_id="1",
    )
    assert fallback is not None
    assert fallback[0].summary == "Fallback event key"

    no_summary = parse_homeowner_event_history(
        {"events": [{"event_date": 1770000000}]},
        site_id="1",
    )
    assert no_summary is not None
    assert no_summary[0].summary is None

    incomplete_metadata = parse_homeowner_event_history(
        {
            "events": [
                {
                    "event_date": 1770000000,
                    "description": "Device (SNo. EV1234567890) reported an event.",
                },
                {
                    "event_date": 1770000060,
                    "type": "Serial: TYPE1234567890",
                },
                {
                    "event_date": 1770000120,
                    "recommended_action": (
                        "Inspect serial number ACTION1234567890 before continuing."
                    ),
                },
            ]
        },
        site_id="1",
    )
    assert incomplete_metadata is not None
    assert len(incomplete_metadata) == 3
    assert all(
        identifier not in repr(incomplete_metadata)
        for identifier in (
            "EV1234567890",
            "TYPE1234567890",
            "ACTION1234567890",
        )
    )


def test_standing_alarm_parser_normalizes_redacts_and_deduplicates() -> None:
    alarms = parse_standing_alarms(_standing_alarm_payload(), site_id="1234567")

    assert len(alarms) == 1
    alarm = alarms[0]
    assert len(alarm.fingerprint) == 16
    assert alarm.severity == "4"
    assert alarm.device_type == "Gateway SERI...TE-1"
    assert alarm.first_set == "2026/07/11 01:02:03 +0000 (UTC)"
    assert "SERIAL-PRIVATE-1" not in repr(alarm)
    assert alarm.description == "Private diagnostic details"
    assert parse_standing_alarms(None, site_id="1") == ()
    assert parse_standing_alarms({"alarms": {}}, site_id="1") == ()

    fallback = parse_standing_alarms(
        {"alarms": [{"severity": None, "type": None}]},
        site_id="1",
    )
    assert fallback[0].severity == "unknown"
    assert fallback[0].device_type == "unknown"
    assert fallback[0].first_set is None


def test_event_helper_edge_cases() -> None:
    assert _text(None) is None
    assert _text([]) is None
    assert _text(_BadText()) is None
    assert _text("  hello\nworld ") == "hello world"
    assert _normalized(None) == ""
    assert _normalized("High-Impact Event") == "high_impact_event"
    assert _timestamp("2026-07-11T01:02:03Z") == "2026-07-11T01:02:03+00:00"
    assert _timestamp("2026-07-11T01:02:03") is None
    assert _timestamp("not-a-timestamp") is None
    assert _timestamp("x" * 65) is None
    assert _lookup_catalog({}) == {}
    assert _lookup_catalog([None, {"id": "", "name": "Open"}]) == {
        "open": {"id": "", "name": "Open"}
    }
    assert _catalog_label("missing", {}) == "missing"
    assert _catalog_label("open", {"open": {"name": None}}) == "open"
    assert _event_is_active({"cleared_date": "now"}, "open") is False
    assert _event_is_active({"cleared_date": None}, "resolved") is False
    assert _event_is_active({"cleared_date": None}, "open") is True
    assert _event_fingerprint({"id": "event-id"}) == _event_fingerprint(
        {"id": "event-id", "serial_number": "different"}
    )


_DEFAULT_ALARMS = object()


def _runtime_coordinator(
    hass,
    *,
    payload: object = None,
    alarm_payload: object = _DEFAULT_ALARMS,
    history_payload: object | None = None,
    due: bool = True,
    entry_id: str | None = "Entry-ID",
    repairs_enabled: bool = True,
) -> SimpleNamespace:
    health = SimpleNamespace(consecutive_failures=0)
    record_failure = MagicMock(return_value=False)
    record_success = MagicMock()
    if alarm_payload is _DEFAULT_ALARMS:
        alarm_payload = _standing_alarm_payload()
    return SimpleNamespace(
        hass=hass,
        site_id="1234567",
        runtime_active=True,
        inventory_view=SimpleNamespace(),
        config_entry=(
            SimpleNamespace(
                entry_id=entry_id,
                options={OPT_SYSTEM_EVENT_REPAIR_ISSUES: repairs_enabled},
            )
            if entry_id is not None
            else None
        ),
        client=SimpleNamespace(
            system_dashboard_events=AsyncMock(return_value=payload),
            system_dashboard_standing_alarms=AsyncMock(return_value=alarm_payload),
            homeowner_events_page=AsyncMock(return_value=history_payload),
        ),
        _endpoint_family_should_run=lambda family: due,
        _endpoint_family_wait_active=lambda family: False,
        _endpoint_family_can_use_stale=lambda family: True,
        _endpoint_family_state=lambda family: health,
        _note_endpoint_family_failure=record_failure,
        note_endpoint_family_failure=record_failure,
        _note_endpoint_family_success=record_success,
        note_endpoint_family_success=record_success,
    )


@pytest.mark.asyncio
async def test_runtime_probes_homeowner_history_and_reports_safe_diagnostics(
    hass,
) -> None:
    coordinator = _runtime_coordinator(
        hass,
        history_payload={
            "events": [
                {
                    "id": "private-id",
                    "event_date": 1770000000,
                    "description": "Routine event",
                }
            ],
            "next": "private-cursor",
        },
    )
    runtime = SystemEventsRuntime(coordinator)

    await runtime.async_refresh_history()

    assert runtime.history_available is True
    assert runtime.history_refresh_due() is True
    coordinator.client.homeowner_events_page.assert_awaited_once_with(
        next_cursor="start",
        page_size=200,
        locale=hass.config.language,
    )
    coordinator._note_endpoint_family_success.assert_called_once_with(
        SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
    )
    diagnostics = runtime.history_diagnostics()
    assert diagnostics == {
        "available": True,
        "cached_range_count": 0,
        "cached_event_count": 0,
        "last_success_utc": runtime._history_last_success_utc.isoformat(),  # noqa: SLF001
        "using_cached_data": False,
        "truncated": False,
    }
    assert "private" not in repr(diagnostics)


@pytest.mark.asyncio
async def test_runtime_history_probe_handles_skip_and_optional_failures(hass) -> None:
    coordinator = _runtime_coordinator(hass, due=False)
    runtime = SystemEventsRuntime(coordinator)
    await runtime.async_refresh_history()
    coordinator.client.homeowner_events_page.assert_not_awaited()

    coordinator._endpoint_family_should_run = lambda family: True
    coordinator.client.homeowner_events_page = None
    await runtime.async_refresh_history()

    coordinator.client.homeowner_events_page = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    await runtime.async_refresh_history()
    coordinator._note_endpoint_family_failure.assert_called_once()

    coordinator.client.homeowner_events_page = AsyncMock(return_value=None)
    await runtime.async_refresh_history()
    assert coordinator._note_endpoint_family_failure.call_count == 2

    coordinator.client.homeowner_events_page = AsyncMock(
        side_effect=EnphaseLoginWallUnauthorized(
            endpoint="/events/homeowner",
            request_label="GET homeowner events",
        )
    )
    with pytest.raises(EnphaseLoginWallUnauthorized):
        await runtime.async_refresh_history()


@pytest.mark.asyncio
async def test_runtime_history_paginates_filters_and_caches_range(hass) -> None:
    coordinator = _runtime_coordinator(hass)
    coordinator.client.homeowner_events_page = AsyncMock(
        side_effect=[
            {
                "events": [
                    {
                        "id": "newer",
                        "event_date": 1770000300,
                        "description": "Newer informational event",
                    },
                    {
                        "id": "in-range",
                        "event_date": 1770000200,
                        "description": "In range warning event",
                    },
                ],
                "next": "private-next-cursor",
            },
            {
                "events": [
                    {
                        "id": "older",
                        "event_date": 1770000000,
                        "description": "Older event",
                    }
                ],
                "next": None,
            },
        ]
    )
    runtime = SystemEventsRuntime(coordinator)
    start = datetime.fromtimestamp(1770000100, tz=timezone.utc)
    end = datetime.fromtimestamp(1770000400, tz=timezone.utc)

    events = await runtime.async_history_events(start, end)
    cached_events = await runtime.async_history_events(start, end)

    assert [event.summary for event in events] == [
        "In range warning event",
        "Newer informational event",
    ]
    assert cached_events == events
    assert coordinator.client.homeowner_events_page.await_count == 2
    assert (
        coordinator.client.homeowner_events_page.await_args_list[1].kwargs[
            "next_cursor"
        ]
        == "private-next-cursor"
    )
    diagnostics = runtime.history_diagnostics()
    assert diagnostics["cached_range_count"] == 1
    assert diagnostics["cached_event_count"] == 2
    assert diagnostics["truncated"] is False
    assert "private-next-cursor" not in repr(
        runtime._history_range_cache
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_runtime_history_marks_repeated_cursor_and_row_cap_truncated(
    hass, monkeypatch
) -> None:
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.SYSTEM_EVENT_HISTORY_ROW_LIMIT",
        2,
    )
    coordinator = _runtime_coordinator(hass)
    coordinator.client.homeowner_events_page = AsyncMock(
        return_value={
            "events": [
                {"id": "1", "event_date": 1770000200},
                {"id": "2", "event_date": 1770000100},
            ],
            "next": "more-private-data",
        }
    )
    runtime = SystemEventsRuntime(coordinator)

    events = await runtime.async_history_events(
        datetime.fromtimestamp(1769990000, tz=timezone.utc),
        datetime.fromtimestamp(1770010000, tz=timezone.utc),
    )

    assert len(events) == 2
    assert runtime.history_diagnostics()["truncated"] is True
    assert "more-private-data" not in repr(runtime.history_diagnostics())


@pytest.mark.asyncio
async def test_runtime_history_bounds_oversized_page(hass, monkeypatch) -> None:
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.SYSTEM_EVENT_HISTORY_ROW_LIMIT",
        2,
    )
    coordinator = _runtime_coordinator(hass)
    coordinator.client.homeowner_events_page = AsyncMock(
        return_value={
            "events": [
                {"id": "1", "event_date": 1770000200},
                {"id": "2", "event_date": 1770000100},
                {"id": "3", "event_date": 1770000000},
            ]
        }
    )
    runtime = SystemEventsRuntime(coordinator)

    events = await runtime.async_history_events(
        datetime.fromtimestamp(1769990000, tz=timezone.utc),
        datetime.fromtimestamp(1770010000, tz=timezone.utc),
    )

    assert len(events) == 2
    assert runtime.history_diagnostics()["truncated"] is True


@pytest.mark.asyncio
async def test_runtime_history_rejects_non_list_rows(hass) -> None:
    coordinator = _runtime_coordinator(hass, history_payload={"events": {}})
    runtime = SystemEventsRuntime(coordinator)

    assert (
        await runtime.async_history_events(
            datetime.fromtimestamp(1769990000, tz=timezone.utc),
            datetime.fromtimestamp(1770010000, tz=timezone.utc),
        )
        == ()
    )
    coordinator._note_endpoint_family_failure.assert_called_once()


@pytest.mark.asyncio
async def test_runtime_history_marks_page_cap_and_repeated_cursor_truncated(
    hass, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.SYSTEM_EVENT_HISTORY_MAX_PAGES",
        0,
    )
    await runtime.async_history_events(
        datetime.fromtimestamp(1769990000, tz=timezone.utc),
        datetime.fromtimestamp(1770010000, tz=timezone.utc),
    )
    assert runtime.history_diagnostics()["truncated"] is True

    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.SYSTEM_EVENT_HISTORY_MAX_PAGES",
        100,
    )
    runtime = SystemEventsRuntime(coordinator)
    coordinator.client.homeowner_events_page = AsyncMock(
        return_value={
            "events": [{"id": "1", "event_date": 1770000200}],
            "next": "start",
        }
    )
    await runtime.async_history_events(
        datetime.fromtimestamp(1770000000, tz=timezone.utc),
        datetime.fromtimestamp(1770010000, tz=timezone.utc),
    )
    assert runtime.history_diagnostics()["truncated"] is True


@pytest.mark.asyncio
async def test_runtime_history_cache_eviction_inner_hit_cooldown_and_missing_fetcher(
    hass, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    now = 1000.0
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.time.monotonic", lambda: now
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.SYSTEM_EVENT_HISTORY_CACHE_MAX_RANGES",
        1,
    )
    first_start = datetime.fromtimestamp(1770000000, tz=timezone.utc)
    first_end = datetime.fromtimestamp(1770000200, tz=timezone.utc)
    first_key = runtime._history_cache_key(first_start, first_end, "en")  # noqa: SLF001
    runtime._store_history_range(first_key, (), truncated=False)  # noqa: SLF001
    second_key = runtime._history_cache_key(  # noqa: SLF001
        first_start + timedelta(days=1),
        first_end + timedelta(days=1),
        "en",
    )
    runtime._store_history_range(second_key, (), truncated=False)  # noqa: SLF001
    assert list(runtime._history_range_cache) == [second_key]  # noqa: SLF001

    runtime._history_range_cache.clear()  # noqa: SLF001

    class _PopulateCacheLock:
        async def __aenter__(self):
            runtime._store_history_range(first_key, (), truncated=True)  # noqa: SLF001

        async def __aexit__(self, exc_type, exc, tb):
            return False

    runtime._history_lock = _PopulateCacheLock()  # type: ignore[assignment]  # noqa: SLF001
    assert await runtime.async_history_events(first_start, first_end) == ()
    assert runtime.history_diagnostics()["truncated"] is True

    runtime._history_lock = asyncio.Lock()  # noqa: SLF001
    now += 901
    coordinator._endpoint_family_state(
        SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
    ).consecutive_failures = 1
    coordinator._endpoint_family_wait_active = lambda family: True
    runtime._history_truncated = False  # noqa: SLF001
    assert await runtime.async_history_events(first_start, first_end) == ()
    assert runtime.history_diagnostics()["using_cached_data"] is True
    assert runtime.history_diagnostics()["truncated"] is True

    coordinator._endpoint_family_can_use_stale = lambda family: False
    runtime._history_last_success_utc = datetime.now(timezone.utc)  # noqa: SLF001
    runtime._history_truncated = True  # noqa: SLF001
    assert await runtime.async_history_events(first_start, first_end) == ()
    assert runtime.history_diagnostics()["using_cached_data"] is False
    assert runtime.history_diagnostics()["truncated"] is False

    runtime._history_range_cache.clear()  # noqa: SLF001
    coordinator._endpoint_family_state(
        SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
    ).consecutive_failures = 0
    coordinator.client.homeowner_events_page = None
    assert await runtime.async_history_events(first_start, first_end) == ()

    coordinator.client.homeowner_events_page = AsyncMock(return_value=None)
    assert await runtime.async_history_events(first_start, first_end) == ()


@pytest.mark.asyncio
async def test_runtime_history_returns_expired_cache_after_failure(
    hass, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(hass)
    coordinator.client.homeowner_events_page = AsyncMock(
        return_value={
            "events": [
                {
                    "id": "cached",
                    "event_date": 1770000100,
                    "description": "Cached event",
                }
            ]
        }
    )
    runtime = SystemEventsRuntime(coordinator)
    start = datetime.fromtimestamp(1770000000, tz=timezone.utc)
    end = datetime.fromtimestamp(1770000200, tz=timezone.utc)
    now = 1000.0
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.time.monotonic", lambda: now
    )
    expected = await runtime.async_history_events(start, end)
    key = runtime._history_cache_key(start, end, "en")  # noqa: SLF001
    runtime._store_history_range(key, expected, truncated=True)  # noqa: SLF001
    runtime._history_truncated = False  # noqa: SLF001
    now += 901.0
    coordinator.client.homeowner_events_page.side_effect = RuntimeError("boom")

    actual = await runtime.async_history_events(start, end)

    assert actual == expected
    assert runtime.history_diagnostics()["using_cached_data"] is True
    assert runtime.history_diagnostics()["truncated"] is True
    coordinator._note_endpoint_family_failure.assert_called_once()


@pytest.mark.asyncio
async def test_runtime_history_preserves_login_wall_failure(hass) -> None:
    coordinator = _runtime_coordinator(hass)
    coordinator.client.homeowner_events_page = AsyncMock(
        side_effect=EnphaseLoginWallUnauthorized(
            endpoint="/events/homeowner",
            request_label="GET homeowner events",
            status=200,
            content_type="text/html",
        )
    )
    runtime = SystemEventsRuntime(coordinator)

    with pytest.raises(EnphaseLoginWallUnauthorized):
        await runtime.async_history_events(
            datetime.fromtimestamp(1770000000, tz=timezone.utc),
            datetime.fromtimestamp(1770000200, tz=timezone.utc),
        )

    coordinator._note_endpoint_family_failure.assert_not_called()


@pytest.fixture
def alarm_repair_entry(hass, config_entry):
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_SYSTEM_EVENT_REPAIR_ISSUES: True}
    )
    return config_entry


@pytest.mark.asyncio
async def test_runtime_refresh_uses_standing_alarms_for_repairs(
    hass, alarm_repair_entry
) -> None:
    coordinator = _runtime_coordinator(hass, payload=_event_payload())
    coordinator.config_entry = alarm_repair_entry
    runtime = SystemEventsRuntime(coordinator)
    registry = ir.async_get(hass)

    await runtime.async_refresh()

    assert runtime.available is True
    assert runtime.active_count == 2
    assert runtime.high_impact_count == 1
    assert runtime.standing_alarm_count == 1
    assert runtime.problem_active is True
    (((domain, issue_id), issue),) = registry.issues.items()
    assert domain == DOMAIN
    assert issue_id.startswith(
        f"{SYSTEM_EVENT_REPAIR_PREFIX}{alarm_repair_entry.entry_id.lower()}_"
    )
    assert "SERIAL-PRIVATE" not in issue_id
    assert issue.translation_key == "active_system_event"
    assert issue.severity.value == "error"
    assert issue.translation_placeholders["device_type"] == "Gateway SERI...TE-1"
    assert issue.data == {
        "severity": "4",
        "device_type": "Gateway SERI...TE-1",
        "event_date": "2026/07/11 01:02:03 +0000 (UTC)",
        "last_seen_utc": runtime._last_success_utc.isoformat(),  # noqa: SLF001
    }
    coordinator._note_endpoint_family_success.assert_called_once_with(
        SYSTEM_EVENTS_ENDPOINT_FAMILY
    )

    # Repeated identical data does not recreate or update the repair.
    await runtime.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue_id) is issue

    coordinator.client.system_dashboard_events.return_value = {"events": []}
    await runtime.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue_id) is issue
    assert runtime.active_count == 1
    assert runtime.high_impact_count == 0
    assert runtime.standing_alarm_count == 1
    assert runtime.problem_active is True


@pytest.mark.asyncio
async def test_runtime_clears_repair_immediately_for_resolved_event(
    hass, alarm_repair_entry
) -> None:
    event_id = "matching-event"
    coordinator = _runtime_coordinator(
        hass,
        payload={
            "events": [
                {
                    "id": event_id,
                    "event_type": "Gateway fault",
                    "event_state": "open",
                    "event_severity": "critical",
                    "device_type": "Gateway",
                }
            ]
        },
        alarm_payload={
            "alarms": [
                {
                    "id": event_id,
                    "severity": "critical",
                    "type": "Gateway",
                    "first_set": "2026/07/12 19:48:55 +1000 (AEST)",
                }
            ]
        },
    )
    coordinator.config_entry = alarm_repair_entry
    runtime = SystemEventsRuntime(coordinator)
    registry = ir.async_get(hass)

    await runtime.async_refresh()
    ((domain, issue_id),) = registry.issues
    assert domain == DOMAIN

    coordinator.client.system_dashboard_events.return_value = {
        "events": [
            {
                "id": event_id,
                "event_type": "Gateway fault",
                "event_state": "resolved",
                "event_severity": "critical",
                "device_type": "Gateway",
            }
        ]
    }
    await runtime.async_refresh()

    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert runtime.active_count == 0
    assert runtime.problem_active is False


@pytest.mark.asyncio
async def test_runtime_repairs_default_off_and_clear_existing_issue(
    hass, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(
        hass,
        payload=_event_payload(),
        due=False,
        repairs_enabled=False,
    )
    coordinator.config_entry.options = None
    runtime = SystemEventsRuntime(coordinator)
    assert runtime.repairs_enabled is False
    coordinator.config_entry.options = {}
    issue_id = f"{SYSTEM_EVENT_REPAIR_PREFIX}{runtime._entry_suffix()}_persisted"  # noqa: SLF001
    deleted: list[str] = []
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(issues={(DOMAIN, issue_id): object()}),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_delete_issue",
        lambda _hass, _domain, deleted_issue_id: deleted.append(deleted_issue_id),
    )

    await runtime.async_refresh()

    assert deleted == [issue_id]
    assert runtime.repairs_enabled is False
    assert runtime.diagnostics()["repairs_enabled"] is False
    coordinator.client.system_dashboard_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_skips_cooldown_and_keeps_cache_on_failures(hass) -> None:
    coordinator = _runtime_coordinator(hass, payload={"events": []}, due=False)
    runtime = SystemEventsRuntime(coordinator)

    await runtime.async_refresh()
    coordinator.client.system_dashboard_events.assert_not_awaited()

    coordinator._endpoint_family_should_run = lambda _family: True
    coordinator.client = SimpleNamespace()
    with pytest.raises(OptionalEndpointUnavailable):
        await runtime.async_refresh()

    coordinator.client.system_dashboard_events = AsyncMock(return_value=None)
    coordinator.client.system_dashboard_standing_alarms = AsyncMock(
        return_value={"alarms": []}
    )
    with pytest.raises(OptionalEndpointUnavailable):
        await runtime.async_refresh()
    coordinator.client.system_dashboard_events = AsyncMock(return_value={"events": []})
    coordinator.client.system_dashboard_standing_alarms = AsyncMock(return_value=None)
    with pytest.raises(OptionalEndpointUnavailable):
        await runtime.async_refresh()
    assert runtime.available is False


@pytest.mark.asyncio
async def test_runtime_diagnostics_and_persisted_issue_cleanup(
    hass, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(
        hass,
        payload={
            "events": [
                {
                    "id": "warning",
                    "event_type": "Warning",
                    "event_state": "open",
                    "event_severity": "warning",
                    "device_type": "IQ Battery",
                }
            ]
        },
        alarm_payload={"alarms": []},
        entry_id="",
    )
    runtime = SystemEventsRuntime(coordinator)
    fallback_suffix = runtime._entry_suffix()  # noqa: SLF001
    stale_issue = f"{SYSTEM_EVENT_REPAIR_PREFIX}{fallback_suffix}_stale"
    registry = ir.async_get(hass)
    for domain, name in (
        (DOMAIN, stale_issue),
        ("other", stale_issue),
        (DOMAIN, "unrelated"),
    ):
        ir.async_create_issue(
            hass,
            domain,
            name,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="active_system_event",
        )

    await runtime.async_refresh()
    coordinator._endpoint_family_state(
        SYSTEM_EVENTS_ENDPOINT_FAMILY
    ).consecutive_failures = 1
    snapshot = runtime.diagnostics()

    assert registry.async_get_issue(DOMAIN, stale_issue) is not None
    assert snapshot["available"] is True
    assert snapshot["active_count"] == 0
    assert snapshot["high_impact_count"] == 0
    assert snapshot["standing_alarm_count"] == 0
    assert snapshot["severity_counts"] == {}
    assert snapshot["device_type_counts"] == {}
    assert snapshot["last_success_utc"].endswith("+00:00")
    assert snapshot["using_cached_data"] is True
    assert snapshot["truncated"] is False
    assert runtime.active_events[0].event_type == "Warning"
    assert runtime.problem_active is False
    assert runtime.active_event_attributes == ()

    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(issues=None),
    )
    assert runtime._existing_issue_ids(  # noqa: SLF001
        runtime._registry_issue_entries() or {}  # noqa: SLF001
    ) == {stale_issue}

    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(issues={"malformed": object()}),
    )
    assert runtime._registry_issue_entries() == {}  # noqa: SLF001

    active_issue = f"{SYSTEM_EVENT_REPAIR_PREFIX}{fallback_suffix}_active"
    inactive_issue = f"{SYSTEM_EVENT_REPAIR_PREFIX}{fallback_suffix}_inactive"
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(
            issues={
                (DOMAIN, active_issue): SimpleNamespace(active=True),
                (DOMAIN, inactive_issue): SimpleNamespace(active=False),
            }
        ),
    )
    assert runtime._existing_issue_ids(  # noqa: SLF001
        runtime._registry_issue_entries() or {}, active_only=True  # noqa: SLF001
    ) == {active_issue}


@pytest.mark.asyncio
async def test_runtime_bounds_active_event_attributes(hass, monkeypatch) -> None:
    rows = [
        {
            "id": f"event-{index}",
            "event_type": f"Routine {index}",
            "device_type": "IQ Gateway",
            "severity": "error",
            "event_state": "open",
            "event_date": "2026-07-11T01:02:03Z",
            "updated_at": "2026-07-11T01:03:03Z",
        }
        for index in range(ACTIVE_EVENTS_ATTRIBUTE_LIMIT + 5)
    ]
    runtime = SystemEventsRuntime(
        _runtime_coordinator(
            hass,
            payload={"events": rows},
            alarm_payload=_standing_alarm_payload(),
            repairs_enabled=False,
        )
    )

    await runtime.async_refresh()

    generated = []
    iter_events = runtime._iter_activity_events

    def count_rows():
        for row in iter_events():
            generated.append(row)
            yield row

    monkeypatch.setattr(runtime, "_iter_activity_events", count_rows)
    attributes = runtime.active_event_attributes
    assert len(generated) == ACTIVE_EVENTS_ATTRIBUTE_LIMIT
    assert attributes[0]["type"] == "Standing Alarm"
    assert attributes[1]["type"] == "Routine 0"
    assert len(runtime.activity_events) == ACTIVE_EVENTS_ATTRIBUTE_LIMIT + 6
    assert runtime.active_count == ACTIVE_EVENTS_ATTRIBUTE_LIMIT + 6
    assert len(attributes) == ACTIVE_EVENTS_ATTRIBUTE_LIMIT
    assert runtime.problem_active is True
    assert set(attributes[1]) == {
        "type",
        "device_type",
        "state",
        "event_date",
        "updated_at",
        "severity",
        "description",
    }
    assert attributes[1]["event_date"] == ("2026-07-11T01:02:03+00:00")


def test_runtime_unavailable_diagnostics(hass) -> None:
    runtime = SystemEventsRuntime(_runtime_coordinator(hass))
    snapshot = runtime.diagnostics()
    assert snapshot["last_success_utc"] is None
    assert snapshot["using_cached_data"] is False
    assert snapshot["truncated"] is False
    assert snapshot["standing_alarm_count"] == 0


def test_runtime_ignores_invalid_persisted_last_seen(hass, monkeypatch) -> None:
    runtime = SystemEventsRuntime(_runtime_coordinator(hass))
    prefix = f"{SYSTEM_EVENT_REPAIR_PREFIX}{runtime._entry_suffix()}_"  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(
            issues={
                (DOMAIN, f"{prefix}wrong_type"): SimpleNamespace(
                    data={"last_seen_utc": 123}
                ),
                (DOMAIN, f"{prefix}naive"): SimpleNamespace(
                    data={"last_seen_utc": "2026-07-11T00:00:00"}
                ),
            }
        ),
    )

    runtime._restore_repair_last_seen(  # noqa: SLF001
        runtime._registry_issue_entries() or {}  # noqa: SLF001
    )

    assert runtime._repair_last_seen_utc == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_runtime_expires_missing_repair_after_complete_snapshot_grace(
    hass, alarm_repair_entry, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(hass, payload=_event_payload())
    coordinator.config_entry = alarm_repair_entry
    runtime = SystemEventsRuntime(coordinator)
    registry = ir.async_get(hass)
    first_seen = datetime(2026, 7, 11, 23, 55, tzinfo=timezone.utc)
    observed_at = first_seen
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.dt_util.utcnow",
        lambda: observed_at,
    )

    await runtime.async_refresh()
    ((domain, issue_id),) = registry.issues
    assert domain == DOMAIN
    coordinator.client.system_dashboard_standing_alarms.return_value = {"alarms": []}
    observed_at = first_seen + SYSTEM_EVENT_REPAIR_MISSING_GRACE - timedelta(seconds=1)
    await runtime.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    observed_at = first_seen + SYSTEM_EVENT_REPAIR_MISSING_GRACE
    await runtime.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.asyncio
async def test_runtime_does_not_expire_repairs_from_truncated_snapshot(
    hass, alarm_repair_entry, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(hass, payload=_event_payload())
    coordinator.config_entry = alarm_repair_entry
    runtime = SystemEventsRuntime(coordinator)
    registry = ir.async_get(hass)
    first_seen = datetime(2026, 7, 11, tzinfo=timezone.utc)
    observed_at = first_seen
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.dt_util.utcnow",
        lambda: observed_at,
    )

    await runtime.async_refresh()
    ((domain, issue_id),) = registry.issues
    assert domain == DOMAIN
    coordinator.client.system_dashboard_standing_alarms.return_value = {
        "alarms": [],
        "_enphase_ev_truncated": True,
    }
    observed_at = first_seen + SYSTEM_EVENT_REPAIR_MISSING_GRACE * 2
    await runtime.async_refresh()

    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    assert runtime.diagnostics()["truncated"] is True


@pytest.mark.asyncio
async def test_runtime_persists_last_seen_checkpoint_across_reload(
    hass, alarm_repair_entry, monkeypatch
) -> None:
    registry = ir.async_get(hass)
    first_seen = datetime(2026, 7, 11, 23, 55, tzinfo=timezone.utc)
    checkpoint = first_seen + SYSTEM_EVENT_REPAIR_CHECKPOINT_INTERVAL
    observed_at = first_seen
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.dt_util.utcnow",
        lambda: observed_at,
    )

    coordinator = _runtime_coordinator(hass, payload=_event_payload())
    coordinator.config_entry = alarm_repair_entry
    runtime = SystemEventsRuntime(coordinator)
    await runtime.async_refresh()
    ((domain, issue_id),) = registry.issues
    assert domain == DOMAIN
    assert (
        registry.async_get_issue(DOMAIN, issue_id).data["last_seen_utc"]
        == first_seen.isoformat()
    )
    observed_at = checkpoint
    await runtime.async_refresh()
    assert (
        registry.async_get_issue(DOMAIN, issue_id).data["last_seen_utc"]
        == checkpoint.isoformat()
    )

    coordinator.client.system_dashboard_events.return_value = {"events": []}
    coordinator.client.system_dashboard_standing_alarms.return_value = {"alarms": []}
    reloaded = SystemEventsRuntime(coordinator)
    observed_at = checkpoint + SYSTEM_EVENT_REPAIR_MISSING_GRACE
    await reloaded.async_refresh()

    assert registry.async_get_issue(DOMAIN, issue_id) is None


def test_activity_descriptions_are_sanitized_and_bounded():
    text = (
        'Site 1234567 SERIAL-PRIVATE-1 at 192.168.1.1 {"token": "private secret"} '
        "token=secret https://example.invalid/private?token=secret " + "x" * 200
    )
    event = parse_active_system_events(
        {
            "events": [
                {
                    "id": "one",
                    "event_type": "Fault",
                    "severity": "error",
                    "serial_number": "SERIAL-PRIVATE-1",
                    "details": text,
                }
            ]
        },
        site_id="1234567",
    )[0]
    alarm = parse_standing_alarms(
        {
            "alarms": [
                {"id": "one", "serial_num": "SERIAL-PRIVATE-1", "description": text}
            ]
        },
        site_id="1234567",
    )[0]
    for item in (event, alarm):
        assert item.description is not None
        assert len(item.description) <= 163
        for private in (
            "1234567",
            "SERIAL-PRIVATE-1",
            "192.168.1.1",
            "secret",
            "https://",
        ):
            assert private not in item.description


def test_activity_descriptions_redact_identifiers_from_other_devices():
    details = (
        'Fault {"device_id": "DEVICE-PRIVATE-9999", '
        '"serial_number": "OTHER-SERIAL-PRIVATE", "reason": "offline"}'
    )
    event = parse_active_system_events(
        {
            "events": [
                {
                    "id": "one",
                    "event_type": "Fault",
                    "severity": "error",
                    "serial_number": "REPORTED-SERIAL",
                    "details": details,
                }
            ]
        },
        site_id="1234567",
    )[0]
    alarm = parse_standing_alarms(
        {
            "alarms": [
                {
                    "id": "one",
                    "serial_num": "REPORTED-SERIAL",
                    "description": details,
                }
            ]
        },
        site_id="1234567",
    )[0]
    for item in (event, alarm):
        assert "DEVICE-PRIVATE-9999" not in item.description
        assert "OTHER-SERIAL-PRIVATE" not in item.description
        assert "offline" in item.description


@pytest.mark.parametrize("standing", [False, True])
def test_system_dashboard_redacts_serials_found_in_other_rows(standing) -> None:
    serial = "ABC-123456789"
    longer = serial + "-LONG"
    if standing:
        events = parse_standing_alarms(
            {
                "alarms": [
                    {"id": "first", "serial_num": serial, "description": longer},
                    {"id": "second", "serial_num": longer},
                    {"id": "third", "serial_num": longer},
                ]
            },
            site_id="1234567",
        )
    else:
        events = parse_active_system_events(
            {
                "events": [
                    {"id": "first", "serial_number": serial, "details": longer},
                    {"id": "second", "serial_number": longer},
                    {"id": "third", "serial_number": longer},
                ]
            },
            site_id="1234567",
        )
    assert events[0].description == "ABC-...LONG"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["contained", "expired", "truncated", "row-cap", "locale", "wider"]
)
async def test_calendar_reuses_only_fresh_complete_same_locale_ranges(
    hass, monkeypatch, case
) -> None:
    from custom_components.enphase_ev import system_events

    now = 1000.0
    monkeypatch.setattr(system_events, "time", SimpleNamespace(monotonic=lambda: now))
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    start = datetime(2026, 2, 2, tzinfo=timezone.utc)
    end = start + timedelta(hours=4)
    rows = [
        {
            "id": "outside",
            "description": "Outside",
            "event_date": (start + timedelta(hours=3)).timestamp(),
        },
        {
            "id": "within",
            "description": "Within",
            "event_date": (start + timedelta(hours=1, minutes=30)).timestamp(),
        },
        {
            "id": "crossing",
            "description": "Crossing",
            "event_date": start.timestamp(),
            "event_clear_date": (start + timedelta(hours=2)).timestamp(),
        },
    ]
    fetch = coordinator.client.homeowner_events_page
    cursor = {"truncated": "start", "row-cap": "older"}.get(case)
    fetch.return_value = {"events": rows, "next": cursor}
    with monkeypatch.context() as cap_patch:
        if case == "row-cap":
            cap_patch.setattr(
                system_events, "SYSTEM_EVENT_HISTORY_ROW_LIMIT", len(rows)
            )
        assert len(await runtime.async_history_events(start, end)) == 3
    fetch.return_value = {"events": rows, "next": None}
    narrow_start = start + timedelta(hours=1)
    narrow_end = start + timedelta(hours=2)
    if case == "expired":
        now += system_events.SYSTEM_EVENT_HISTORY_CACHE_TTL
    elif case == "locale":
        monkeypatch.setattr(runtime, "_history_locale", lambda: "fr")
    elif case == "wider":
        narrow_start = start - timedelta(hours=1)
    events = await runtime.async_history_events(narrow_start, narrow_end)
    assert [event.summary for event in events] == ["Crossing", "Within"]
    assert fetch.await_count == (1 if case == "contained" else 2)
    assert not runtime.history_diagnostics()["truncated"]
    assert runtime.history_diagnostics()["cached_range_count"] == 2


@pytest.mark.asyncio
async def test_alarm_reconciliation_reuses_registry_snapshot(
    hass, alarm_repair_entry, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(
        hass,
        payload={"events": []},
        alarm_payload={
            "alarms": [
                {"id": f"alarm-{index}", "severity": "error"} for index in range(8)
            ]
        },
    )
    coordinator.config_entry = alarm_repair_entry
    runtime = SystemEventsRuntime(coordinator)
    await runtime.async_refresh()
    registry = ir.async_get(hass)
    initial = tuple(registry.issues.values())
    assert len(initial) == 8

    snapshot = MagicMock(wraps=runtime._registry_issue_entries)
    monkeypatch.setattr(runtime, "_registry_issue_entries", snapshot)
    await runtime.async_refresh()

    snapshot.assert_called_once_with()
    assert all(
        current is previous
        for current, previous in zip(registry.issues.values(), initial, strict=True)
    )


@pytest.mark.parametrize("standing", [False, True])
def test_dashboard_redaction_masks_serials_that_cross_another_serial(standing) -> None:
    first = "101234565678"
    second = "567812345678"
    overlapping = first + second[4:]
    if standing:
        entries = parse_standing_alarms(
            {
                "alarms": [
                    {"id": "one", "serial_num": first, "description": overlapping},
                    {"id": "two", "serial_num": second},
                ]
            },
            site_id="9999999",
        )
    else:
        entries = parse_active_system_events(
            {
                "events": [
                    {"id": "one", "serial_number": first, "details": overlapping},
                    {"id": "two", "serial_number": second},
                ]
            },
            site_id="9999999",
        )
    assert entries[0].description == "[redacted]"


@pytest.mark.asyncio
async def test_history_range_fetch_finds_spanning_events_on_later_pages(hass) -> None:
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    coordinator = _runtime_coordinator(hass)
    fetch = coordinator.client.homeowner_events_page

    async def page(*, next_cursor, **kwargs):
        if next_cursor == "start":
            return {
                "events": [
                    {
                        "id": "old-instant",
                        "description": "Already finished",
                        "event_date": (day + timedelta(hours=2)).timestamp(),
                    }
                ],
                "next": "older",
            }
        assert next_cursor == "older"
        return {
            "events": [
                {
                    "id": "overlap",
                    "description": "Crossing",
                    "event_date": (day + timedelta(hours=1)).timestamp(),
                    "event_clear_date": (
                        day + timedelta(hours=3, minutes=30)
                    ).timestamp(),
                }
            ],
            "next": None,
        }

    fetch.side_effect = page
    start = day + timedelta(hours=3)
    end = day + timedelta(hours=4)
    cold = SystemEventsRuntime(coordinator)
    results = await cold.async_history_events(start, end)
    assert [event.summary for event in results] == ["Crossing"]
    assert fetch.await_count == 2

    warm = SystemEventsRuntime(coordinator)
    await warm.async_history_events(day, day + timedelta(hours=5))
    assert fetch.await_count == 4
    assert await warm.async_history_events(start, end) == results
    assert fetch.await_count == 4


@pytest.mark.asyncio
async def test_calendar_preserves_matching_rows_when_a_later_page_fails(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coordinator_module
    from custom_components.enphase_ev import system_events

    now = 1000.0
    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr(system_events, "time", clock)
    monkeypatch.setattr(coordinator_module, "time", clock)
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    coordinator = coordinator_factory()
    runtime = coordinator.system_events_runtime
    page = {
        "events": [
            {"id": "old", "event_date": day.timestamp(), "description": "Outside"},
            {
                "id": "inside",
                "event_date": (day + timedelta(hours=2)).timestamp(),
                "description": "Inside",
            },
        ],
        "next": "older",
    }
    fetch = AsyncMock(
        side_effect=[
            page,
            aiohttp.ClientResponseError(
                SimpleNamespace(real_url="https://example.invalid/history"),
                (),
                status=429,
                headers={"Retry-After": "1800"},
            ),
            aiohttp.ClientResponseError(
                SimpleNamespace(real_url="https://example.invalid/history"),
                (),
                status=400,
            ),
            {**page, "next": None},
        ]
    )
    monkeypatch.setattr(coordinator.client, "homeowner_events_page", fetch)
    family = SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
    start, end = day + timedelta(hours=1), day + timedelta(hours=3)

    entries = await runtime.async_history_events(start, end)
    assert [event.summary for event in entries] == ["Inside"]
    assert runtime.history_diagnostics()["truncated"]
    assert runtime.history_diagnostics()["cached_range_count"] == 1
    assert not runtime.history_available
    assert coordinator._endpoint_family_state(family).consecutive_failures == 1
    assert coordinator._endpoint_family_wait_active(family)
    assert not coordinator._endpoint_family_can_use_stale(family)

    assert await runtime.async_history_events(start, end) == entries
    assert runtime.history_diagnostics()["using_cached_data"]
    assert runtime.history_diagnostics()["truncated"]
    assert await runtime.async_history_events(day + timedelta(minutes=90), end) == ()
    now += system_events.SYSTEM_EVENT_HISTORY_CACHE_TTL
    assert await runtime.async_history_events(start, end) == entries
    assert fetch.await_count == 2

    now = 2801.0
    assert await runtime.async_history_events(start, end) == ()
    assert not runtime.history_diagnostics()["using_cached_data"]
    assert await runtime.async_history_events(start, end) == entries
    assert fetch.await_count == 4
    assert not runtime.history_diagnostics()["truncated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [False, True])
async def test_calendar_does_not_cache_failed_prefix_without_cooldown(
    coordinator_factory, monkeypatch, warm
) -> None:
    coordinator = coordinator_factory()
    runtime = coordinator.system_events_runtime
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    end = day + timedelta(hours=1)
    event = {"id": "event", "event_date": day.timestamp(), "description": "Seen"}
    fetch = AsyncMock(
        side_effect=[
            {"events": [event], "next": "older"},
            aiohttp.ClientResponseError(
                SimpleNamespace(real_url="https://example.invalid/history"),
                (),
                status=400,
            ),
            {"events": [event], "next": None},
        ]
    )
    monkeypatch.setattr(coordinator.client, "homeowner_events_page", fetch)
    family = SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
    if warm:
        coordinator.note_endpoint_family_success(family)
        assert coordinator.endpoint_family_next_retry_mono(family) is not None

    partial = await runtime.async_history_events(day, end)
    assert [row.summary for row in partial] == ["Seen"]
    assert runtime.history_diagnostics()["truncated"]
    assert runtime.history_diagnostics()["cached_range_count"] == 0
    assert await runtime.async_history_events(day, end) == partial
    assert not runtime.history_diagnostics()["truncated"]
    assert fetch.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429])
@pytest.mark.parametrize("probe_succeeds", [False, True])
async def test_probe_discards_failed_calendar_prefix_when_cooldown_changes(
    coordinator_factory, monkeypatch, status, probe_succeeds
) -> None:
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    from custom_components.enphase_ev import coordinator as coordinator_module
    from custom_components.enphase_ev import system_events

    now = 1000.0
    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr(system_events, "time", clock)
    monkeypatch.setattr(coordinator_module, "time", clock)
    coordinator = coordinator_factory()
    runtime = coordinator.system_events_runtime
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    end = day + timedelta(hours=1)
    event = {"id": "event", "event_date": day.timestamp(), "description": "Seen"}
    page = {"events": [event], "next": "older"}
    request = SimpleNamespace(real_url="https://example.invalid/history")
    probe = (
        {"events": [], "next": None}
        if probe_succeeds
        else aiohttp.ClientResponseError(request, (), status=503)
    )
    fetch = AsyncMock(
        side_effect=[
            page,
            aiohttp.ClientResponseError(
                request, (), status=429, headers={"Retry-After": "3600"}
            ),
            probe,
            aiohttp.ClientResponseError(request, (), status=status),
        ]
    )
    monkeypatch.setattr(coordinator.client, "homeowner_events_page", fetch)

    async def run_history_probe(self):
        await runtime.async_refresh_history()

    monkeypatch.setattr(
        DataUpdateCoordinator, "async_request_refresh", run_history_probe
    )

    assert [row.summary for row in await runtime.async_history_events(day, end)] == [
        "Seen"
    ]
    assert runtime.history_diagnostics()["cached_range_count"] == 1
    now += 10
    await coordinator.async_request_refresh()
    assert runtime.history_available is probe_succeeds
    assert runtime.history_diagnostics()["cached_range_count"] == 0
    if not probe_succeeds:
        assert await runtime.async_history_events(day, end) == ()
        assert fetch.await_count == 3
        # The probe's shorter cooldown has expired, while the original would last.
        now = 3100.0
    assert await runtime.async_history_events(day, end) == ()
    assert fetch.await_count == 4


@pytest.mark.asyncio
async def test_calendar_cap_does_not_repeat_the_feed_scan_for_other_ranges(
    hass, monkeypatch
) -> None:
    from custom_components.enphase_ev import system_events

    now = 1000.0
    monkeypatch.setattr(system_events, "time", SimpleNamespace(monotonic=lambda: now))
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    page_size = system_events.SYSTEM_EVENT_HISTORY_PAGE_SIZE
    cap = system_events.SYSTEM_EVENT_HISTORY_ROW_LIMIT
    assert cap % page_size == 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def page(*, next_cursor, page_size, **kwargs):
        first = 0 if next_cursor == "start" else int(next_cursor)
        if first == 0:
            entered.set()
            await release.wait()
        return {
            "events": [
                {"id": str(i), "event_date": (day - timedelta(minutes=i)).timestamp()}
                for i in range(first, first + page_size)
            ],
            "next": str(first + page_size),
        }

    fetch = coordinator.client.homeowner_events_page
    fetch.side_effect = page
    start, end = day - timedelta(minutes=20), day + timedelta(minutes=1)
    narrow = day - timedelta(minutes=5)
    async with asyncio.timeout(15):
        first = asyncio.create_task(runtime.async_history_events(start, end))
        await entered.wait()
        contained = asyncio.create_task(runtime.async_history_events(narrow, end))
        separate = asyncio.create_task(
            runtime.async_history_events(
                day - timedelta(hours=30), day - timedelta(hours=29)
            )
        )
        await asyncio.sleep(0)
        release.set()
        wide, selected, disjoint = await asyncio.gather(first, contained, separate)

    assert len(wide) == 21
    assert selected == wide[-6:]
    assert len(disjoint) == 60
    assert fetch.await_count == cap // page_size
    assert runtime.history_diagnostics()["truncated"]

    now += system_events.SYSTEM_EVENT_HISTORY_CACHE_TTL / 2
    assert await runtime.async_history_events(narrow, end) == selected
    assert fetch.await_count == cap // page_size

    now += system_events.SYSTEM_EVENT_HISTORY_CACHE_TTL / 2
    fetch.side_effect = None
    fetch.return_value = {"events": [], "next": None}
    assert await runtime.async_history_events(narrow, end) == ()
    assert fetch.await_count == cap // page_size + 1
    assert not runtime.history_diagnostics()["truncated"]


@pytest.mark.asyncio
async def test_complete_feed_replaces_an_older_exact_incomplete_result(hass) -> None:
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    first = {"id": "first", "event_date": day.timestamp(), "description": "First"}
    second = {
        "id": "second",
        "event_date": (day + timedelta(minutes=1)).timestamp(),
        "description": "Second",
    }
    fetch = coordinator.client.homeowner_events_page
    fetch.side_effect = [
        {"events": [first], "next": "start"},
        {"events": [second, first], "next": None},
    ]
    end = day + timedelta(minutes=2)
    assert [row.summary for row in await runtime.async_history_events(day, end)] == [
        "First"
    ]
    assert runtime.history_diagnostics()["truncated"]

    # A disjoint query obtains the full feed. The incomplete exact entry cannot win.
    assert (
        await runtime.async_history_events(
            day - timedelta(hours=2), day - timedelta(hours=1)
        )
        == ()
    )
    assert [row.summary for row in await runtime.async_history_events(day, end)] == [
        "First",
        "Second",
    ]
    assert fetch.await_count == 2
    assert not runtime.history_diagnostics()["truncated"]


@pytest.mark.asyncio
async def test_calendar_feed_lookup_discards_expired_snapshot(hass, monkeypatch):
    from custom_components.enphase_ev import system_events

    now = 1000.0
    monkeypatch.setattr(system_events, "time", SimpleNamespace(monotonic=lambda: now))
    coordinator = _runtime_coordinator(hass, history_payload={"events": []})
    runtime = SystemEventsRuntime(coordinator)
    start = datetime(2026, 2, 2, tzinfo=timezone.utc)
    await runtime.async_history_events(start, start + timedelta(hours=1))
    locale = runtime._history_locale()
    assert runtime._fresh_history_feed(locale) is not None

    now += system_events.SYSTEM_EVENT_HISTORY_CACHE_TTL
    assert runtime._fresh_history_feed(locale) is None
    assert runtime._history_feed is None


@pytest.mark.asyncio
async def test_malformed_terminal_page_cannot_establish_empty_coverage(hass) -> None:
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    fetch = coordinator.client.homeowner_events_page
    fetch.side_effect = [
        {"events": [{"event_date": "broken"}], "next": None},
        {
            "events": [
                {"id": "valid", "event_date": day.timestamp(), "description": "Valid"}
            ],
            "next": None,
        },
    ]

    assert await runtime.async_history_events(day, day + timedelta(hours=2)) == ()
    assert not runtime.history_available
    assert runtime.history_diagnostics()["cached_range_count"] == 0
    coordinator._note_endpoint_family_failure.assert_called_once()
    recovered = await runtime.async_history_events(day, day + timedelta(hours=1))
    assert [event.summary for event in recovered] == ["Valid"]
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_contained_calendar_result_keeps_source_expiry_and_stale_fallback(
    hass, monkeypatch
) -> None:
    from custom_components.enphase_ev import system_events

    now = 1000.0
    monkeypatch.setattr(system_events, "time", SimpleNamespace(monotonic=lambda: now))
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    fetch = coordinator.client.homeowner_events_page
    fetch.side_effect = [
        {
            "events": [
                {"id": "kept", "event_date": day.timestamp(), "description": "Kept"}
            ],
            "next": None,
        },
        aiohttp.ClientResponseError(
            SimpleNamespace(real_url="https://example.invalid/history"), (), status=503
        ),
    ]

    expected = await runtime.async_history_events(day, day + timedelta(hours=2))
    now += system_events.SYSTEM_EVENT_HISTORY_CACHE_TTL / 2
    narrow_end = day + timedelta(hours=1)
    assert await runtime.async_history_events(day, narrow_end) == expected
    assert fetch.await_count == 1

    now += system_events.SYSTEM_EVENT_HISTORY_CACHE_TTL / 2
    assert await runtime.async_history_events(day, narrow_end) == expected
    assert fetch.await_count == 2
    assert runtime.history_diagnostics()["using_cached_data"]


@pytest.mark.asyncio
async def test_wider_calendar_query_reloads_changed_tail_behind_unchanged_head(hass):
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    current_time = (day + timedelta(hours=3)).timestamp()
    old_time = (day + timedelta(hours=1)).timestamp()
    head = {
        "events": [
            {"id": "recent", "description": "Recent", "event_date": current_time}
        ],
        "next": "same-anchor",
    }
    older = {"id": "older", "description": "Before", "event_date": old_time}
    fetch = coordinator.client.homeowner_events_page
    fetch.side_effect = [
        head,
        {"events": [older], "next": None},
        head,
        {
            "events": [
                {**older, "description": "After"},
                {"id": "new", "description": "New", "event_date": old_time + 60},
            ],
            "next": None,
        },
    ]

    end = day + timedelta(hours=4)
    narrow = await runtime.async_history_events(day + timedelta(hours=2), end)
    assert [row.summary for row in narrow] == ["Recent"]
    wide = await runtime.async_history_events(day, end)
    assert [row.summary for row in wide] == ["After", "New", "Recent"]
    assert fetch.await_count == 4
    assert not runtime.history_diagnostics()["truncated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_field", ["row", "start", "clear", "inverted"])
async def test_mixed_validity_calendar_page_displays_rows_without_claiming_coverage(
    hass, invalid_field
):
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    valid = {"id": "valid", "event_date": day.timestamp(), "description": "Valid"}
    uncertain_rows = {
        "row": [valid, {"event_date": "broken"}],
        "start": [{**valid, "event_start_date": "broken"}],
        "clear": [{**valid, "event_clear_date": "broken"}],
        "inverted": [{**valid, "event_clear_date": day.timestamp() - 1}],
    }
    fetch = coordinator.client.homeowner_events_page
    fetch.side_effect = [
        {"events": uncertain_rows[invalid_field], "next": None},
        {"events": [valid], "next": None},
    ]

    results = await runtime.async_history_events(day, day + timedelta(hours=2))
    assert [event.summary for event in results] == ["Valid"]
    assert runtime.history_diagnostics()["truncated"]
    assert await runtime.async_history_events(day, day + timedelta(hours=1)) == results
    assert fetch.await_count == 2
    assert not runtime.history_diagnostics()["truncated"]


@pytest.mark.asyncio
async def test_calendar_scan_never_blocks_the_background_probe(
    coordinator_factory, monkeypatch
):
    coordinator = coordinator_factory()
    runtime = coordinator.system_events_runtime
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    start, end = day, day + timedelta(hours=1)
    entered_tail, release_tail = asyncio.Event(), asyncio.Event()
    release_probe = asyncio.Event()
    cursors = []

    async def page(*, next_cursor, **kwargs):
        cursors.append(next_cursor)
        if next_cursor == "older":
            entered_tail.set()
            await release_tail.wait()
            raise aiohttp.ClientResponseError(
                SimpleNamespace(real_url="https://example.invalid/history"),
                (),
                status=429,
                headers={"Retry-After": "1800"},
            )
        if len(cursors) > 1:
            await release_probe.wait()
        return {
            "events": [
                {"id": "kept", "event_date": day.timestamp(), "description": "Kept"}
            ],
            "next": "older",
        }

    monkeypatch.setattr(coordinator.client, "homeowner_events_page", page)
    family = SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
    async with asyncio.timeout(5):
        query = asyncio.create_task(runtime.async_history_events(start, end))
        await entered_tail.wait()
        try:
            await asyncio.wait_for(runtime.async_refresh_history(), timeout=1)
            assert not query.done()
            assert cursors == ["start", "older"]
        finally:
            release_tail.set()
            release_probe.set()
            partial = await query

    assert cursors == ["start", "older"]
    assert coordinator._endpoint_family_wait_active(family)
    assert coordinator._endpoint_family_state(family).consecutive_failures == 1
    assert [event.summary for event in partial] == ["Kept"]
    assert await runtime.async_history_events(start, end) == partial
    assert cursors == ["start", "older"]


@pytest.mark.asyncio
async def test_duplicate_availability_probe_does_not_wait_for_first_probe(hass):
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(**kwargs):
        entered.set()
        await release.wait()
        return {"events": [], "next": None}

    coordinator.client.homeowner_events_page = AsyncMock(side_effect=fetch)
    probe = asyncio.create_task(runtime.async_refresh_history())
    async with asyncio.timeout(5):
        try:
            await entered.wait()
            await asyncio.wait_for(runtime.async_refresh_history(), timeout=1)
            assert not probe.done()
        finally:
            release.set()
            await probe
    coordinator.client.homeowner_events_page.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("feed_expires", [False, True])
async def test_queued_calendar_query_keeps_its_locale_feed(
    hass, monkeypatch, feed_expires
):
    from custom_components.enphase_ev import system_events

    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)
    entered_english, release_english = asyncio.Event(), asyncio.Event()
    entered_italian, release_italian = asyncio.Event(), asyncio.Event()
    requested_locales = []
    language = "en"
    now = 1000.0
    monkeypatch.setattr(system_events, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(runtime, "_history_locale", lambda: language)

    async def fetch(*, locale, **kwargs):
        requested_locales.append(locale)
        if requested_locales == ["en"]:
            entered_english.set()
            await release_english.wait()
        if locale == "it":
            entered_italian.set()
            await release_italian.wait()
        return {
            "events": [
                {"id": "early", "event_date": day.timestamp(), "description": locale},
                {
                    "id": "late",
                    "event_date": (day + timedelta(hours=2)).timestamp(),
                    "description": locale,
                },
            ],
            "next": None,
        }

    coordinator.client.homeowner_events_page.side_effect = fetch
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        first = tasks.create_task(
            runtime.async_history_events(
                day + timedelta(hours=2), day + timedelta(hours=3)
            )
        )
        await entered_english.wait()
        language = "it"
        second = tasks.create_task(
            runtime.async_history_events(day, day + timedelta(hours=1))
        )
        await asyncio.sleep(0)
        language = "en"
        third = tasks.create_task(
            runtime.async_history_events(day, day + timedelta(hours=1))
        )
        await asyncio.sleep(0)
        release_english.set()
        await entered_italian.wait()
        if feed_expires:
            now += system_events.SYSTEM_EVENT_HISTORY_CACHE_TTL
        release_italian.set()

    assert [row.summary for row in first.result()] == ["en"]
    assert [row.summary for row in second.result()] == ["it"]
    assert [row.summary for row in third.result()] == ["en"]
    assert requested_locales == (["en", "it", "en"] if feed_expires else ["en", "it"])
    assert not runtime._history_queries


@pytest.mark.asyncio
async def test_retired_calendar_cancels_active_and_queued_queries(
    coordinator_factory, monkeypatch
):
    coordinator = coordinator_factory()
    runtime = coordinator.system_events_runtime
    day = datetime(2026, 2, 2, tzinfo=timezone.utc)
    end = day + timedelta(hours=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_page(**kwargs):
        entered.set()
        await release.wait()
        return {"events": [], "next": "older"}

    fetch = AsyncMock(side_effect=held_page)
    monkeypatch.setattr(coordinator.client, "homeowner_events_page", fetch)
    family = SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
    health_before = replace(coordinator._endpoint_family_state(family))
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        active = tasks.create_task(runtime.async_history_events(day, end))
        await entered.wait()
        queued = tasks.create_task(
            runtime.async_history_events(day - timedelta(days=1), end)
        )
        await asyncio.sleep(0)
        coordinator.mark_runtime_stopped()
        release.set()

    assert active.cancelled()
    assert queued.cancelled()
    with pytest.raises(asyncio.CancelledError):
        await runtime.async_history_events(day, end)
    await runtime.async_refresh_history()
    fetch.assert_awaited_once()
    assert not runtime._history_queries
    assert runtime.history_diagnostics()["cached_range_count"] == 0
    assert not runtime.history_available
    assert coordinator._endpoint_family_state(family) == health_before


@pytest.mark.asyncio
async def test_retired_history_probe_does_not_publish(hass):
    coordinator = _runtime_coordinator(hass)
    runtime = SystemEventsRuntime(coordinator)

    async def retire(**kwargs):
        coordinator.runtime_active = False
        return {"events": [], "next": None}

    coordinator.client.homeowner_events_page.side_effect = retire
    with pytest.raises(asyncio.CancelledError):
        await runtime.async_refresh_history()

    assert not runtime.history_available
    coordinator._note_endpoint_family_success.assert_not_called()
    coordinator._note_endpoint_family_failure.assert_not_called()
