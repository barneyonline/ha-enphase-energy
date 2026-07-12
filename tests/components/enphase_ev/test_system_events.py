"""Tests for read-only System Dashboard event monitoring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.enphase_ev.api import OptionalEndpointUnavailable
from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.system_events import (
    SYSTEM_EVENTS_ENDPOINT_FAMILY,
    SYSTEM_EVENT_REPAIR_CHECKPOINT_INTERVAL,
    SYSTEM_EVENT_REPAIR_MISSING_GRACE,
    SYSTEM_EVENT_REPAIR_PREFIX,
    SystemEventsRuntime,
    _catalog_label,
    _event_fingerprint,
    _event_is_active,
    _lookup_catalog,
    _normalized,
    _text,
    parse_active_system_events,
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
    assert event.event_date == "2026-07-11T01:02:03Z"
    assert event.updated_at == "2026-07-11T01:03:03Z"
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


def test_event_helper_edge_cases() -> None:
    assert _text(None) is None
    assert _text([]) is None
    assert _text(_BadText()) is None
    assert _text("  hello\nworld ") == "hello world"
    assert _normalized(None) == ""
    assert _normalized("High-Impact Event") == "high_impact_event"
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


def _runtime_coordinator(
    hass,
    *,
    payload: object = None,
    due: bool = True,
    entry_id: str | None = "Entry-ID",
) -> SimpleNamespace:
    health = SimpleNamespace(consecutive_failures=0)
    return SimpleNamespace(
        hass=hass,
        site_id="1234567",
        config_entry=(SimpleNamespace(entry_id=entry_id) if entry_id else None),
        client=SimpleNamespace(system_dashboard_events=AsyncMock(return_value=payload)),
        _endpoint_family_should_run=lambda family: (
            family == SYSTEM_EVENTS_ENDPOINT_FAMILY and due
        ),
        _endpoint_family_state=lambda family: health,
        _note_endpoint_family_success=MagicMock(),
    )


@pytest.mark.asyncio
async def test_runtime_refresh_clears_repairs_only_for_explicit_resolution(
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
    assert runtime.active_count == 1
    assert runtime.high_impact_count == 1
    assert len(created) == 1
    issue_id, issue = created[0]
    assert issue_id.startswith(f"{SYSTEM_EVENT_REPAIR_PREFIX}entry_id_")
    assert "SERIAL-PRIVATE" not in issue_id
    assert issue["translation_key"] == "active_system_event"
    assert issue["severity"].value == "error"
    assert issue["translation_placeholders"]["device_type"] == (
        "IQ Gateway SERI...TE-1"
    )
    assert issue["data"] == {
        "severity": "critical",
        "device_type": "IQ Gateway SERI...TE-1",
        "event_date": "2026-07-11T01:02:03Z",
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
    assert runtime.active_count == 0
    assert runtime.high_impact_count == 0

    resolved_row = dict(_event_payload()["events"][0])
    resolved_row["cleared_date"] = "2026-07-11T02:00:00Z"
    coordinator.client.system_dashboard_events.return_value = {"events": [resolved_row]}
    await runtime.async_refresh()
    assert deleted == [issue_id]


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
        entry_id=None,
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
    assert snapshot["active_count"] == 1
    assert snapshot["high_impact_count"] == 0
    assert snapshot["severity_counts"] == {"warning": 1}
    assert snapshot["device_type_counts"] == {"IQ Battery": 1}
    assert snapshot["last_success_utc"].endswith("+00:00")
    assert snapshot["using_cached_data"] is True
    assert snapshot["truncated"] is False
    assert runtime.active_events[0].event_type == "Warning"

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


def test_runtime_unavailable_diagnostics(hass) -> None:
    runtime = SystemEventsRuntime(_runtime_coordinator(hass))
    snapshot = runtime.diagnostics()
    assert snapshot["last_success_utc"] is None
    assert snapshot["using_cached_data"] is False
    assert snapshot["truncated"] is False


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
    coordinator.client.system_dashboard_events.return_value = {"events": []}
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
    coordinator.client.system_dashboard_events.return_value = {
        "events": [],
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

    reloaded = SystemEventsRuntime(_runtime_coordinator(hass, payload={"events": []}))
    await reloaded.async_refresh()

    assert deleted == [issue_id]
