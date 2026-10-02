"""Isolate EVSE server failures and retain bounded, credential-free evidence."""

from __future__ import annotations

import logging
import asyncio
from collections.abc import Callable
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, TypeVar, cast

import aiohttp
from homeassistant.core import HomeAssistant, callback as ha_callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .cloud_retry import retry_after_delay
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])
callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)
_HISTORY_LIMIT = 16
_ERROR_CODES = frozenset(
    {"INTERNAL_SERVER_ERROR", "SERVICE_UNAVAILABLE", "GATEWAY_TIMEOUT", "BAD_GATEWAY"}
)
_REQUEST_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return parsed.isoformat() if parsed is not None and parsed.tzinfo else None


def _safe_record(value: object) -> dict[str, object] | None:
    """Allow only known fields and constrained values, including on restore."""
    if not isinstance(value, dict):
        return None
    stamp = _timestamp(value.get("failed_at"))
    status = value.get("http_status")
    if stamp is None or type(status) is not int or not 500 <= status < 600:
        return None
    code = value.get("error_code")
    request_id = value.get("request_id")
    return {
        "endpoint": "charger_status",
        "failed_at": stamp,
        "http_status": status,
        "next_retry_utc": _timestamp(value.get("next_retry_utc")),
        "error_code": code if isinstance(code, str) and code in _ERROR_CODES else None,
        "request_id": (
            request_id
            if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)
            else None
        ),
    }


def _store(hass: HomeAssistant, identity: str) -> Store[dict[str, Any]]:
    return Store(hass, 1, f"{DOMAIN}.evse_status_health.{identity}")


async def async_load_status_history(
    hass: HomeAssistant, identity: str
) -> dict[str, Any]:
    """Read safe incident evidence even when config-entry setup is retrying."""
    try:
        saved = await _store(hass, identity).async_load()
    except Exception:
        _LOGGER.warning("Unable to restore charger-status incident history")
        return {}
    if not isinstance(saved, dict):
        return {}
    values = saved.get("failures")
    records = values[-_HISTORY_LIMIT:] if isinstance(values, list) else []
    return {
        "last_success_utc": _timestamp(saved.get("last_success_utc")),
        "next_retry_utc": _timestamp(saved.get("next_retry_utc")),
        "failures": [record for value in records if (record := _safe_record(value))],
    }


class EvseStatusHealth:
    """Own only charger-status cooldown; sibling refreshes remain independent."""

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        entry = getattr(coordinator, "config_entry", None)
        self.identity = getattr(entry, "entry_id", coordinator.site_id)
        self.available = True
        self.last_success_utc: str | None = None
        self.next_retry_utc: datetime | None = None
        self._next_retry_mono: float | None = None
        self._failures: list[dict[str, object]] = []
        self._consecutive_failures = 0
        self._cancel: Callable[[], None] | None = None

    @property
    def cooldown_active(self) -> bool:
        return (
            self._next_retry_mono is not None
            and time.monotonic() < self._next_retry_mono
        )

    def diagnostics(self) -> dict[str, object]:
        return {
            "endpoint": "charger_status",
            "available": self.available,
            "last_success_utc": self.last_success_utc,
            "next_retry_utc": (
                self.next_retry_utc.isoformat()
                if self.cooldown_active and self.next_retry_utc
                else None
            ),
            "http_status": (
                self._failures[-1]["http_status"]
                if self._failures and not self.available
                else None
            ),
            "failures": [dict(record) for record in self._failures],
        }

    async def async_restore(self) -> None:
        saved = await async_load_status_history(self.coordinator.hass, self.identity)
        if not self.coordinator.runtime_active:
            return
        self.last_success_utc = saved.get("last_success_utc")
        self._failures = saved.get("failures", [])
        if not self.coordinator._evse_status_refresh_enabled():
            self.available = True
            self.stop()
            self.coordinator.diagnostics.clear_evse_status_issue()
            return
        value = saved.get("next_retry_utc")
        if value is None or not self._failures:
            return
        deadline = dt_util.parse_datetime(value)
        assert deadline is not None
        self.available = False
        remaining = (deadline - dt_util.utcnow()).total_seconds()
        if remaining > 0:
            self._schedule(remaining)

    async def async_failure(self, error: aiohttp.ClientResponseError) -> None:
        if not self.coordinator.runtime_active:
            raise asyncio.CancelledError
        self.available = False
        self._consecutive_failures += 1
        # Server errors differ from account-wide 429 limits. Bound locally chosen
        # recovery delays to ten minutes, while respecting longer provider waits.
        delay = min(
            600.0,
            60.0
            * 2 ** min(self._consecutive_failures - 1, 4)
            * random.uniform(1.0, 1.25),
        )
        delay = max(delay, retry_after_delay(error) or 0.0)
        self._schedule(delay)
        assert self.next_retry_utc is not None
        record = _safe_record(
            {
                "failed_at": dt_util.utcnow().isoformat(),
                "http_status": error.status,
                "next_retry_utc": self.next_retry_utc.isoformat(),
                "error_code": getattr(error, "enphase_error_status", None),
                "request_id": (
                    error.headers.get("X-Request-ID") if error.headers else None
                ),
            }
        )
        assert record is not None
        self._failures = [*self._failures, record][-_HISTORY_LIMIT:]
        await self._async_save()

    async def async_success(self) -> None:
        if not self.coordinator.runtime_active:
            raise asyncio.CancelledError
        recovered = not self.available
        self.last_success_utc = dt_util.utcnow().isoformat()
        self._consecutive_failures = 0
        self.stop()
        if recovered:
            await self._async_save()
        self.available = True

    async def _async_save(self) -> None:
        try:
            await _store(self.coordinator.hass, self.identity).async_save(
                {
                    "last_success_utc": self.last_success_utc,
                    "next_retry_utc": (
                        self.next_retry_utc.isoformat() if self.next_retry_utc else None
                    ),
                    "failures": self._failures,
                }
            )
        except Exception:
            _LOGGER.warning("Unable to save charger-status incident history")
        if not self.coordinator.runtime_active:
            raise asyncio.CancelledError

    def _schedule(self, delay: float) -> None:
        self.stop()
        self.next_retry_utc = dt_util.utcnow() + timedelta(seconds=delay)
        self._next_retry_mono = time.monotonic() + delay

        @callback
        def retry(_now: datetime) -> None:
            self._cancel = None
            self._next_retry_mono = None
            self.next_retry_utc = None
            if self.coordinator.runtime_active:
                self.coordinator._start_backoff_refresh()

        self._cancel = async_call_later(self.coordinator.hass, delay, retry)

    def stop(self) -> None:
        """Retire the timer without changing persisted incident evidence."""
        if self._cancel is not None:
            self._cancel()
            self._cancel = None
        self.next_retry_utc = None
        self._next_retry_mono = None
