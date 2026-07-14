"""Read-only System Dashboard event monitoring and repair synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .api import EnphaseLoginWallUnauthorized, OptionalEndpointUnavailable
from .const import (
    DEFAULT_SYSTEM_EVENT_REPAIR_ISSUES,
    DOMAIN,
    OPT_SYSTEM_EVENT_REPAIR_ISSUES,
)
from .log_redaction import redact_text

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)

SYSTEM_EVENTS_ENDPOINT_FAMILY = "system_events"
SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY = "system_event_history"
SYSTEM_EVENT_REPAIR_PREFIX = "active_system_event_"
SYSTEM_EVENT_REPAIR_MISSING_GRACE = timedelta(hours=6)
SYSTEM_EVENT_REPAIR_CHECKPOINT_INTERVAL = timedelta(hours=1)
ACTIVE_EVENTS_ATTRIBUTE_LIMIT = 20
SYSTEM_EVENT_HISTORY_PAGE_SIZE = 200
SYSTEM_EVENT_HISTORY_ROW_LIMIT = 2_000
SYSTEM_EVENT_HISTORY_MAX_PAGES = 100
SYSTEM_EVENT_HISTORY_CACHE_TTL = 900.0
SYSTEM_EVENT_HISTORY_CACHE_MAX_RANGES = 4
SYSTEM_EVENT_HISTORY_INSTANT_DURATION = timedelta(minutes=1)
_TERMINAL_STATES = frozenset(
    {"clear", "cleared", "close", "closed", "inactive", "normal", "resolved"}
)
_HIGH_IMPACT_SEVERITIES = frozenset(
    {"critical", "emergency", "error", "fatal", "severe"}
)
_INFORMATIONAL_LABELS = frozenset({"info", "informational"})
_HISTORY_SERIAL_RE = re.compile(
    r"(?i)(?:sno\.?|serial(?:\s+number)?)\s*[:#=-]?\s*\(?([A-Z0-9-]{6,})"
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


def _timestamp(value: object) -> str | None:
    """Return a bounded UTC timestamp or discard malformed event metadata."""

    text = _text(value)
    if text is None or len(text) > 64:
        return None
    parsed = dt_util.parse_datetime(text)
    if parsed is None or parsed.tzinfo is None:
        return None
    return str(dt_util.as_utc(parsed).isoformat())


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


def _event_is_informational(
    row: dict[str, object],
    *,
    severity: str,
    event_type: str,
    state: str,
) -> bool:
    """Return whether explicit event metadata classifies a row as informational."""

    return any(
        _normalized(value) in _INFORMATIONAL_LABELS
        for value in (
            severity,
            event_type,
            state,
            row.get("status"),
            row.get("event_severity"),
            row.get("severity"),
        )
    )


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


@dataclass(frozen=True, slots=True)
class StandingAlarm:
    """Sanitized normalized System Dashboard standing alarm."""

    fingerprint: str
    severity: str
    device_type: str
    first_set: str | None


@dataclass(frozen=True, slots=True)
class SystemEventHistoryEntry:
    """Sanitized normalized homeowner event-history row."""

    fingerprint: str
    summary: str | None
    description: str | None
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class _HistoryRangeCacheEntry:
    """Short-lived sanitized result for one calendar range."""

    expires_mono: float
    events: tuple[SystemEventHistoryEntry, ...]
    truncated: bool


def _history_epoch(value: object) -> datetime | None:
    """Return an aware UTC datetime for a seconds-or-milliseconds epoch."""

    if value is None or isinstance(value, bool):
        return None
    try:
        timestamp = int(float(str(value).strip()))
        if timestamp > 10**12:
            timestamp //= 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, TypeError, ValueError):
        return None


def _history_identifiers(row: dict[str, object]) -> tuple[str, ...]:
    """Return identifier candidates used only while sanitizing a history row."""

    identifiers: list[str] = []
    serial = _text(row.get("serial_num"))
    if serial:
        identifiers.append(serial)
    for key in ("description", "type", "recommended_action"):
        text = _text(row.get(key))
        if text:
            identifiers.extend(
                match.group(1) for match in _HISTORY_SERIAL_RE.finditer(text)
            )
    impacted = row.get("devices_impacted")
    if isinstance(impacted, list):
        for item in impacted:
            text = _text(item)
            if not text:
                continue
            identifiers.extend(
                match.group(1) for match in _HISTORY_SERIAL_RE.finditer(text)
            )
    return tuple(dict.fromkeys(identifiers))


def _history_fingerprint(row: dict[str, object]) -> str:
    """Return a stable non-reversible history-row key."""

    stable_id = _text(row.get("id"))
    if stable_id:
        source = f"id:{stable_id}"
    else:
        source = "|".join(
            _text(row.get(key)) or ""
            for key in ("event_key", "event_date", "event_start_date", "serial_num")
        )
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def _history_event_key_label(value: object) -> str | None:
    """Return a compact human-readable fallback for an event key."""

    text = _text(value)
    if not text:
        return None
    return " ".join(text.replace("_", " ").replace("-", " ").split()).capitalize()


def parse_homeowner_event_history(
    payload: object,
    *,
    site_id: str,
) -> tuple[SystemEventHistoryEntry, ...] | None:
    """Parse and sanitize one homeowner event-history payload."""

    if not isinstance(payload, dict):
        return None
    rows = payload.get("events")
    if not isinstance(rows, list):
        return None
    events: dict[str, SystemEventHistoryEntry] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        start = _history_epoch(row.get("event_start_date")) or _history_epoch(
            row.get("event_date")
        )
        if start is None:
            continue
        clear = _history_epoch(row.get("event_clear_date"))
        end = (
            clear
            if clear is not None and clear > start
            else (start + SYSTEM_EVENT_HISTORY_INSTANT_DURATION)
        )
        identifiers = _history_identifiers(row)
        description = redact_text(
            _text(row.get("description")) or "",
            site_ids=(site_id,),
            identifiers=identifiers,
            max_length=512,
        )
        event_type = redact_text(
            _text(row.get("type")) or "",
            site_ids=(site_id,),
            identifiers=identifiers,
            max_length=120,
        )
        event_key = redact_text(
            _history_event_key_label(row.get("event_key")) or "",
            site_ids=(site_id,),
            identifiers=identifiers,
            max_length=120,
        )
        recommended_action = redact_text(
            _text(row.get("recommended_action")) or "",
            site_ids=(site_id,),
            identifiers=identifiers,
            max_length=512,
        )
        fingerprint = _history_fingerprint(row)
        events.setdefault(
            fingerprint,
            SystemEventHistoryEntry(
                fingerprint=fingerprint,
                summary=description or event_type or event_key or None,
                description=recommended_action or None,
                start=start,
                end=end,
            ),
        )
    return tuple(sorted(events.values(), key=lambda event: event.start))


def parse_active_system_events(
    payload: object,
    *,
    site_id: str,
) -> tuple[SystemEvent, ...]:
    """Parse active events while discarding raw identifiers and message details."""

    events, _resolved = _parse_system_event_snapshot(payload, site_id=site_id)
    return events


def parse_standing_alarms(
    payload: object,
    *,
    site_id: str,
) -> tuple[StandingAlarm, ...]:
    """Parse standing alarms while discarding identifiers and free-form details."""

    if not isinstance(payload, dict):
        return ()
    rows = payload.get("alarms")
    if not isinstance(rows, list):
        return ()
    serials = [
        serial
        for row in rows
        if isinstance(row, dict)
        and (serial := _text(row.get("serial_num"))) is not None
    ]
    alarms: list[StandingAlarm] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        fingerprint = _event_fingerprint(
            {
                "id": row.get("id"),
                "serial_number": row.get("serial_num"),
                "event_type": row.get("description"),
                "event_date": row.get("first_set"),
            }
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        severity_text = redact_text(
            _text(row.get("severity")) or "",
            site_ids=(site_id,),
            identifiers=serials,
            max_length=40,
        )
        severity = _normalized(severity_text) or "unknown"
        device_type = redact_text(
            _text(row.get("type")) or "unknown",
            site_ids=(site_id,),
            identifiers=serials,
            max_length=80,
        )
        first_set = redact_text(
            _text(row.get("first_set")) or "",
            site_ids=(site_id,),
            identifiers=serials,
            max_length=80,
        )
        alarms.append(
            StandingAlarm(
                fingerprint=fingerprint,
                severity=severity,
                device_type=device_type or "unknown",
                first_set=first_set or None,
            )
        )
    return tuple(alarms)


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
        if _event_is_informational(
            row,
            severity=severity,
            event_type=event_type,
            state=state,
        ):
            continue
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
                event_date=_timestamp(row.get("event_date")),
                updated_at=_timestamp(row.get("updated_at")),
                high_impact=severity in _HIGH_IMPACT_SEVERITIES,
            )
        )
    return tuple(events), frozenset(resolved)


class SystemEventsRuntime:
    """Fetch, cache, summarize, and synchronize active system events."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self._events: tuple[SystemEvent, ...] = ()
        self._standing_alarms: tuple[StandingAlarm, ...] = ()
        self._last_success_utc: datetime | None = None
        self._reported_issue_ids: set[str] = set()
        self._active_reported_issue_ids: set[str] = set()
        self._repair_last_seen_utc: dict[str, datetime] = {}
        self._repair_checkpoint_utc: dict[str, datetime] = {}
        self._snapshot_truncated = False
        self._history_last_success_utc: datetime | None = None
        self._history_probe_events: tuple[SystemEventHistoryEntry, ...] = ()
        self._history_range_cache: OrderedDict[
            tuple[str, str, str], _HistoryRangeCacheEntry
        ] = OrderedDict()
        self._history_lock = asyncio.Lock()
        self._history_using_cached_data = False
        self._history_truncated = False

    @property
    def active_events(self) -> tuple[SystemEvent, ...]:
        """Return the sanitized active-event cache."""

        return self._events

    @property
    def available(self) -> bool:
        """Return whether at least one valid response has been received."""

        return self._last_success_utc is not None

    @property
    def history_available(self) -> bool:
        """Return whether the homeowner event-history endpoint has succeeded."""

        return self._history_last_success_utc is not None

    @property
    def active_count(self) -> int:
        """Return the number of records that drive the Problem state."""

        return self.standing_alarm_count + self.high_impact_count

    @property
    def standing_alarm_count(self) -> int:
        """Return the number of authoritative standing alarms."""

        return len(self._standing_alarms)

    @property
    def high_impact_count(self) -> int:
        """Return the number of active error/critical events."""

        return sum(event.high_impact for event in self._events)

    @property
    def problem_active(self) -> bool:
        """Return whether alarms or explicitly high-impact events indicate a problem."""

        return self.standing_alarm_count > 0 or self.high_impact_count > 0

    @property
    def active_event_attributes(self) -> tuple[dict[str, object], ...]:
        """Return bounded identifier-free events that drive the Problem state."""

        summaries: list[dict[str, object]] = [
            {
                "type": "Standing Alarm",
                "device_type": alarm.device_type,
                "state": "active",
                "event_date": alarm.first_set,
                "updated_at": None,
            }
            for alarm in self._standing_alarms
        ]
        summaries.extend(
            {
                "type": event.event_type,
                "device_type": event.device_type,
                "state": event.state,
                "event_date": event.event_date,
                "updated_at": event.updated_at,
            }
            for event in self._events
            if event.high_impact
        )
        return tuple(summaries[:ACTIVE_EVENTS_ATTRIBUTE_LIMIT])

    def refresh_due(self) -> bool:
        """Return whether the optional event endpoint may be polled now."""

        return self.coordinator._endpoint_family_should_run(
            SYSTEM_EVENTS_ENDPOINT_FAMILY
        )

    def history_refresh_due(self) -> bool:
        """Return whether the optional history endpoint may be probed now."""

        return self.coordinator._endpoint_family_should_run(
            SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
        )

    def _history_locale(self) -> str:
        """Return the Home Assistant locale used for Enphase event descriptions."""

        config = getattr(getattr(self.coordinator, "hass", None), "config", None)
        locale = _text(getattr(config, "language", None))
        return locale or "en"

    def _history_cache_key(
        self,
        start: datetime,
        end: datetime,
        locale: str,
    ) -> tuple[str, str, str]:
        return (
            dt_util.as_utc(start).isoformat(),
            dt_util.as_utc(end).isoformat(),
            locale,
        )

    def _history_cached_range(
        self,
        key: tuple[str, str, str],
        *,
        allow_expired: bool = False,
    ) -> _HistoryRangeCacheEntry | None:
        entry = self._history_range_cache.get(key)
        if entry is None:
            return None
        if not allow_expired and time.monotonic() >= entry.expires_mono:
            return None
        self._history_range_cache.move_to_end(key)
        return entry

    def _store_history_range(
        self,
        key: tuple[str, str, str],
        events: tuple[SystemEventHistoryEntry, ...],
        *,
        truncated: bool,
    ) -> None:
        self._history_range_cache[key] = _HistoryRangeCacheEntry(
            expires_mono=time.monotonic() + SYSTEM_EVENT_HISTORY_CACHE_TTL,
            events=events,
            truncated=truncated,
        )
        self._history_range_cache.move_to_end(key)
        while len(self._history_range_cache) > SYSTEM_EVENT_HISTORY_CACHE_MAX_RANGES:
            self._history_range_cache.popitem(last=False)

    async def async_refresh_history(self) -> None:
        """Probe the homeowner history endpoint without blocking core setup."""

        if not self.history_refresh_due():
            return
        fetcher = getattr(self.coordinator.client, "homeowner_events_page", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher(
                next_cursor="start",
                page_size=SYSTEM_EVENT_HISTORY_PAGE_SIZE,
                locale=self._history_locale(),
            )
        except EnphaseLoginWallUnauthorized:
            raise
        except Exception as err:  # noqa: BLE001
            self.coordinator._note_endpoint_family_failure(
                SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY,
                err,
            )
            return
        parsed = parse_homeowner_event_history(
            payload,
            site_id=str(self.coordinator.site_id),
        )
        if parsed is None:
            self.coordinator._note_endpoint_family_failure(
                SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY,
                OptionalEndpointUnavailable("System event history unavailable"),
            )
            return
        self._history_probe_events = parsed
        self._history_last_success_utc = dt_util.utcnow()
        self._history_using_cached_data = False
        self.coordinator._note_endpoint_family_success(
            SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
        )

    async def async_history_events(
        self,
        start: datetime,
        end: datetime,
    ) -> tuple[SystemEventHistoryEntry, ...]:
        """Return sanitized homeowner events overlapping a calendar range."""

        locale = self._history_locale()
        key = self._history_cache_key(start, end, locale)
        cached = self._history_cached_range(key)
        if cached is not None:
            self._history_using_cached_data = False
            self._history_truncated = cached.truncated
            return cached.events

        async with self._history_lock:
            cached = self._history_cached_range(key)
            if cached is not None:
                self._history_using_cached_data = False
                self._history_truncated = cached.truncated
                return cached.events

            health = self.coordinator._endpoint_family_state(
                SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
            )
            if (
                health.consecutive_failures > 0
                and self.coordinator._endpoint_family_wait_active(
                    SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
                )
            ):
                stale = (
                    self._history_cached_range(key, allow_expired=True)
                    if self.coordinator._endpoint_family_can_use_stale(
                        SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
                    )
                    else None
                )
                self._history_using_cached_data = stale is not None
                self._history_truncated = (
                    stale.truncated if stale is not None else False
                )
                return stale.events if stale is not None else ()

            fetcher = getattr(self.coordinator.client, "homeowner_events_page", None)
            if not callable(fetcher):
                return ()
            cursor = "start"
            seen_cursors: set[str] = set()
            rows_seen = 0
            pages_seen = 0
            events: dict[str, SystemEventHistoryEntry] = {}
            truncated = False
            try:
                while rows_seen < SYSTEM_EVENT_HISTORY_ROW_LIMIT:
                    pages_seen += 1
                    if pages_seen > SYSTEM_EVENT_HISTORY_MAX_PAGES:
                        truncated = True
                        break
                    payload = await fetcher(
                        next_cursor=cursor,
                        page_size=SYSTEM_EVENT_HISTORY_PAGE_SIZE,
                        locale=locale,
                    )
                    if not isinstance(payload, dict):
                        raise OptionalEndpointUnavailable(
                            "System event history unavailable"
                        )
                    rows = payload.get("events")
                    if not isinstance(rows, list):
                        raise OptionalEndpointUnavailable(
                            "System event history unavailable"
                        )
                    remaining = SYSTEM_EVENT_HISTORY_ROW_LIMIT - rows_seen
                    bounded_rows = rows[:remaining]
                    bounded_payload = dict(payload)
                    bounded_payload["events"] = bounded_rows
                    parsed = parse_homeowner_event_history(
                        bounded_payload,
                        site_id=str(self.coordinator.site_id),
                    )
                    if parsed is None:  # pragma: no cover - shape validated above
                        raise OptionalEndpointUnavailable(
                            "System event history unavailable"
                        )
                    page_truncated = len(rows) > len(bounded_rows)
                    if page_truncated:
                        truncated = True
                    rows_seen += len(bounded_rows)
                    for event in parsed:
                        events.setdefault(event.fingerprint, event)
                    oldest = min((event.start for event in parsed), default=None)
                    next_value = payload.get("next")
                    next_cursor = _text(next_value)
                    if oldest is not None and oldest < start:
                        break
                    if page_truncated:
                        break
                    if not next_cursor:
                        break
                    if next_cursor in seen_cursors or next_cursor == cursor:
                        truncated = True
                        break
                    if rows_seen >= SYSTEM_EVENT_HISTORY_ROW_LIMIT:
                        truncated = True
                        break
                    seen_cursors.add(cursor)
                    cursor = next_cursor
            except EnphaseLoginWallUnauthorized:
                raise
            except Exception as err:  # noqa: BLE001
                self.coordinator._note_endpoint_family_failure(
                    SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY,
                    err,
                )
                stale = (
                    self._history_cached_range(key, allow_expired=True)
                    if self.coordinator._endpoint_family_can_use_stale(
                        SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
                    )
                    else None
                )
                self._history_using_cached_data = stale is not None
                self._history_truncated = (
                    stale.truncated if stale is not None else False
                )
                return stale.events if stale is not None else ()

            overlapping = tuple(
                sorted(
                    (
                        event
                        for event in events.values()
                        if event.end > start and event.start < end
                    ),
                    key=lambda event: event.start,
                )
            )
            self._store_history_range(key, overlapping, truncated=truncated)
            self._history_last_success_utc = dt_util.utcnow()
            self._history_using_cached_data = False
            self._history_truncated = truncated
            self.coordinator._note_endpoint_family_success(
                SYSTEM_EVENT_HISTORY_ENDPOINT_FAMILY
            )
            return overlapping

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

    def _issue_id(self, alarm: StandingAlarm) -> str:
        return f"{SYSTEM_EVENT_REPAIR_PREFIX}{self._entry_suffix()}_{alarm.fingerprint}"

    @property
    def repairs_enabled(self) -> bool:
        """Return whether System Event Repair notifications are enabled."""

        config_entry = getattr(self.coordinator, "config_entry", None)
        options = getattr(config_entry, "options", {})
        if not isinstance(options, Mapping):
            return DEFAULT_SYSTEM_EVENT_REPAIR_ISSUES
        return bool(
            options.get(
                OPT_SYSTEM_EVENT_REPAIR_ISSUES,
                DEFAULT_SYSTEM_EVENT_REPAIR_ISSUES,
            )
        )

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

    def _clear_repairs(self) -> None:
        """Remove all persisted System Event Repairs for this config entry."""

        for issue_id in self._existing_issue_ids():
            ir.async_delete_issue(self.coordinator.hass, DOMAIN, issue_id)
        self._reported_issue_ids.clear()
        self._active_reported_issue_ids.clear()
        self._repair_last_seen_utc.clear()
        self._repair_checkpoint_utc.clear()

    def _sync_repairs(
        self,
        *,
        observed_at: datetime,
        authoritative: bool,
        resolved_fingerprints: frozenset[str],
    ) -> None:
        """Synchronize Repairs from the authoritative standing-alarm snapshot."""

        if not self.repairs_enabled:
            self._clear_repairs()
            return

        active_alarms = {
            self._issue_id(alarm): alarm
            for alarm in self._standing_alarms
            if alarm.fingerprint not in resolved_fingerprints
        }
        active_issue_ids = set(active_alarms)
        self._restore_repair_last_seen()
        existing = self._existing_issue_ids()
        active_existing = self._existing_issue_ids(active_only=True)
        for issue_id in active_alarms:
            self._repair_last_seen_utc[issue_id] = observed_at
        missing_issue_ids = existing - active_issue_ids
        stale_issue_ids: set[str] = set()
        if authoritative:
            for issue_id in missing_issue_ids:
                last_seen = self._repair_last_seen_utc.setdefault(issue_id, observed_at)
                if observed_at - last_seen >= SYSTEM_EVENT_REPAIR_MISSING_GRACE:
                    stale_issue_ids.add(issue_id)
        resolved_issue_ids = {
            f"{SYSTEM_EVENT_REPAIR_PREFIX}{self._entry_suffix()}_{fingerprint}"
            for fingerprint in resolved_fingerprints
        }
        delete_issue_ids = stale_issue_ids | (existing & resolved_issue_ids)
        for issue_id in delete_issue_ids:
            ir.async_delete_issue(self.coordinator.hass, DOMAIN, issue_id)
            self._repair_last_seen_utc.pop(issue_id, None)
            self._repair_checkpoint_utc.pop(issue_id, None)
        for issue_id, alarm in active_alarms.items():
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
                    "severity": alarm.severity,
                    "device_type": alarm.device_type,
                    "event_date": alarm.first_set or "unknown",
                },
                data={
                    "severity": alarm.severity,
                    "device_type": alarm.device_type,
                    "event_date": alarm.first_set,
                    "last_seen_utc": observed_at.isoformat(),
                },
            )
            self._repair_checkpoint_utc[issue_id] = observed_at
        self._reported_issue_ids = (existing | set(active_alarms)) - delete_issue_ids
        self._active_reported_issue_ids = set(active_alarms)

    async def async_refresh(self) -> None:
        """Refresh events, retaining cached state across optional failures."""

        if not self.repairs_enabled:
            self._clear_repairs()
        if not self.refresh_due():
            return
        fetcher = getattr(self.coordinator.client, "system_dashboard_events", None)
        alarm_fetcher = getattr(
            self.coordinator.client,
            "system_dashboard_standing_alarms",
            None,
        )
        if not callable(fetcher) or not callable(alarm_fetcher):
            raise OptionalEndpointUnavailable("System events endpoint unavailable")
        payload = await fetcher()
        alarm_payload = await alarm_fetcher()
        if not isinstance(payload, dict) or not isinstance(alarm_payload, dict):
            raise OptionalEndpointUnavailable("System events endpoint unavailable")
        self._events, resolved_fingerprints = _parse_system_event_snapshot(
            payload,
            site_id=str(self.coordinator.site_id),
        )
        self._standing_alarms = tuple(
            alarm
            for alarm in parse_standing_alarms(
                alarm_payload,
                site_id=str(self.coordinator.site_id),
            )
            if alarm.fingerprint not in resolved_fingerprints
        )
        observed_at = dt_util.utcnow()
        self._snapshot_truncated = (
            payload.get("_enphase_ev_truncated") is True
            or alarm_payload.get("_enphase_ev_truncated") is True
        )
        self._last_success_utc = observed_at
        self.coordinator._note_endpoint_family_success(SYSTEM_EVENTS_ENDPOINT_FAMILY)
        self._sync_repairs(
            observed_at=observed_at,
            authoritative=not self._snapshot_truncated,
            resolved_fingerprints=resolved_fingerprints,
        )
        _LOGGER.debug(
            "System event summary refreshed for site [site]: active=%s "
            "high_impact=%s standing_alarms=%s",
            self.active_count,
            self.high_impact_count,
            self.standing_alarm_count,
        )

    def diagnostics(self) -> dict[str, object]:
        """Return an identifier-free diagnostic summary."""

        severities = Counter(alarm.severity for alarm in self._standing_alarms)
        device_types = Counter(alarm.device_type for alarm in self._standing_alarms)
        device_types.update(
            event.device_type for event in self._events if event.high_impact
        )
        health = self.coordinator._endpoint_family_state(SYSTEM_EVENTS_ENDPOINT_FAMILY)
        return {
            "available": self.available,
            "active_count": self.active_count,
            "high_impact_count": self.high_impact_count,
            "standing_alarm_count": self.standing_alarm_count,
            "severity_counts": dict(sorted(severities.items())),
            "device_type_counts": dict(sorted(device_types.items())),
            "last_success_utc": (
                self._last_success_utc.isoformat() if self._last_success_utc else None
            ),
            "using_cached_data": bool(
                self.available and health.consecutive_failures > 0
            ),
            "truncated": self._snapshot_truncated,
            "repairs_enabled": self.repairs_enabled,
        }

    def history_diagnostics(self) -> dict[str, object]:
        """Return identifier-free homeowner event-history diagnostics."""

        cached_fingerprints = {
            event.fingerprint
            for entry in self._history_range_cache.values()
            for event in entry.events
        }
        return {
            "available": self.history_available,
            "cached_range_count": len(self._history_range_cache),
            "cached_event_count": len(cached_fingerprints),
            "last_success_utc": (
                self._history_last_success_utc.isoformat()
                if self._history_last_success_utc
                else None
            ),
            "using_cached_data": self._history_using_cached_data,
            "truncated": self._history_truncated,
        }
