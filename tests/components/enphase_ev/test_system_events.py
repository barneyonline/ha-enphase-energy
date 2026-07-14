"""Tests for read-only System Dashboard event monitoring."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    assert "Private diagnostic details" not in repr(alarm)
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
    if alarm_payload is _DEFAULT_ALARMS:
        alarm_payload = _standing_alarm_payload()
    return SimpleNamespace(
        hass=hass,
        site_id="1234567",
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
        _note_endpoint_family_failure=MagicMock(),
        _note_endpoint_family_success=MagicMock(),
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
    runtime._history_range_cache.clear()  # noqa: SLF001
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


@pytest.mark.asyncio
async def test_runtime_refresh_uses_standing_alarms_for_repairs(
    hass, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(hass, payload=_event_payload())
    runtime = SystemEventsRuntime(coordinator)
    created: list[tuple[str, dict[str, object]]] = []
    deleted: list[str] = []
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(issues={}),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_create_issue",
        lambda _hass, _domain, issue_id, **kwargs: created.append((issue_id, kwargs)),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_delete_issue",
        lambda _hass, _domain, issue_id: deleted.append(issue_id),
    )

    await runtime.async_refresh()

    assert runtime.available is True
    assert runtime.active_count == 2
    assert runtime.high_impact_count == 1
    assert runtime.standing_alarm_count == 1
    assert runtime.problem_active is True
    assert len(created) == 1
    issue_id, issue = created[0]
    assert issue_id.startswith(f"{SYSTEM_EVENT_REPAIR_PREFIX}entry_id_")
    assert "SERIAL-PRIVATE" not in issue_id
    assert issue["translation_key"] == "active_system_event"
    assert issue["severity"].value == "error"
    assert issue["translation_placeholders"]["device_type"] == "Gateway SERI...TE-1"
    assert issue["data"] == {
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
    assert len(created) == 1

    coordinator.client.system_dashboard_events.return_value = {"events": []}
    await runtime.async_refresh()
    assert deleted == []
    assert runtime.active_count == 1
    assert runtime.high_impact_count == 0
    assert runtime.standing_alarm_count == 1
    assert runtime.problem_active is True


@pytest.mark.asyncio
async def test_runtime_clears_repair_immediately_for_resolved_event(
    hass, monkeypatch
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
    runtime = SystemEventsRuntime(coordinator)
    created: list[str] = []
    deleted: list[str] = []
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(issues={}),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_create_issue",
        lambda _hass, _domain, issue_id, **_kwargs: created.append(issue_id),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_delete_issue",
        lambda _hass, _domain, issue_id: deleted.append(issue_id),
    )

    await runtime.async_refresh()
    assert len(created) == 1

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

    assert deleted == created
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
    deleted: list[str] = []
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(
            issues={
                (DOMAIN, stale_issue): object(),
                ("other", stale_issue): object(),
                (DOMAIN, "unrelated"): object(),
                "malformed": object(),
            }
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_delete_issue",
        lambda _hass, _domain, issue_id: deleted.append(issue_id),
    )

    await runtime.async_refresh()
    coordinator._endpoint_family_state(
        SYSTEM_EVENTS_ENDPOINT_FAMILY
    ).consecutive_failures = 1
    snapshot = runtime.diagnostics()

    assert deleted == []
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
    assert runtime._existing_issue_ids() == {stale_issue}  # noqa: SLF001

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
    assert runtime._existing_issue_ids(active_only=True) == {  # noqa: SLF001
        active_issue
    }


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
            alarm_payload={"alarms": []},
        )
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(issues={}),
    )

    await runtime.async_refresh()

    assert runtime.active_count == ACTIVE_EVENTS_ATTRIBUTE_LIMIT + 5
    assert len(runtime.active_event_attributes) == ACTIVE_EVENTS_ATTRIBUTE_LIMIT
    assert runtime.problem_active is True
    assert set(runtime.active_event_attributes[0]) == {
        "type",
        "device_type",
        "state",
        "event_date",
        "updated_at",
    }
    assert runtime.active_event_attributes[0]["event_date"] == (
        "2026-07-11T01:02:03+00:00"
    )


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

    runtime._restore_repair_last_seen()  # noqa: SLF001

    assert runtime._repair_last_seen_utc == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_runtime_expires_missing_repair_after_complete_snapshot_grace(
    hass, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(hass, payload=_event_payload())
    runtime = SystemEventsRuntime(coordinator)
    created: list[str] = []
    deleted: list[str] = []
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(issues={}),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_create_issue",
        lambda _hass, _domain, issue_id, **_kwargs: created.append(issue_id),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_delete_issue",
        lambda _hass, _domain, issue_id: deleted.append(issue_id),
    )
    first_seen = datetime(2026, 7, 11, 23, 55, tzinfo=timezone.utc)
    observed_times = iter(
        (
            first_seen,
            first_seen + SYSTEM_EVENT_REPAIR_MISSING_GRACE - timedelta(seconds=1),
            first_seen + SYSTEM_EVENT_REPAIR_MISSING_GRACE,
        )
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.dt_util.utcnow",
        lambda: next(observed_times),
    )

    await runtime.async_refresh()
    coordinator.client.system_dashboard_standing_alarms.return_value = {"alarms": []}
    await runtime.async_refresh()
    assert deleted == []

    await runtime.async_refresh()
    assert deleted == created


@pytest.mark.asyncio
async def test_runtime_does_not_expire_repairs_from_truncated_snapshot(
    hass, monkeypatch
) -> None:
    coordinator = _runtime_coordinator(hass, payload=_event_payload())
    runtime = SystemEventsRuntime(coordinator)
    deleted: list[str] = []
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(issues={}),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_create_issue",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_delete_issue",
        lambda _hass, _domain, issue_id: deleted.append(issue_id),
    )
    first_seen = datetime(2026, 7, 11, tzinfo=timezone.utc)
    observed_times = iter(
        (first_seen, first_seen + SYSTEM_EVENT_REPAIR_MISSING_GRACE * 2)
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.dt_util.utcnow",
        lambda: next(observed_times),
    )

    await runtime.async_refresh()
    coordinator.client.system_dashboard_standing_alarms.return_value = {
        "alarms": [],
        "_enphase_ev_truncated": True,
    }
    await runtime.async_refresh()

    assert deleted == []
    assert runtime.diagnostics()["truncated"] is True


@pytest.mark.asyncio
async def test_runtime_persists_last_seen_checkpoint_across_reload(
    hass, monkeypatch
) -> None:
    issues: dict[tuple[str, str], object] = {}
    created: list[str] = []
    deleted: list[str] = []

    def _create(_hass, domain, issue_id, **kwargs) -> None:
        created.append(issue_id)
        issues[(domain, issue_id)] = SimpleNamespace(
            active=True,
            data=kwargs["data"],
        )

    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_get",
        lambda _hass: SimpleNamespace(issues=issues),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_create_issue",
        _create,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.ir.async_delete_issue",
        lambda _hass, _domain, issue_id: deleted.append(issue_id),
    )
    first_seen = datetime(2026, 7, 11, 23, 55, tzinfo=timezone.utc)
    checkpoint = first_seen + SYSTEM_EVENT_REPAIR_CHECKPOINT_INTERVAL
    observed_times = iter(
        (
            first_seen,
            checkpoint,
            checkpoint + SYSTEM_EVENT_REPAIR_MISSING_GRACE,
        )
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.system_events.dt_util.utcnow",
        lambda: next(observed_times),
    )

    runtime = SystemEventsRuntime(_runtime_coordinator(hass, payload=_event_payload()))
    await runtime.async_refresh()
    await runtime.async_refresh()
    assert len(created) == 2
    issue_id = created[-1]
    assert issues[(DOMAIN, issue_id)].data["last_seen_utc"] == checkpoint.isoformat()

    reloaded = SystemEventsRuntime(
        _runtime_coordinator(
            hass,
            payload={"events": []},
            alarm_payload={"alarms": []},
        )
    )
    await reloaded.async_refresh()

    assert deleted == [issue_id]
