"""Read-only VPP/ELRP enrollment and event runtime."""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util import dt as dt_util

from .api import (
    EnphaseLoginWallUnauthorized,
    OptionalEndpointUnavailable,
    Unauthorized,
)
from .api_client.vpp_surface import valid_object_id
from .const import DEFAULT_VPP_EVENTS_ENABLED, OPT_VPP_EVENTS_ENABLED

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

VPP_ENROLLMENT_ENDPOINT_FAMILY = "vpp_enrollment"
VPP_EVENTS_ENDPOINT_FAMILY = "vpp_events"
VPP_EVENT_LIMIT = 500
VPP_EVENT_STALE_AFTER_S = 3600.0
VPP_PROGRAM_CACHE_LIFETIME_S = 604800.0
VPP_TERMINAL_STATUSES = frozenset(
    {"cancelled", "canceled", "completed", "failed", "superseded", "superceded"}
)


def _text(value: object, *, max_length: int = 128) -> str | None:
    """Return bounded scalar text."""

    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return None
    try:
        text = " ".join(str(value).split()).strip()
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    return text[:max_length]


def _normalized(value: object) -> str:
    """Return comparison-safe API text without constraining unknown values."""

    text = _text(value)
    if text is None:
        return ""
    return "_".join(text.casefold().replace("-", " ").split())


def _datetime(value: object) -> datetime | None:
    """Return an aware UTC datetime."""

    text = _text(value, max_length=64)
    if text is None:
        return None
    parsed = dt_util.parse_datetime(text)
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _event_fingerprint(row: dict[str, object]) -> str:
    """Return a stable non-reversible identity for one VPP event."""

    stable_id = _text(row.get("event_id")) or _text(row.get("id"))
    source = (
        f"id:{stable_id}"
        if stable_id
        else "|".join(
            _text(row.get(key)) or ""
            for key in ("start_time", "end_time", "type", "subtype", "status")
        )
    )
    return hashlib.sha256(source.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class VppEvent:
    """Normalized identifier-free VPP event."""

    fingerprint: str
    start: datetime
    end: datetime
    event_type: str
    subtype: str
    status: str
    cancelled: bool = False
    superseded: bool = False

    def actionable(self, now: datetime) -> bool:
        """Return whether this record may still affect the site."""

        return bool(
            self.end > now
            and not self.cancelled
            and not self.superseded
            and _normalized(self.status) not in VPP_TERMINAL_STATUSES
        )


@dataclass(frozen=True, slots=True)
class VppSnapshot:
    """Immutable VPP state included in coordinator publication equality."""

    enrollment_state: str = "unknown"
    available: bool = False
    events: tuple[VppEvent, ...] = ()
    truncated: bool = False


def parse_vpp_events(payload: object) -> tuple[tuple[VppEvent, ...], bool] | None:
    """Normalize a VPP event wrapper, returning None for an invalid shape."""

    if not isinstance(payload, dict):
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None

    parsed: dict[str, VppEvent] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        start = _datetime(row.get("start_time"))
        end = _datetime(row.get("end_time"))
        if start is None or end is None or end <= start:
            continue
        fingerprint = _event_fingerprint(row)
        status = _text(row.get("status")) or "unknown"
        event_type = _text(row.get("type")) or "unknown"
        subtype = _text(row.get("subtype")) or "unknown"
        cancellation = _text(row.get("cancellation_timestamp"), max_length=64)
        parsed[fingerprint] = VppEvent(
            fingerprint=fingerprint,
            start=start,
            end=end,
            event_type=event_type,
            subtype=subtype,
            status=status,
            cancelled=bool(cancellation)
            or _normalized(status) in {"cancelled", "canceled"},
            superseded=(
                row.get("superceded") is True
                or row.get("superseded") is True
                or _normalized(status) in {"superseded", "superceded"}
            ),
        )
    all_events = sorted(
        parsed.values(), key=lambda item: (item.start, item.end, item.fingerprint)
    )
    truncated = len(all_events) > VPP_EVENT_LIMIT
    if truncated:
        now = dt_util.utcnow()
        actionable: list[VppEvent] = []
        other_upcoming: list[VppEvent] = []
        history: list[VppEvent] = []
        for event in all_events:
            if event.end <= now:
                history.append(event)
            elif event.actionable(now):
                actionable.append(event)
            else:
                other_upcoming.append(event)
        retained = actionable[:VPP_EVENT_LIMIT]
        remaining = VPP_EVENT_LIMIT - len(retained)
        if remaining:
            retained.extend(other_upcoming[:remaining])
            remaining = VPP_EVENT_LIMIT - len(retained)
        if remaining:
            retained.extend(history[-remaining:])
        all_events = sorted(
            retained,
            key=lambda item: (item.start, item.end, item.fingerprint),
        )
    return tuple(all_events), truncated


class VppRuntime:
    """Resolve one enrollment and cache read-only VPP event state."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self._enrollment_state = "unknown"
        self._program_id: str | None = None
        self._program_last_confirmed_mono: float | None = None
        self._force_enrollment_lookup = False
        self._events: tuple[VppEvent, ...] = ()
        self._truncated = False
        self._events_last_success_mono: float | None = None
        self._events_last_success_utc: datetime | None = None

    @property
    def enabled(self) -> bool:
        """Return whether the config entry opted into VPP polling."""

        entry = getattr(self.coordinator, "config_entry", None)
        options = getattr(entry, "options", {}) if entry is not None else {}
        return bool(
            options.get(OPT_VPP_EVENTS_ENABLED, DEFAULT_VPP_EVENTS_ENABLED)
            if isinstance(options, Mapping)
            else DEFAULT_VPP_EVENTS_ENABLED
        )

    @property
    def enrollment_state(self) -> str:
        return self._enrollment_state

    @property
    def available(self) -> bool:
        """Return whether a recent valid event response is available."""

        if not self.enabled or self._enrollment_state != "enrolled":
            return False
        last_success = self._events_last_success_mono
        return bool(
            isinstance(last_success, (int, float))
            and time.monotonic() - float(last_success) <= VPP_EVENT_STALE_AFTER_S
        )

    @property
    def events(self) -> tuple[VppEvent, ...]:
        return self._events

    @property
    def snapshot(self) -> VppSnapshot:
        return VppSnapshot(
            enrollment_state=self._enrollment_state if self.enabled else "disabled",
            available=self.available,
            events=self._events if self.enabled else (),
            truncated=self._truncated if self.enabled else False,
        )

    def clear(self) -> None:
        """Clear all enrollment and event state."""

        self._enrollment_state = "unknown"
        self._program_id = None
        self._program_last_confirmed_mono = None
        self._force_enrollment_lookup = False
        self._events = ()
        self._truncated = False
        self._events_last_success_mono = None
        self._events_last_success_utc = None

    def refresh_due(self) -> bool:
        """Return whether either VPP endpoint family may run."""

        if not self.enabled:
            return False
        self._expire_cached_program()
        if self._force_enrollment_lookup:
            return True
        if self._program_id is None:
            return self.coordinator._endpoint_family_should_run(
                VPP_ENROLLMENT_ENDPOINT_FAMILY
            )
        return bool(
            self.coordinator._endpoint_family_should_run(VPP_ENROLLMENT_ENDPOINT_FAMILY)
            or self.coordinator._endpoint_family_should_run(VPP_EVENTS_ENDPOINT_FAMILY)
        )

    def _expire_cached_program(self) -> None:
        """Discard a program that has not been reconfirmed for seven days."""

        confirmed = self._program_last_confirmed_mono
        if (
            self._program_id is not None
            and confirmed is not None
            and time.monotonic() - confirmed > VPP_PROGRAM_CACHE_LIFETIME_S
        ):
            self._program_id = None
            self._program_last_confirmed_mono = None
            self._enrollment_state = "unknown"

    def next_actionable(self, now: datetime | None = None) -> VppEvent | None:
        """Return the current or next non-terminal VPP event."""

        if not self.available:
            return None
        current = dt_util.as_utc(now) if now is not None else dt_util.utcnow()
        return next(
            (event for event in self._events if event.actionable(current)), None
        )

    @staticmethod
    def _safe_failure(summary: str, err: Exception) -> OptionalEndpointUnavailable:
        status = err.status if isinstance(err, aiohttp.ClientResponseError) else None
        suffix = f" (status {status})" if isinstance(status, int) else ""
        return OptionalEndpointUnavailable(f"{summary}{suffix}")

    async def _async_refresh_enrollment(self) -> None:
        force_lookup = self._force_enrollment_lookup
        if not force_lookup and not self.coordinator._endpoint_family_should_run(
            VPP_ENROLLMENT_ENDPOINT_FAMILY
        ):
            return
        self._force_enrollment_lookup = False
        lookup = getattr(self.coordinator.client, "vpp_enrollment_id", None)
        details = getattr(self.coordinator.client, "vpp_enrollment_details", None)
        if not callable(lookup) or not callable(details):
            raise OptionalEndpointUnavailable("VPP enrollment endpoint unavailable")
        try:
            payload = await lookup()
            if not isinstance(payload, dict) or "data" not in payload:
                raise OptionalEndpointUnavailable("Invalid VPP enrollment response")
            raw_enrollment = payload.get("data")
            if raw_enrollment is None or raw_enrollment == "":
                self._enrollment_state = "unenrolled"
                self._program_id = None
                self._program_last_confirmed_mono = None
                self._events = ()
                self._truncated = False
                self._events_last_success_mono = None
                self._events_last_success_utc = None
                self.coordinator._note_endpoint_family_success(
                    VPP_ENROLLMENT_ENDPOINT_FAMILY
                )
                return
            enrollment_id = valid_object_id(raw_enrollment)
            if enrollment_id is None:
                self._enrollment_state = "ambiguous"
                self._program_id = None
                self._program_last_confirmed_mono = None
                raise OptionalEndpointUnavailable("Ambiguous VPP enrollment response")
            detail_payload = await details(enrollment_id)
            detail_data = (
                detail_payload.get("data") if isinstance(detail_payload, dict) else None
            )
            program_id = (
                valid_object_id(detail_data.get("program_id"))
                if isinstance(detail_data, dict)
                else None
            )
            if program_id is None:
                self._enrollment_state = "ambiguous"
                self._program_id = None
                self._program_last_confirmed_mono = None
                raise OptionalEndpointUnavailable("Ambiguous VPP program response")
        except EnphaseLoginWallUnauthorized as err:
            self.coordinator._note_endpoint_family_failure(
                VPP_ENROLLMENT_ENDPOINT_FAMILY,
                self._safe_failure("VPP enrollment unavailable", err),
            )
            return
        except Unauthorized as err:
            raise ConfigEntryAuthFailed from err
        except Exception as err:  # noqa: BLE001
            safe = (
                err
                if isinstance(err, OptionalEndpointUnavailable)
                else self._safe_failure("VPP enrollment unavailable", err)
            )
            self.coordinator._note_endpoint_family_failure(
                VPP_ENROLLMENT_ENDPOINT_FAMILY,
                safe,
            )
            return
        self._enrollment_state = "enrolled"
        self._program_id = program_id
        self._program_last_confirmed_mono = time.monotonic()
        self.coordinator._note_endpoint_family_success(VPP_ENROLLMENT_ENDPOINT_FAMILY)

    async def _async_refresh_events(self) -> None:
        program_id = self._program_id
        if program_id is None or not self.coordinator._endpoint_family_should_run(
            VPP_EVENTS_ENDPOINT_FAMILY
        ):
            return
        fetcher = getattr(self.coordinator.client, "vpp_events", None)
        if not callable(fetcher):
            raise OptionalEndpointUnavailable("VPP events endpoint unavailable")
        try:
            payload = await fetcher(program_id)
            parsed = parse_vpp_events(payload)
            if parsed is None:
                raise OptionalEndpointUnavailable("Invalid VPP events response")
        except EnphaseLoginWallUnauthorized as err:
            self.coordinator._note_endpoint_family_failure(
                VPP_EVENTS_ENDPOINT_FAMILY,
                self._safe_failure("VPP events unavailable", err),
            )
            return
        except Unauthorized as err:
            raise ConfigEntryAuthFailed from err
        except Exception as err:  # noqa: BLE001
            if isinstance(err, aiohttp.ClientResponseError) and err.status in (
                400,
                404,
            ):
                self._program_id = None
                self._program_last_confirmed_mono = None
                self._enrollment_state = "unknown"
                self._force_enrollment_lookup = True
            safe = (
                err
                if isinstance(err, OptionalEndpointUnavailable)
                else self._safe_failure("VPP events unavailable", err)
            )
            self.coordinator._note_endpoint_family_failure(
                VPP_EVENTS_ENDPOINT_FAMILY,
                safe,
            )
            return
        self._events, self._truncated = parsed
        self._enrollment_state = "enrolled"
        self._events_last_success_mono = time.monotonic()
        self._events_last_success_utc = datetime.now(tz=UTC)
        self.coordinator._note_endpoint_family_success(VPP_EVENTS_ENDPOINT_FAMILY)

    async def async_refresh(self) -> None:
        """Refresh enrollment when needed, followed by the event cache."""

        if not self.enabled:
            self.clear()
            return
        self._expire_cached_program()
        await self._async_refresh_enrollment()
        await self._async_refresh_events()

    def diagnostics(self) -> dict[str, object]:
        """Return identifier-free VPP diagnostics."""

        if not self.enabled:
            return {"enabled": False}
        now = dt_util.utcnow()
        status_counts = Counter(_normalized(event.status) for event in self._events)
        type_counts = Counter(_normalized(event.event_type) for event in self._events)
        health = self.coordinator._endpoint_family_state(VPP_EVENTS_ENDPOINT_FAMILY)
        return {
            "enabled": self.enabled,
            "enrollment_state": self._enrollment_state,
            "available": self.available,
            "event_count": len(self._events),
            "actionable_count": sum(event.actionable(now) for event in self._events),
            "status_counts": dict(sorted(status_counts.items())),
            "type_counts": dict(sorted(type_counts.items())),
            "last_success_utc": (
                self._events_last_success_utc.isoformat()
                if self._events_last_success_utc
                else None
            ),
            "using_cached_data": bool(
                self.available and health.consecutive_failures > 0
            ),
            "truncated": self._truncated,
        }
