"""Read-only System Dashboard event monitoring and repair synchronization."""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .api import OptionalEndpointUnavailable
from .const import DOMAIN
from .log_redaction import redact_text

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)

SYSTEM_EVENTS_ENDPOINT_FAMILY = "system_events"
SYSTEM_EVENT_REPAIR_PREFIX = "active_system_event_"
SYSTEM_EVENT_REPAIR_MISSING_GRACE = timedelta(hours=6)
SYSTEM_EVENT_REPAIR_CHECKPOINT_INTERVAL = timedelta(hours=1)
_TERMINAL_STATES = frozenset(
    {"clear", "cleared", "close", "closed", "inactive", "normal", "resolved"}
)
_HIGH_IMPACT_SEVERITIES = frozenset(
    {"critical", "emergency", "error", "fatal", "severe"}
)


def _text(value: object) -> str | None:
    """Return compact text for scalar event fields."""

    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    try:
        text = " ".join(str(value).split()).strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None


def _normalized(value: object) -> str:
    """Return a comparison-safe event label."""

    text = _text(value)
    if not text:
        return ""
    return "_".join(text.casefold().replace("-", " ").split())


def _lookup_catalog(payload: object) -> dict[str, dict[str, object]]:
    """Index a dashboard lookup catalog by both id and name."""

    if not isinstance(payload, list):
        return {}
    out: dict[str, dict[str, object]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        for key in ("id", "name"):
            normalized = _normalized(item.get(key))
            if normalized:
                out[normalized] = item
    return out


def _catalog_label(
    value: object,
    catalog: dict[str, dict[str, object]],
) -> str | None:
    """Resolve a lookup id to its display name when available."""

    entry = catalog.get(_normalized(value))
    if entry is not None:
        return _text(entry.get("name")) or _text(value)
    return _text(value)


def _event_severity(
    row: dict[str, object],
    *,
    event_types: dict[str, dict[str, object]],
    severities: dict[str, dict[str, object]],
) -> str:
    """Return an explicit event severity without inferring from free text."""

    candidates: list[object] = [
        row.get("event_severity"),
        row.get("severity"),
        row.get("severity_id"),
    ]
    event_type = event_types.get(_normalized(row.get("event_type")))
    if event_type is not None:
        candidates.extend(
            (
                event_type.get("event_severity"),
                event_type.get("severity"),
                event_type.get("severity_id"),
            )
        )
    if _normalized(row.get("event_state")) in _HIGH_IMPACT_SEVERITIES:
        candidates.append(row.get("event_state"))
    for candidate in candidates:
        label = _catalog_label(candidate, severities)
        if label:
            return _normalized(label)
    return "unknown"


def _event_is_active(row: dict[str, object], state: str) -> bool:
    """Return whether an event is still active according to explicit fields."""

    if _text(row.get("cleared_date")):
        return False
    return _normalized(state) not in _TERMINAL_STATES


def _event_fingerprint(row: dict[str, object]) -> str:
    """Return a stable, non-reversible event key for issue IDs."""

    stable_id = _text(row.get("id")) or _text(row.get("alarm_id"))
    if stable_id:
        source = f"id:{stable_id}"
    else:
        source = "|".join(
            _text(row.get(key)) or ""
            for key in ("serial_number", "event_type", "event_date")
        )
    return hashlib.sha256(source.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class SystemEvent:
    """Sanitized normalized System Dashboard event."""

    fingerprint: str
    event_type: str
    device_type: str
    severity: str
    state: str
    event_date: str | None
    updated_at: str | None
    high_impact: bool


def parse_active_system_events(
    payload: object,
    *,
    site_id: str,
) -> tuple[SystemEvent, ...]:
    """Parse active events while discarding raw identifiers and message details."""

    events, _resolved = _parse_system_event_snapshot(payload, site_id=site_id)
    return events


def _parse_system_event_snapshot(
    payload: object,
    *,
    site_id: str,
) -> tuple[tuple[SystemEvent, ...], frozenset[str]]:
    """Return sanitized active rows and explicitly resolved fingerprints."""

    if not isinstance(payload, dict):
        return (), frozenset()
    rows = payload.get("events")
    if not isinstance(rows, list):
        return (), frozenset()
    states = _lookup_catalog(payload.get("event_states"))
    severities = _lookup_catalog(payload.get("event_severities"))
    event_types = _lookup_catalog(payload.get("event_types"))
    serials = [
        serial
        for row in rows
        if isinstance(row, dict)
        and (serial := _text(row.get("serial_number"))) is not None
    ]
    events: list[SystemEvent] = []
    resolved: set[str] = set()
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = _catalog_label(row.get("event_state"), states) or "unknown"
        fingerprint = _event_fingerprint(row)
        if not _event_is_active(row, state):
            resolved.add(fingerprint)
            continue
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        severity = _event_severity(
            row,
            event_types=event_types,
            severities=severities,
        )
        event_type = _catalog_label(row.get("event_type"), event_types) or "unknown"
        event_type = redact_text(
            event_type,
            site_ids=(site_id,),
            identifiers=serials,
            max_length=120,
        )
        device_type = redact_text(
            _text(row.get("device_type")) or "unknown",
            site_ids=(site_id,),
            identifiers=serials,
            max_length=80,
        )
        events.append(
            SystemEvent(
                fingerprint=fingerprint,
                event_type=event_type or "unknown",
                device_type=device_type or "unknown",
                severity=severity,
                state=_normalized(state) or "unknown",
                event_date=_text(row.get("event_date")),
                updated_at=_text(row.get("updated_at")),
                high_impact=severity in _HIGH_IMPACT_SEVERITIES,
            )
        )
    return tuple(events), frozenset(resolved)


class SystemEventsRuntime:
    """Fetch, cache, summarize, and synchronize active system events."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self._events: tuple[SystemEvent, ...] = ()
        self._last_success_utc: datetime | None = None
        self._reported_issue_ids: set[str] = set()
        self._active_reported_issue_ids: set[str] = set()
        self._repair_last_seen_utc: dict[str, datetime] = {}
        self._repair_checkpoint_utc: dict[str, datetime] = {}
        self._snapshot_truncated = False

    @property
    def active_events(self) -> tuple[SystemEvent, ...]:
        """Return the sanitized active-event cache."""

        return self._events

    @property
    def available(self) -> bool:
        """Return whether at least one valid response has been received."""

        return self._last_success_utc is not None

    @property
    def active_count(self) -> int:
        """Return the current number of active events."""

        return len(self._events)

    @property
    def high_impact_count(self) -> int:
        """Return the number of active error/critical events."""

        return sum(event.high_impact for event in self._events)

    def refresh_due(self) -> bool:
        """Return whether the optional event endpoint may be polled now."""

        return self.coordinator._endpoint_family_should_run(
            SYSTEM_EVENTS_ENDPOINT_FAMILY
        )

    def _entry_suffix(self) -> str:
        entry_id = getattr(
            getattr(self.coordinator, "config_entry", None), "entry_id", ""
        )
        normalized = "".join(
            char.casefold() if char.isalnum() else "_" for char in str(entry_id)
        ).strip("_")
        if normalized:
            return normalized
        return hashlib.sha256(str(self.coordinator.site_id).encode()).hexdigest()[:12]

    def _issue_id(self, event: SystemEvent) -> str:
        return f"{SYSTEM_EVENT_REPAIR_PREFIX}{self._entry_suffix()}_{event.fingerprint}"

    def _registry_issue_entries(self) -> dict[str, object] | None:
        """Return persisted event Repair entries for this config entry."""

        registry = ir.async_get(self.coordinator.hass)
        issues = getattr(registry, "issues", {})
        if not isinstance(issues, dict):
            return None
        entry_prefix = f"{SYSTEM_EVENT_REPAIR_PREFIX}{self._entry_suffix()}_"
        entries: dict[str, object] = {}
        for key, entry in issues.items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or key[0] != DOMAIN
                or not isinstance(key[1], str)
                or not key[1].startswith(entry_prefix)
            ):
                continue
            entries[key[1]] = entry
        return entries

    def _existing_issue_ids(self, *, active_only: bool = False) -> set[str]:
        """Return event issue IDs already persisted by Home Assistant."""

        entries = self._registry_issue_entries()
        if entries is None:
            return set(
                self._active_reported_issue_ids
                if active_only
                else self._reported_issue_ids
            )
        existing = {
            issue_id
            for issue_id, entry in entries.items()
            if not active_only or getattr(entry, "active", True) is True
        }
        if active_only:
            return existing | (self._active_reported_issue_ids - entries.keys())
        return existing | self._reported_issue_ids

    def _restore_repair_last_seen(self) -> None:
        """Restore persisted Repair checkpoints from issue-registry data."""

        entries = self._registry_issue_entries()
        if not entries:
            return
        for issue_id, entry in entries.items():
            data = getattr(entry, "data", None)
            if not isinstance(data, dict):
                continue
            raw_last_seen = data.get("last_seen_utc")
            if not isinstance(raw_last_seen, str):
                continue
            last_seen = dt_util.parse_datetime(raw_last_seen)
            if last_seen is None or last_seen.tzinfo is None:
                continue
            last_seen = dt_util.as_utc(last_seen)
            self._repair_last_seen_utc.setdefault(issue_id, last_seen)
            self._repair_checkpoint_utc.setdefault(issue_id, last_seen)

    def _sync_repairs(
        self,
        resolved_fingerprints: frozenset[str],
        *,
        observed_at: datetime,
        authoritative: bool,
    ) -> None:
        """Create Repairs and age missing rows only after a complete snapshot."""

        active_high_impact = {
            self._issue_id(event): event for event in self._events if event.high_impact
        }
        active_issue_ids = {self._issue_id(event) for event in self._events}
        resolved_issue_ids = {
            f"{SYSTEM_EVENT_REPAIR_PREFIX}{self._entry_suffix()}_{fingerprint}"
            for fingerprint in resolved_fingerprints
        }
        self._restore_repair_last_seen()
        existing = self._existing_issue_ids()
        active_existing = self._existing_issue_ids(active_only=True)
        for issue_id in active_high_impact:
            self._repair_last_seen_utc[issue_id] = observed_at
        missing_issue_ids = existing - active_issue_ids - resolved_issue_ids
        stale_issue_ids: set[str] = set()
        if authoritative:
            for issue_id in missing_issue_ids:
                last_seen = self._repair_last_seen_utc.setdefault(issue_id, observed_at)
                if observed_at - last_seen >= SYSTEM_EVENT_REPAIR_MISSING_GRACE:
                    stale_issue_ids.add(issue_id)
        delete_issue_ids = (
            (existing & resolved_issue_ids)
            | ((existing & active_issue_ids) - active_high_impact.keys())
            | stale_issue_ids
        )
        for issue_id in delete_issue_ids:
            ir.async_delete_issue(self.coordinator.hass, DOMAIN, issue_id)
            self._repair_last_seen_utc.pop(issue_id, None)
            self._repair_checkpoint_utc.pop(issue_id, None)
        for issue_id, event in active_high_impact.items():
            checkpoint = self._repair_checkpoint_utc.get(issue_id)
            if (
                issue_id in active_existing
                and checkpoint is not None
                and observed_at - checkpoint < SYSTEM_EVENT_REPAIR_CHECKPOINT_INTERVAL
            ):
                continue
            ir.async_create_issue(
                self.coordinator.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="active_system_event",
                translation_placeholders={
                    "site_id": str(self.coordinator.site_id),
                    "severity": event.severity,
                    "device_type": event.device_type,
                    "event_date": event.event_date or "unknown",
                },
                data={
                    "severity": event.severity,
                    "device_type": event.device_type,
                    "event_date": event.event_date,
                    "last_seen_utc": observed_at.isoformat(),
                },
            )
            self._repair_checkpoint_utc[issue_id] = observed_at
        self._reported_issue_ids = (
            existing | set(active_high_impact)
        ) - delete_issue_ids
        self._active_reported_issue_ids = set(active_high_impact)

    async def async_refresh(self) -> None:
        """Refresh events, retaining cached state across optional failures."""

        if not self.refresh_due():
            return
        fetcher = getattr(self.coordinator.client, "system_dashboard_events", None)
        if not callable(fetcher):
            raise OptionalEndpointUnavailable("System events endpoint unavailable")
        payload = await fetcher()
        if not isinstance(payload, dict):
            raise OptionalEndpointUnavailable("System events endpoint unavailable")
        self._events, resolved_fingerprints = _parse_system_event_snapshot(
            payload,
            site_id=str(self.coordinator.site_id),
        )
        observed_at = dt_util.utcnow()
        self._snapshot_truncated = payload.get("_enphase_ev_truncated") is True
        self._last_success_utc = observed_at
        self.coordinator._note_endpoint_family_success(SYSTEM_EVENTS_ENDPOINT_FAMILY)
        self._sync_repairs(
            resolved_fingerprints,
            observed_at=observed_at,
            authoritative=not self._snapshot_truncated,
        )
        _LOGGER.debug(
            "System event summary refreshed for site [site]: active=%s high_impact=%s",
            self.active_count,
            self.high_impact_count,
        )

    def diagnostics(self) -> dict[str, object]:
        """Return an identifier-free diagnostic summary."""

        severities = Counter(event.severity for event in self._events)
        device_types = Counter(event.device_type for event in self._events)
        health = self.coordinator._endpoint_family_state(SYSTEM_EVENTS_ENDPOINT_FAMILY)
        return {
            "available": self.available,
            "active_count": self.active_count,
            "high_impact_count": self.high_impact_count,
            "severity_counts": dict(sorted(severities.items())),
            "device_type_counts": dict(sorted(device_types.items())),
            "last_success_utc": (
                self._last_success_utc.isoformat() if self._last_success_utc else None
            ),
            "using_cached_data": bool(
                self.available and health.consecutive_failures > 0
            ),
            "truncated": self._snapshot_truncated,
        }
