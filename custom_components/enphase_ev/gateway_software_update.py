"""Read-only IQ Gateway software-update progress monitoring."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .log_redaction import redact_text
from .parsing_helpers import coerce_optional_text as _text
from .runtime_helpers import (
    iso_or_none as _iso_or_none,
    monotonic_deadline_to_utc_iso as _mono_to_utc_iso,
)

_LOGGER = logging.getLogger(__name__)

GATEWAY_UPDATE_IDLE_TTL_SECONDS = 15 * 60
GATEWAY_UPDATE_ACTIVE_TTL_SECONDS = 30
GATEWAY_UPDATE_RETRY_BACKOFF_SECONDS = 15 * 60
GATEWAY_UPDATE_FETCH_TIMEOUT_SECONDS = 15
GATEWAY_UPDATE_MAX_COMPONENTS = 32
GATEWAY_UPDATE_MAX_DEVICE_STATUSES = 32
GATEWAY_UPDATE_MAX_STATUS_TEXT = 160

_ACTIVE_STATUS_WORDS = (
    "download",
    "install",
    "transfer",
    "update",
    "updating",
    "upgrade",
    "in progress",
)
_TERMINAL_STATUS_WORDS = (
    "up to date",
    "complete",
    "completed",
    "done",
    "fail",
    "error",
    "cancel",
    "idle",
    "success",
)
_NON_PROGRESS_TERMINAL_STATUS_WORDS = (
    "up to date",
    "done",
    "fail",
    "error",
    "cancel",
    "idle",
    "success",
)
_DURATION_PART_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)",
    re.IGNORECASE,
)


class GatewaySoftwareUpdateManager:
    """Cache sanitized gateway update progress with stale-on-error behavior."""

    def __init__(
        self,
        client_getter: Callable[[], Any],
        serial_getter: Callable[[], str | None],
        *,
        idle_ttl_seconds: int = GATEWAY_UPDATE_IDLE_TTL_SECONDS,
        active_ttl_seconds: int = GATEWAY_UPDATE_ACTIVE_TTL_SECONDS,
        retry_backoff_seconds: int = GATEWAY_UPDATE_RETRY_BACKOFF_SECONDS,
        fetch_timeout_seconds: int = GATEWAY_UPDATE_FETCH_TIMEOUT_SECONDS,
    ) -> None:
        self._client_getter = client_getter
        self._serial_getter = serial_getter
        self._idle_ttl_seconds = max(60, int(idle_ttl_seconds))
        self._active_ttl_seconds = max(15, int(active_ttl_seconds))
        self._retry_backoff_seconds = max(60, int(retry_backoff_seconds))
        self._fetch_timeout_seconds = max(5, int(fetch_timeout_seconds))

        self._status: dict[str, Any] | None = None
        self._expires_mono = 0.0
        self._last_fetch_utc: datetime | None = None
        self._last_success_utc: datetime | None = None
        self._last_error: str | None = None
        self._using_stale = False
        self._lock = asyncio.Lock()

    @property
    def cached_status(self) -> dict[str, Any] | None:
        """Return the current normalized status."""
        return self._status

    @property
    def next_refresh_seconds(self) -> float:
        """Return the bounded delay until another network refresh is useful."""
        return max(1.0, self._expires_mono - time.monotonic())

    async def async_get_status(
        self, *, force_refresh: bool = False
    ) -> dict[str, Any] | None:
        """Fetch and normalize one live-debug update message."""
        now = time.monotonic()
        if not force_refresh and now < self._expires_mono:
            return self._status

        async with self._lock:
            now = time.monotonic()
            if not force_refresh and now < self._expires_mono:
                return self._status

            self._last_fetch_utc = dt_util.utcnow()
            serial: str | None = None
            try:
                client = self._client_getter()
                serial = _text(self._serial_getter())
                if client is None:
                    raise RuntimeError("client unavailable")
                if serial is None:
                    raise RuntimeError("gateway serial unavailable")
                payload = await asyncio.wait_for(
                    client.site_livestream_payload(
                        serial,
                        live_debug=True,
                        timeout_s=float(self._fetch_timeout_seconds),
                    ),
                    timeout=float(self._fetch_timeout_seconds + 2),
                )
                status = normalize_gateway_software_update(
                    payload,
                    identifiers=(serial,),
                )
                if status is None:
                    raise RuntimeError("software-update status unavailable")
            except Exception as err:  # noqa: BLE001
                self._last_error = redact_text(
                    err,
                    identifiers=(serial,) if serial else (),
                )
                self._using_stale = self._status is not None
                self._expires_mono = time.monotonic() + self._retry_backoff_seconds
                _LOGGER.debug(
                    "Gateway software-update progress refresh failed: %s",
                    self._last_error,
                )
                return self._status

            self._status = status
            self._last_success_utc = dt_util.utcnow()
            self._last_error = None
            self._using_stale = False
            ttl = (
                self._active_ttl_seconds
                if status.get("in_progress") is True
                else self._idle_ttl_seconds
            )
            self._expires_mono = time.monotonic() + ttl
            return self._status

    def status_snapshot(self) -> dict[str, Any]:
        """Return diagnostics-safe cache metadata."""
        return {
            "cache_expires_utc": _mono_to_utc_iso(self._expires_mono),
            "last_fetch_utc": _iso_or_none(self._last_fetch_utc),
            "last_success_utc": _iso_or_none(self._last_success_utc),
            "last_error": self._last_error,
            "using_stale": self._using_stale,
        }


def normalize_gateway_software_update(
    payload: Any,
    *,
    identifiers: tuple[object, ...] = (),
) -> dict[str, Any] | None:
    """Extract sanitized software-update fields from a live-debug message."""
    if not isinstance(payload, Mapping):
        return None

    site = _first_mapping(payload.get("site"))
    if site is None:
        return None
    update_info = _first_mapping(site.get("site_update_info"))
    components, device_statuses, e3_progress = _normalize_device_updates(
        payload.get("devices"), identifiers=identifiers
    )
    if update_info is None and not components and not device_statuses:
        return None

    current_status = _number_or_text(
        update_info.get("Current_Status") if update_info else None,
        identifiers=identifiers,
    )
    current_status_text = _safe_text(
        update_info.get("Current_Status_str") if update_info else None,
        identifiers=identifiers,
    )
    last_status = _number_or_text(
        update_info.get("Last_Status") if update_info else None,
        identifiers=identifiers,
    )
    last_status_text = _safe_text(
        update_info.get("Last_Status_str") if update_info else None,
        identifiers=identifiers,
    )
    estimated = _safe_text(
        update_info.get("Estimated Time Left") if update_info else None,
        identifiers=identifiers,
        max_length=80,
    )
    total = _safe_text(
        update_info.get("Total Duration") if update_info else None,
        identifiers=identifiers,
        max_length=80,
    )
    installed_image_version = _safe_text(
        update_info.get("Current essimg version") if update_info else None,
        identifiers=identifiers,
        max_length=80,
    )
    percentage = _overall_percentage(components, e3_progress)
    in_progress = _update_in_progress(
        current_status=current_status,
        current_status_text=current_status_text,
        device_statuses=device_statuses,
        components=components,
        percentage=percentage,
    )

    return {
        "current_status": current_status,
        "current_status_text": current_status_text,
        "last_status": last_status,
        "last_status_text": last_status_text,
        "estimated_time_left": estimated,
        "estimated_time_left_seconds": duration_seconds(estimated),
        "total_duration": total,
        "total_duration_seconds": duration_seconds(total),
        "installed_image_version": installed_image_version,
        "last_reported_at": _safe_text(
            site.get("timestamp"), identifiers=identifiers, max_length=80
        ),
        "device_statuses": device_statuses,
        "component_updates": components,
        "e3_progress": e3_progress,
        "transfer_speed_bps": _transfer_speed(components),
        "in_progress": in_progress,
        "update_percentage": percentage if in_progress is True else None,
    }


def duration_seconds(value: Any) -> int | None:
    """Convert common live-debug duration strings to whole seconds."""
    text = _text(value)
    if text is None:
        return None
    if text.isdigit():
        return int(text)
    colon_parts = text.split(":")
    if len(colon_parts) in (2, 3) and all(
        part.strip().isdigit() for part in colon_parts
    ):
        numbers = [int(part) for part in colon_parts]
        if len(numbers) == 2:
            return numbers[0] * 60 + numbers[1]
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]

    seconds = 0.0
    matches = list(_DURATION_PART_RE.finditer(text))
    if not matches:
        return None
    for match in matches:
        amount = float(match.group("value"))
        unit = match.group("unit").lower()
        if unit.startswith("d"):
            seconds += amount * 86400
        elif unit.startswith("h"):
            seconds += amount * 3600
        elif unit.startswith("m"):
            seconds += amount * 60
        else:
            seconds += amount
    return round(seconds)


def _first_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, Mapping)), None)
    return None


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _safe_text(
    value: Any,
    *,
    identifiers: tuple[object, ...] = (),
    max_length: int = GATEWAY_UPDATE_MAX_STATUS_TEXT,
) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return redact_text(text, identifiers=identifiers, max_length=max_length) or None


def _number_or_text(
    value: Any,
    *,
    identifiers: tuple[object, ...] = (),
) -> int | float | str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    return _safe_text(value, identifiers=identifiers)


def _normalize_device_updates(
    devices_value: Any,
    *,
    identifiers: tuple[object, ...] = (),
) -> tuple[list[dict[str, Any]], list[str], float | None]:
    components: list[dict[str, Any]] = []
    statuses: list[str] = []
    e3_values: list[float] = []
    for device in _mapping_list(devices_value):
        update_info = device.get("update_info")
        parts = update_info if isinstance(update_info, list) else [update_info]
        for part in parts:
            if isinstance(part, str):
                status = _safe_text(part, identifiers=identifiers)
                if status and len(statuses) < GATEWAY_UPDATE_MAX_DEVICE_STATUSES:
                    statuses.append(status)
                continue
            if isinstance(part, list):
                components.extend(_normalize_components(part, identifiers=identifiers))
                components = components[:GATEWAY_UPDATE_MAX_COMPONENTS]
                continue
            if not isinstance(part, Mapping):
                continue
            progress = _percentage(part.get("e3_progress"))
            if progress is not None:
                e3_values.append(progress)
            if any(key in part for key in ("name", "type", "status_str", "progress")):
                components.extend(
                    _normalize_components([part], identifiers=identifiers)
                )
                components = components[:GATEWAY_UPDATE_MAX_COMPONENTS]
    return components, statuses, max(e3_values) if e3_values else None


def _normalize_components(
    values: list[Any],
    *,
    identifiers: tuple[object, ...] = (),
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        component = {
            "name": _safe_text(value.get("name"), identifiers=identifiers),
            "type": _safe_text(value.get("type"), identifiers=identifiers),
            "status": _number_or_text(value.get("status"), identifiers=identifiers),
            "status_text": _safe_text(value.get("status_str"), identifiers=identifiers),
            "progress": _percentage(value.get("progress")),
            "latest_speed_bps": _non_negative_number(value.get("latest_speed_bps")),
        }
        if any(item is not None for item in component.values()):
            components.append(component)
            if len(components) >= GATEWAY_UPDATE_MAX_COMPONENTS:
                break
    return components


def _non_negative_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return int(number) if number.is_integer() else number


def _percentage(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    number = _non_negative_number(value)
    if number is None:
        return None
    return float(min(100, number))


def _overall_percentage(
    components: list[dict[str, Any]], e3_progress: float | None
) -> float | None:
    if e3_progress is not None:
        return e3_progress
    values: list[float] = [
        float(progress)
        for component in components
        if isinstance((progress := component.get("progress")), (int, float))
    ]
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _transfer_speed(components: list[dict[str, Any]]) -> int | float | None:
    values = [
        component["latest_speed_bps"]
        for component in components
        if isinstance(component.get("latest_speed_bps"), (int, float))
    ]
    return sum(values) if values else None


def _update_in_progress(
    *,
    current_status: int | float | str | None,
    current_status_text: str | None,
    device_statuses: list[str],
    components: list[dict[str, Any]],
    percentage: float | None,
) -> bool | None:
    if percentage is not None and 0 < percentage < 100:
        status_text = current_status_text.lower() if current_status_text else ""
        if any(word in status_text for word in _NON_PROGRESS_TERMINAL_STATUS_WORDS):
            return False
        return True
    current_state = _classify_status_text(current_status_text)
    if current_state is not None:
        return current_state
    other_texts = [*device_statuses]
    other_texts.extend(
        text
        for component in components
        if (text := _text(component.get("status_text"))) is not None
    )
    other_states = [
        state
        for text in other_texts
        if (state := _classify_status_text(text)) is not None
    ]
    if any(other_states):
        return True
    if other_states:
        return False
    if current_status == 0 or percentage == 100:
        return False
    return None


def _classify_status_text(value: str | None) -> bool | None:
    text = value.lower() if value else ""
    if any(word in text for word in _TERMINAL_STATUS_WORDS):
        return False
    if any(word in text for word in _ACTIVE_STATUS_WORDS):
        return True
    return None
