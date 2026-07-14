"""Shared normalization helpers for decomposed sensor feature modules."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

from homeassistant.util import dt as dt_util


def parse_gateway_timestamp(value: object) -> datetime | None:
    """Normalize a cloud timestamp to a timezone-aware UTC datetime."""

    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    timestamp_seconds: float | None = None
    if isinstance(value, (int, float)):
        try:
            timestamp_seconds = float(value)
        except Exception:  # noqa: BLE001
            timestamp_seconds = None
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            timestamp_seconds = float(cleaned)
        except Exception:
            timestamp_seconds = None
        if timestamp_seconds is None:
            normalized = cleaned.replace("[UTC]", "").replace("Z", "+00:00")
            parsed = dt_util.parse_datetime(normalized)
            if parsed is None:
                try:
                    parsed = datetime.fromisoformat(normalized)
                except Exception:  # noqa: BLE001
                    return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return cast(datetime, parsed.astimezone(timezone.utc))
    if timestamp_seconds is None:
        return None
    if timestamp_seconds > 1_000_000_000_000:
        timestamp_seconds /= 1000.0
    try:
        return datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


# Compatibility name retained for existing sensor tests and imports.
_gateway_parse_timestamp = parse_gateway_timestamp
