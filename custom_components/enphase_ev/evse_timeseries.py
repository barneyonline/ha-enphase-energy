"""EVSE timeseries helpers for charger energy fallback data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Iterable, cast

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import EVSETimeseriesUnavailable, InvalidPayloadError
from .cloud_retry import retry_after_delay, transient_backoff_delay
from .log_redaction import redact_text

_LOGGER = logging.getLogger(__name__)

EVSE_TIMESERIES_CACHE_TTL = 15 * 60
EVSE_TIMESERIES_FAILURE_BACKOFF_S = 15 * 60
EVSE_TIMESERIES_CACHE_DAYS = 3


@dataclass(slots=True)
class _Acquisition:
    """Share an endpoint attempt among overlapping callers without owning tasks."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    completion: int = 0
    generation: int = -1
    callers: int = 0


class EVSETimeseriesManager:
    """Cache and merge EVSE daily/lifetime timeseries payloads."""

    def __init__(
        self,
        hass: HomeAssistant,
        client_provider: Callable[[], object],
        *,
        logger: logging.Logger | None = None,
        site_id_getter: Callable[[], object] | None = None,
        cache_ttl: float = EVSE_TIMESERIES_CACHE_TTL,
        failure_backoff: float = EVSE_TIMESERIES_FAILURE_BACKOFF_S,
    ) -> None:
        self._hass = hass
        self._client_provider = client_provider
        self._logger = logger or _LOGGER
        self._site_id_getter = site_id_getter
        self._cache_ttl = max(60.0, float(cache_ttl or 0))
        self._failure_backoff = max(60.0, float(failure_backoff or 0))
        self._daily_cache: dict[str, tuple[float, dict[str, dict[str, object]]]] = {}
        self._lifetime_cache: tuple[float, dict[str, dict[str, object]]] | None = None
        self._acquisitions: dict[tuple[str, str], _Acquisition] = {}
        self._generation = 0
        self._stopped = False
        self._active_serials: frozenset[str] | None = None
        self._transient_failures = {"daily": 0, "lifetime": 0}
        self._endpoint_state: dict[str, dict[str, object]] = {
            "daily": {
                "available": True,
                "using_stale": False,
                "failures": 0,
                "last_error": None,
                "last_failure_utc": None,
                "backoff_until": None,
                "backoff_ends_utc": None,
                "last_payload_signature": None,
            },
            "lifetime": {
                "available": True,
                "using_stale": False,
                "failures": 0,
                "last_error": None,
                "last_failure_utc": None,
                "backoff_until": None,
                "backoff_ends_utc": None,
                "last_payload_signature": None,
            },
        }

    def _site_ids(self) -> tuple[object, ...]:
        if self._site_id_getter is None:
            return ()
        try:
            site_id = self._site_id_getter()
        except Exception:  # noqa: BLE001
            return ()
        return (site_id,) if site_id else ()

    def _redact_error(self, err: object) -> str:
        return str(redact_text(err, site_ids=self._site_ids()))

    @property
    def cache_ttl(self) -> float:
        return self._cache_ttl

    @property
    def service_available(self) -> bool:
        return bool(self.daily_available or self.lifetime_available)

    @property
    def service_last_error(self) -> str | None:
        failures = [
            (
                state.get("last_failure_utc"),
                state.get("last_error"),
            )
            for state in self._endpoint_state.values()
            if state.get("last_error") is not None
        ]
        if not failures:
            return None
        failures.sort(
            key=lambda item: (
                item[0].timestamp() if isinstance(item[0], datetime) else float("-inf")
            )
        )
        latest_error = failures[-1][1]
        return latest_error if isinstance(latest_error, str) else None

    @property
    def service_failures(self) -> int:
        return sum(
            int(str(state.get("failures", 0) or 0))
            for state in self._endpoint_state.values()
        )

    @property
    def service_backoff_active(self) -> bool:
        return bool(self.daily_backoff_active or self.lifetime_backoff_active)

    @property
    def service_backoff_ends_utc(self) -> datetime | None:
        ends: list[datetime] = []
        for state in self._endpoint_state.values():
            value = state.get("backoff_ends_utc")
            if isinstance(value, datetime):
                ends.append(value)
        if not ends:
            return None
        return max(ends)

    @property
    def service_last_failure_utc(self) -> datetime | None:
        failures: list[datetime] = []
        for state in self._endpoint_state.values():
            value = state.get("last_failure_utc")
            if isinstance(value, datetime):
                failures.append(value)
        if not failures:
            return None
        return max(failures)

    @property
    def daily_available(self) -> bool:
        return self._endpoint_available("daily")

    @property
    def lifetime_available(self) -> bool:
        return self._endpoint_available("lifetime")

    @property
    def daily_backoff_active(self) -> bool:
        return self._endpoint_backoff_active("daily")

    @property
    def lifetime_backoff_active(self) -> bool:
        return self._endpoint_backoff_active("lifetime")

    @property
    def daily_cache_age(self) -> dict[str, float]:
        now = time.monotonic()
        ages: dict[str, float] = {}
        for day_key, (cached_at, _data) in self._daily_cache.items():
            try:
                age = now - cached_at
            except Exception:
                continue
            if age >= 0:
                ages[day_key] = round(age, 3)
        return ages

    @property
    def lifetime_cache_age(self) -> float | None:
        cached = self._lifetime_cache
        if cached is None:
            return None
        try:
            age = time.monotonic() - cached[0]
        except Exception:
            return None
        return round(age, 3) if age >= 0 else None

    @property
    def daily_cache_days(self) -> list[str]:
        return sorted(self._daily_cache.keys())

    @property
    def lifetime_serial_count(self) -> int:
        if self._lifetime_cache is None:
            return 0
        return len(self._lifetime_cache[1])

    def _endpoint_state_for(self, endpoint: str) -> dict[str, object]:
        return self._endpoint_state[endpoint]

    def _endpoint_available(self, endpoint: str) -> bool:
        state = self._endpoint_state_for(endpoint)
        return bool(
            state.get("available", True) and not self._endpoint_backoff_active(endpoint)
        )

    def _endpoint_backoff_active(self, endpoint: str) -> bool:
        state = self._endpoint_state_for(endpoint)
        backoff_until = state.get("backoff_until")
        return bool(backoff_until and time.monotonic() < float(str(backoff_until)))

    def _mark_endpoint_available(self, endpoint: str) -> None:
        self._transient_failures[endpoint] = 0
        state = self._endpoint_state_for(endpoint)
        state["available"] = True
        state["using_stale"] = False
        state["failures"] = 0
        state["last_error"] = None
        state["last_failure_utc"] = None
        state["backoff_until"] = None
        state["backoff_ends_utc"] = None
        state["last_payload_signature"] = None

    def _note_service_unavailable(
        self,
        endpoint: str,
        err: Exception | str | None,
        *,
        using_stale: bool = False,
        transient: bool = False,
    ) -> None:
        state = self._endpoint_state_for(endpoint)
        reason = self._redact_error(err) if err else "EVSE timeseries unavailable"
        state["available"] = False
        state["using_stale"] = using_stale
        state["failures"] = int(str(state.get("failures", 0) or 0)) + 1
        state["last_error"] = reason
        state["last_failure_utc"] = dt_util.utcnow()
        state["last_payload_signature"] = (
            err.signature_dict() if isinstance(err, InvalidPayloadError) else None
        )
        delay = max(self._failure_backoff, 60.0)
        if transient:
            self._transient_failures[endpoint] += 1
            delay = transient_backoff_delay(self._transient_failures[endpoint])
        if isinstance(err, BaseException):
            delay = max(delay, retry_after_delay(err) or 0.0)
        delay = max(
            delay, float(str(state.get("backoff_until") or 0.0)) - time.monotonic()
        )
        state["backoff_until"] = time.monotonic() + delay
        try:
            state["backoff_ends_utc"] = dt_util.utcnow() + timedelta(seconds=delay)
        except Exception:
            state["backoff_ends_utc"] = None

    @staticmethod
    def _day_key(day_local: datetime) -> str:
        return day_local.strftime("%Y-%m-%d")

    def _lifetime_cache_fresh(self) -> bool:
        cached = self._lifetime_cache
        if cached is None:
            return False
        try:
            return (time.monotonic() - cached[0]) < self._cache_ttl
        except Exception:
            return False

    def _daily_cache_fresh(self, day_key: str) -> bool:
        cached = self._daily_cache.get(day_key)
        if cached is None:
            return False
        try:
            return (time.monotonic() - cached[0]) < self._cache_ttl
        except Exception:
            return False

    def _endpoint_due(self, endpoint: str, day_key: str, *, force: bool) -> bool:
        fresh = (
            self._lifetime_cache_fresh()
            if endpoint == "lifetime"
            else self._daily_cache_fresh(day_key)
        )
        return (
            not self._stopped
            and not self._endpoint_backoff_active(endpoint)
            and (force or not fresh)
        )

    def refresh_due(
        self,
        *,
        day_local: datetime | None = None,
        force: bool = False,
    ) -> bool:
        """Return whether a supported family is due, respecting provider cooldowns."""
        if day_local is None:
            day_local = dt_util.as_local(dt_util.now())
        day_key = self._day_key(day_local)
        client = self._client_provider()
        return any(
            self._endpoint_due(endpoint, day_key, force=force)
            and callable(getattr(client, f"evse_timeseries_{endpoint}_energy", None))
            for endpoint in ("lifetime", "daily")
        )

    async def async_refresh(
        self,
        *,
        day_local: datetime | None = None,
        force: bool = False,
    ) -> None:
        if day_local is None:
            day_local = dt_util.as_local(dt_util.now())
        day_key = self._day_key(day_local)
        calls: list[tuple[str, tuple[str, str], _Acquisition, int]] = []
        # Register both families before the first await so callers queued on
        # lifetime also share the overlapping daily acquisition.
        for endpoint in ("lifetime", "daily"):
            if not self._endpoint_due(endpoint, day_key, force=force):
                continue
            key = (endpoint, day_key if endpoint == "daily" else "")
            acquisition = self._acquisitions.get(key)
            if acquisition is None:
                acquisition = self._acquisitions[key] = _Acquisition()
            acquisition.callers += 1
            calls.append((endpoint, key, acquisition, acquisition.completion))
        try:
            for endpoint, _key, acquisition, completion in calls:
                async with acquisition.lock:
                    if (
                        completion != acquisition.completion
                        and acquisition.generation == self._generation
                    ):
                        continue
                    if not self._endpoint_due(endpoint, day_key, force=force):
                        continue
                    await self._async_acquire(endpoint, day_local, acquisition)
        finally:
            for _endpoint, key, acquisition, _completion in calls:
                acquisition.callers -= 1
                if not acquisition.callers:
                    self._acquisitions.pop(key)

    async def _async_acquire(
        self, endpoint: str, day_local: datetime, acquisition: _Acquisition
    ) -> None:
        fetcher = getattr(
            self._client_provider(), f"evse_timeseries_{endpoint}_energy", None
        )
        if not callable(fetcher):
            return
        generation = self._generation
        day_key = self._day_key(day_local)
        using_stale = (
            self._lifetime_cache is not None
            if endpoint == "lifetime"
            else day_key in self._daily_cache
        )
        try:
            payload = await fetcher(
                **({"start_date": day_local} if endpoint == "daily" else {})
            )
        except Exception as err:  # noqa: BLE001
            if generation != self._generation:
                return
            acquisition.completion += 1
            acquisition.generation = generation
            self._note_service_unavailable(
                endpoint,
                err,
                using_stale=using_stale,
                transient=not isinstance(
                    err,
                    (
                        EVSETimeseriesUnavailable,
                        InvalidPayloadError,
                        aiohttp.ClientResponseError,
                    ),
                ),
            )
        else:
            if generation != self._generation:
                return
            acquisition.completion += 1
            acquisition.generation = generation
            if isinstance(payload, dict):
                data = self._filter_serials(cast(dict[str, dict[str, object]], payload))
                if endpoint == "lifetime":
                    self._lifetime_cache = (time.monotonic(), data)
                else:
                    self._daily_cache[day_key] = (time.monotonic(), data)
                    self._prune_days()
                self._mark_endpoint_available(endpoint)

    def _filter_serials(
        self, payload: dict[str, dict[str, object]]
    ) -> dict[str, dict[str, object]]:
        if self._active_serials is None:
            return payload
        return {
            serial: value
            for serial, value in payload.items()
            if serial in self._active_serials
        }

    def _prune_days(self) -> None:
        for day_key in sorted(self._daily_cache, reverse=True)[
            EVSE_TIMESERIES_CACHE_DAYS:
        ]:
            del self._daily_cache[day_key]

    def prune(self, *, active_serials: Iterable[str]) -> None:
        """Bound retained dates and drop records for authoritatively retired chargers."""
        active = frozenset(active_serials)
        if active == self._active_serials:
            return
        if self._active_serials is not None and active != self._active_serials:
            self._generation += 1
        self._active_serials = active
        self._daily_cache = {
            day: (stamp, self._filter_serials(data))
            for day, (stamp, data) in self._daily_cache.items()
        }
        if self._lifetime_cache is not None:
            stamp, data = self._lifetime_cache
            self._lifetime_cache = (stamp, self._filter_serials(data))
        self._prune_days()

    def clear(self) -> None:
        """Invalidate outstanding reads before dropping their cached payloads."""
        self._generation += 1
        self._daily_cache.clear()
        self._lifetime_cache = None

    def stop(self) -> None:
        """Retire the manager without creating or retaining background tasks."""
        self._stopped = True
        self.clear()

    def daily_entry(
        self,
        serial: str,
        *,
        day_local: datetime,
    ) -> dict[str, object] | None:
        cached = self._daily_cache.get(self._day_key(day_local))
        if cached is None:
            return None
        return cached[1].get(serial)

    def lifetime_entry(self, serial: str) -> dict[str, object] | None:
        cached = self._lifetime_cache
        if cached is None:
            return None
        return cached[1].get(serial)

    def merge_charger_payloads(
        self,
        payloads: dict[str, dict[str, object]],
        *,
        day_local: datetime,
    ) -> None:
        for serial, entry in payloads.items():
            daily = self.daily_entry(serial, day_local=day_local)
            lifetime = self.lifetime_entry(serial)
            if isinstance(daily, dict):
                value = daily.get("energy_kwh")
                if value is not None:
                    entry["evse_daily_energy_kwh"] = value
                interval = daily.get("interval_minutes")
                if interval is not None:
                    entry["evse_timeseries_interval_minutes"] = interval
            if isinstance(lifetime, dict):
                value = lifetime.get("energy_kwh")
                if value is not None:
                    entry["evse_lifetime_energy_kwh"] = value
                interval = lifetime.get("interval_minutes")
                if (
                    interval is not None
                    and entry.get("evse_timeseries_interval_minutes") is None
                ):
                    entry["evse_timeseries_interval_minutes"] = interval
            last_report = None
            if isinstance(lifetime, dict):
                last_report = lifetime.get("last_report_date")
            if last_report is None and isinstance(daily, dict):
                last_report = daily.get("last_report_date")
            if last_report is not None:
                entry["evse_timeseries_last_reported_at"] = last_report
            if isinstance(daily, dict) or isinstance(lifetime, dict):
                entry["evse_timeseries_source"] = "evse_timeseries"

    def diagnostics(self) -> dict[str, object]:
        endpoint_details: dict[str, dict[str, object]] = {}
        for key, state in self._endpoint_state.items():
            last_failure = state.get("last_failure_utc")
            backoff_ends = state.get("backoff_ends_utc")
            endpoint_details[key] = {
                "available": bool(state.get("available", True)),
                "using_stale": bool(state.get("using_stale", False)),
                "failures": int(str(state.get("failures", 0) or 0)),
                "last_error": state.get("last_error"),
                "last_failure_utc": (
                    last_failure.isoformat()
                    if isinstance(last_failure, datetime)
                    else None
                ),
                "backoff_until": state.get("backoff_until"),
                "backoff_ends_utc": (
                    backoff_ends.isoformat()
                    if isinstance(backoff_ends, datetime)
                    else None
                ),
                "last_payload_signature": state.get("last_payload_signature"),
            }
        return {
            "available": self.service_available,
            "using_stale": bool(
                any(
                    bool(state.get("using_stale"))
                    for state in self._endpoint_state.values()
                )
            ),
            "failures": self.service_failures,
            "last_error": self.service_last_error,
            "last_failure_utc": (
                self.service_last_failure_utc.isoformat()
                if self.service_last_failure_utc is not None
                else None
            ),
            "backoff_active": self.service_backoff_active,
            "backoff_ends_utc": (
                self.service_backoff_ends_utc.isoformat()
                if self.service_backoff_ends_utc is not None
                else None
            ),
            "cache_ttl_seconds": self.cache_ttl,
            "daily_cache_days": self.daily_cache_days,
            "daily_cache_age_seconds": self.daily_cache_age,
            "lifetime_cache_age_seconds": self.lifetime_cache_age,
            "lifetime_serial_count": self.lifetime_serial_count,
            "daily": endpoint_details["daily"],
            "lifetime": endpoint_details["lifetime"],
        }
