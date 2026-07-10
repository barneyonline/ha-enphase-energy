"""Helpers for working with Enphase Energy summary responses."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import aiohttp
from homeassistant.util import dt as dt_util

from .api import InvalidPayloadError
from .log_redaction import redact_text

_LOGGER = logging.getLogger(__name__)

SUMMARY_IDLE_TTL = 600.0
SUMMARY_ACTIVE_MIN_TTL = 5.0
SUMMARY_FAILURE_BACKOFF_S = 60.0
SUMMARY_FAILURE_MAX_BACKOFF_S = 900.0


class SummaryStore:
    """Cache and manage summary_v2 responses."""

    def __init__(
        self,
        client_getter: Callable[[], Any],
        *,
        site_id_getter: Callable[[], object] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client_getter = client_getter
        self._site_id_getter = site_id_getter
        self._logger = logger or _LOGGER
        self._cache: tuple[float, list[dict[str, Any]], float] | None = None
        self._ttl: float = SUMMARY_IDLE_TTL
        self._lock = asyncio.Lock()
        self._state: dict[str, object] = {
            "available": True,
            "using_stale": False,
            "failures": 0,
            "last_success_utc": None,
            "last_success_mono": None,
            "last_failure_utc": None,
            "last_error": None,
            "backoff_until": None,
            "backoff_ends_utc": None,
            "last_payload_signature": None,
        }

    def _site_ids(self, client: Any | None = None) -> tuple[object, ...]:
        site_ids: list[object] = []
        if self._site_id_getter is not None:
            try:
                site_ids.append(self._site_id_getter())
            except Exception:  # noqa: BLE001
                pass
        if client is not None:
            site_ids.append(getattr(client, "_site", None))
        return tuple(site_id for site_id in site_ids if site_id)

    def _redact_error(self, err: object, client: Any | None = None) -> str:
        return str(redact_text(err, site_ids=self._site_ids(client)))

    @property
    def ttl(self) -> float:
        """Return the current cache TTL in seconds."""
        return self._ttl

    def invalidate(self) -> None:
        """Drop the cached payload."""
        self._cache = None

    def _backoff_active(self) -> bool:
        backoff_until = self._state.get("backoff_until")
        return bool(backoff_until and time.monotonic() < float(str(backoff_until)))

    def _failure_backoff_delay(self, err: Exception, failures: int) -> float:
        retry_delay = 0.0
        if isinstance(err, aiohttp.ClientResponseError) and err.headers:
            retry_after = err.headers.get("Retry-After")
            if retry_after:
                try:
                    retry_delay = max(0.0, float(int(retry_after)))
                except Exception:
                    retry_dt = None
                    try:
                        retry_dt = parsedate_to_datetime(str(retry_after))
                    except Exception:
                        retry_dt = None
                    if retry_dt is not None:
                        if retry_dt.tzinfo is None:
                            retry_dt = retry_dt.replace(tzinfo=dt_util.UTC)
                        retry_delay = max(
                            0.0,
                            (
                                retry_dt.astimezone(dt_util.UTC) - dt_util.utcnow()
                            ).total_seconds(),
                        )
        multiplier = 2 ** min(max(failures - 1, 0), 3)
        base_delay = max(self._ttl, SUMMARY_FAILURE_BACKOFF_S)
        return float(
            max(
                retry_delay,
                min(SUMMARY_FAILURE_MAX_BACKOFF_S, base_delay * multiplier),
            )
        )

    def _get_cache(
        self,
    ) -> tuple[float, list[dict[str, Any]], float] | None:
        cache = self._cache
        if not cache:
            return None
        if isinstance(cache, tuple) and len(cache) == 3:
            ts, data, ttl = cache
            return ts, data, float(ttl)
        if isinstance(cache, tuple) and len(cache) == 2:
            ts, data = cache
            return ts, data, SUMMARY_IDLE_TTL
        return None

    def prepare_refresh(
        self, *, want_fast: bool, target_interval: float | None
    ) -> bool:
        """Update the cache TTL target and return if a refresh is required."""
        summary_ttl = SUMMARY_IDLE_TTL
        if want_fast and target_interval:
            summary_ttl = max(
                SUMMARY_ACTIVE_MIN_TTL,
                min(target_interval, SUMMARY_IDLE_TTL),
            )
        cache_info = self._get_cache()
        force = False
        if cache_info is None:
            force = True
        else:
            cache_ts, cache_data, cache_ttl = cache_info
            age = time.monotonic() - cache_ts
            if cache_ttl > summary_ttl or age >= summary_ttl:
                force = True
            elif cache_ttl != summary_ttl:
                self._cache = (cache_ts, cache_data, summary_ttl)
        self._ttl = summary_ttl
        return force

    async def async_fetch(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Return the cached summary, optionally forcing a refresh."""
        cache = self._get_cache()
        if not force and cache:
            cache_ts, cache_data, cache_ttl = cache
            if time.monotonic() - cache_ts < cache_ttl:
                return cache_data

        async with self._lock:
            cached = self._get_cache()
            if not force and cached:
                cache_ts, cache_data, cache_ttl = cached
                if time.monotonic() - cache_ts < cache_ttl:
                    return cache_data
            if self._backoff_active():
                self._state["using_stale"] = bool(cached)
                return cached[1] if cached else []
            client = self._client_getter()
            if client is None:
                self._logger.debug("Summary v2 fetch skipped; client unavailable")
                return cached[1] if cached else []
            try:
                summary = await client.summary_v2()
            except Exception as err:  # noqa: BLE001
                failures = int(str(self._state.get("failures", 0) or 0)) + 1
                delay = self._failure_backoff_delay(err, failures)
                self._state["available"] = False
                self._state["last_failure_utc"] = dt_util.utcnow()
                self._state["last_error"] = self._redact_error(err, client)
                self._state["failures"] = failures
                self._state["backoff_until"] = time.monotonic() + delay
                try:
                    self._state["backoff_ends_utc"] = dt_util.utcnow() + timedelta(
                        seconds=delay
                    )
                except Exception:
                    self._state["backoff_ends_utc"] = None
                self._state["last_payload_signature"] = (
                    err.signature_dict()
                    if isinstance(err, InvalidPayloadError)
                    else None
                )
                if cached:
                    self._state["using_stale"] = True
                    self._logger.debug(
                        "Summary v2 fetch failed; reusing cache: %s",
                        self._redact_error(err, client),
                    )
                    return cached[1]
                self._state["using_stale"] = False
                self._logger.debug(
                    "Summary v2 fetch failed: %s",
                    self._redact_error(err, client),
                )
                return []

            summary_list = self._as_list(summary)
            self._cache = (time.monotonic(), summary_list, self._ttl)
            self._state["available"] = True
            self._state["using_stale"] = False
            self._state["failures"] = 0
            self._state["last_error"] = None
            self._state["last_failure_utc"] = None
            self._state["backoff_until"] = None
            self._state["backoff_ends_utc"] = None
            self._state["last_payload_signature"] = None
            self._state["last_success_utc"] = dt_util.utcnow()
            self._state["last_success_mono"] = time.monotonic()
            return summary_list

    def _as_list(self, summary: Any) -> list[dict[str, Any]]:
        """Normalize the raw summary payload into a list."""
        if not summary:
            return []
        if isinstance(summary, list):
            return summary
        if isinstance(summary, dict):
            interim = summary.get("data")
            return interim if isinstance(interim, list) else []
        if isinstance(summary, (tuple, set)):
            return list(summary)
        return []

    def diagnostics(self) -> dict[str, object]:
        """Return payload-health diagnostics for summary_v2."""

        last_success_age = None
        last_success_mono = self._state.get("last_success_mono")
        if isinstance(last_success_mono, (int, float)):
            age = time.monotonic() - float(last_success_mono)
            if age >= 0:
                last_success_age = round(age, 3)
        last_failure_utc = self._state.get("last_failure_utc")
        last_success_utc = self._state.get("last_success_utc")
        backoff_ends_utc = self._state.get("backoff_ends_utc")
        return {
            "available": bool(self._state.get("available", True)),
            "using_stale": bool(self._state.get("using_stale", False)),
            "failures": int(str(self._state.get("failures", 0) or 0)),
            "last_error": self._state.get("last_error"),
            "backoff_active": self._backoff_active(),
            "backoff_until": self._state.get("backoff_until"),
            "backoff_ends_utc": (
                backoff_ends_utc.isoformat()
                if isinstance(backoff_ends_utc, datetime)
                else None
            ),
            "last_failure_utc": (
                last_failure_utc.isoformat()
                if isinstance(last_failure_utc, datetime)
                else None
            ),
            "last_success_utc": (
                last_success_utc.isoformat()
                if isinstance(last_success_utc, datetime)
                else None
            ),
            "last_success_age_s": last_success_age,
            "last_payload_signature": self._state.get("last_payload_signature"),
        }
