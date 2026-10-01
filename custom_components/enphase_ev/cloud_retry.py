"""Shared Retry-After parsing for independent cloud endpoint caches."""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime

import aiohttp
from homeassistant.util import dt as dt_util


def retry_after_seconds(
    value: str | None, *, now: datetime | None = None
) -> float | None:
    """Return a nonnegative HTTP retry delay, or None for an invalid header."""

    if not value:
        return None
    try:
        return max(0.0, float(int(value)))
    except (ValueError, OverflowError):
        pass
    try:
        deadline = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=dt_util.UTC)
    current = now if now is not None else dt_util.utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt_util.UTC)
    return max(0.0, (deadline - current).total_seconds())


def retry_after_delay(
    error: BaseException, *, now: datetime | None = None
) -> float | None:
    """Read an HTTP retry deadline, including explicitly wrapped cloud errors."""

    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        if isinstance(current, aiohttp.ClientResponseError) and current.headers:
            return retry_after_seconds(
                current.headers.get("Retry-After")
                or current.headers.get("retry-after"),
                now=now,
            )
        current = current.__cause__
    return None
