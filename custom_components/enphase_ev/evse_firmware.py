from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
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

EVSE_FIRMWARE_CACHE_TTL_SECONDS = 60 * 60
EVSE_FIRMWARE_RETRY_BACKOFF_SECONDS = 15 * 60


class EvseFirmwareDetailsManager:
    """Cache EVSE firmware details with stale-on-error behavior."""

    def __init__(
        self,
        client_getter: Callable[[], Any],
        *,
        ttl_seconds: int = EVSE_FIRMWARE_CACHE_TTL_SECONDS,
        retry_backoff_seconds: int = EVSE_FIRMWARE_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self._client_getter = client_getter
        self._ttl_seconds = max(300, int(ttl_seconds))
        self._retry_backoff_seconds = max(60, int(retry_backoff_seconds))

        self._details: dict[str, dict[str, Any]] | None = None
        self._expires_mono: float = 0.0
        self._last_fetch_utc: datetime | None = None
        self._last_success_utc: datetime | None = None
        self._last_error: str | None = None
        self._using_stale = False
        self._lock = asyncio.Lock()

    @property
    def cached_details(self) -> dict[str, dict[str, Any]] | None:
        return self._details

    async def async_get_details(
        self, *, force_refresh: bool = False
    ) -> dict[str, dict[str, Any]] | None:
        now = time.monotonic()
        if not force_refresh and now < self._expires_mono:
            return self._details

        async with self._lock:
            now = time.monotonic()
            if not force_refresh and now < self._expires_mono:
                return self._details

            self._last_fetch_utc = dt_util.utcnow()
            client = self._client_getter()
            try:
                if client is None:
                    raise RuntimeError("client unavailable")
                payload = await client.evse_fw_details()
                if payload is None:
                    raise RuntimeError("fwDetails endpoint unavailable")
                details = _normalize_details(payload)
            except Exception as err:  # noqa: BLE001
                self._last_error = redact_text(err)
                self._using_stale = self._details is not None
                self._expires_mono = time.monotonic() + self._retry_backoff_seconds
                _LOGGER.debug(
                    "EVSE firmware details refresh failed: %s",
                    self._last_error,
                )
                return self._details

            self._details = details
            self._last_success_utc = dt_util.utcnow()
            self._last_error = None
            self._using_stale = False
            self._expires_mono = time.monotonic() + self._ttl_seconds
            return self._details

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "cache_ttl_seconds": self._ttl_seconds,
            "cache_expires_utc": _mono_to_utc_iso(self._expires_mono),
            "last_fetch_utc": _iso_or_none(self._last_fetch_utc),
            "last_success_utc": _iso_or_none(self._last_success_utc),
            "last_error": self._last_error,
            "using_stale": self._using_stale,
        }


def _normalize_details(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("fwDetails payload must be a list")

    details: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        serial = _text(item.get("serialNumber"))
        if not serial:
            continue
        details[serial] = dict(item)
    return details
