"""Shared observed cloud error categories for entities and Activity."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

STATE_NONE = "none"


def _auth_block_is_active(coord: EnphaseCoordinator) -> bool:
    """Read authentication-block evidence without mutating coordinator state."""
    if getattr(coord, "_last_error", None) == "auth_blocked":
        return True
    blocked_until = getattr(coord, "_auth_blocked_until_utc", None)
    if isinstance(blocked_until, datetime):
        return bool(blocked_until > dt_util.utcnow())
    return False


def cloud_error_code(coord: EnphaseCoordinator) -> str:
    """Classify the current observed failure, matching Cloud Error Code."""
    failure_ts = getattr(coord, "last_failure_utc", None)
    success_ts = getattr(coord, "last_success_utc", None)
    failure_active = bool(
        failure_ts and (success_ts is None or failure_ts > success_ts)
    )
    if not failure_active:
        if not getattr(coord, "evse_status_available", True):
            return "service_unavailable"
        return STATE_NONE
    failure_source = getattr(coord, "last_failure_source", None)
    if (
        failure_source == "payload"
        or getattr(coord, "payload_failure_kind", None) is not None
    ):
        return "invalid_payload"
    if failure_source == "auth" and _auth_block_is_active(coord):
        return "auth_blocked"
    code = getattr(coord, "last_failure_status", None)
    if code is None:
        if failure_source == "auth":
            return "authentication_error"
        description = (getattr(coord, "last_failure_description", None) or "").lower()
        if failure_source == "network":
            dns_tokens = (
                "dns",
                "name or service not known",
                "temporary failure in name resolution",
                "resolv",
            )
            if any(token in description for token in dns_tokens):
                return "dns_error"
            return "network_error"
        return STATE_NONE
    try:
        status = int(code)
    except (TypeError, ValueError):
        return "request_error"
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "authentication_error"
    if 500 <= status < 600:
        return "service_unavailable"
    return "request_error"
