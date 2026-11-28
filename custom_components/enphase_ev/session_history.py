"""Session history helpers for the Enphase Energy coordinator."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as _tz
from typing import Any, Awaitable, Callable, Iterable, Iterator, Protocol, cast

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import (
    InvalidPayloadError,
    SessionHistoryUnavailable,
    Unauthorized,
    enlighten_optional_read_scope,
    guarded_read_scope,
)
from .api_client.errors import EnlightenReadSuperseded
from .cloud_retry import retry_after_delay
from .log_redaction import redact_identifier, redact_text
from .request_metrics import request_metrics_scope
from .session_history_pages import SessionHistoryPages, parse_session_timestamp

_LOGGER = logging.getLogger(__name__)

MIN_SESSION_HISTORY_CACHE_TTL = 60  # seconds
SESSION_HISTORY_FAILURE_BACKOFF_S = 15 * 60
SESSION_HISTORY_CONCURRENCY = 3
SESSION_HISTORY_CACHE_DAY_RETENTION = 3


class SessionFetchCallback(Protocol):
    async def __call__(
        self, serial: str, *, day_local: datetime | None = None
    ) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class SessionCacheView:
    """Represents the current cache state for a serial."""

    sessions: list[dict[str, Any]]
    cache_age: float | None
    needs_refresh: bool
    blocked: bool
    state: str
    has_valid_cache: bool
    last_error: str | None


SESSION_CACHE_STATE_VALID = "valid"
SESSION_CACHE_STATE_STALE_REUSED = "stale_reused"
SESSION_CACHE_STATE_UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class SessionCacheEntry:
    """Structured cache entry for a serial/day session-history result."""

    cached_at_mono: float | None
    sessions: list[dict[str, Any]]
    state: str
    last_error: str | None
    has_valid_cache: bool


@dataclass(slots=True)
class _SerialReadScope:
    epoch: object
    readers: int = 0
    scheduled_days: set[str] = field(default_factory=set)
    retired: bool = False


class _HistoryReadDeferred(Exception):
    """The site cooldown started before transport could send this read."""


_SessionRead = asyncio.Future[list[dict[str, Any]] | None]


class SessionHistoryManager:
    """Encapsulate session history caching, fetching, and enrichment."""

    def __init__(
        self,
        hass: HomeAssistant,
        client_getter: Callable[[], Any],
        *,
        cache_ttl: float,
        cache_day_retention: int = SESSION_HISTORY_CACHE_DAY_RETENTION,
        failure_backoff: float = SESSION_HISTORY_FAILURE_BACKOFF_S,
        concurrency: int = SESSION_HISTORY_CONCURRENCY,
        data_supplier: Callable[[], dict[str, dict[str, Any]] | None] | None = None,
        publish_callback: Callable[[dict[str, dict[str, Any]]], None] | None = None,
        site_id_getter: Callable[[], object] | None = None,
        runtime_active: Callable[[], bool] = lambda: True,
        publication_day: Callable[[dict[str, Any], datetime], datetime] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._hass = hass
        self._client_getter = client_getter
        self._cache_ttl = max(MIN_SESSION_HISTORY_CACHE_TTL, float(cache_ttl or 0))
        self._failure_backoff = max(
            MIN_SESSION_HISTORY_CACHE_TTL, float(failure_backoff)
        )
        self._concurrency = max(1, int(concurrency))
        self._cache_day_retention = max(1, int(cache_day_retention))
        self._cache: dict[
            tuple[str, str], SessionCacheEntry | tuple[float, list[dict[str, Any]]]
        ] = {}
        self._block_until: dict[str, float] = {}
        self._criteria_checked_mono: float | None = None
        self._criteria_lock = asyncio.Lock()
        self._session_reads: dict[tuple[str, str], _SessionRead] = {}
        self._serial_read_scopes: dict[str, _SerialReadScope] = {}
        self._runtime_epoch = object()
        self._runtime_active = runtime_active
        self._enrichment_tasks: set[asyncio.Task[None]] = set()
        self._data_supplier = data_supplier
        self._publish_callback = publish_callback
        self._publication_day = publication_day
        self._site_id_getter = site_id_getter
        self._logger = logger or _LOGGER
        self._fetch_override: SessionFetchCallback | None = None
        self._service_available = True
        self._service_failures = 0
        self._service_last_error: str | None = None
        self._service_last_failure_utc: datetime | None = None
        self._service_backoff_until: float | None = None
        self._service_backoff_ends_utc: datetime | None = None
        self._service_using_stale = False
        self._service_last_payload_signature: dict[str, object] | None = None
        self._pagination_diagnostics: dict[str, object] = {}

    @property
    def pagination_diagnostics(self) -> dict[str, object]:
        """Return detached, identifier-free results of the last paginated read."""
        return dict(self._pagination_diagnostics)

    def _site_ids(self) -> tuple[object, ...]:
        if self._site_id_getter is None:
            return ()
        try:
            site_id = self._site_id_getter()
        except Exception:  # noqa: BLE001
            return ()
        return (site_id,) if site_id else ()

    def _redact_error(self, err: object) -> str:
        response_error = (
            err.__cause__ if isinstance(err, SessionHistoryUnavailable) else err
        )
        if (
            isinstance(response_error, aiohttp.ClientResponseError)
            and response_error.status == 429
        ):
            return "Session history rate limited"
        return str(redact_text(err, site_ids=self._site_ids()))

    @property
    def cache_ttl(self) -> float:
        """Return the cache TTL in seconds."""
        return self._cache_ttl

    @cache_ttl.setter
    def cache_ttl(self, value: float | None) -> None:
        """Update the cache TTL, enforcing sane bounds."""
        if value is None:
            self._cache_ttl = MIN_SESSION_HISTORY_CACHE_TTL
            return
        try:
            ttl = float(value)
        except (TypeError, ValueError):
            ttl = MIN_SESSION_HISTORY_CACHE_TTL
        self._cache_ttl = max(MIN_SESSION_HISTORY_CACHE_TTL, ttl)

    @property
    def cache_key_count(self) -> int:
        """Return the number of cached serial/day entries."""
        return len(self._cache)

    def cache_state_counts(self) -> dict[str, int]:
        """Return counts of cache entries by tri-state."""
        counts = {
            SESSION_CACHE_STATE_VALID: 0,
            SESSION_CACHE_STATE_STALE_REUSED: 0,
            SESSION_CACHE_STATE_UNAVAILABLE: 0,
        }
        for cache_key in list(self._cache):
            entry = self._get_cache_entry(cache_key)
            if entry is None:
                continue
            counts[entry.state] = counts.get(entry.state, 0) + 1
        return counts

    @property
    def in_progress(self) -> int:
        """Return the number of serials going through enrichment."""
        return sum(
            bool(scope.scheduled_days) for scope in self._serial_read_scopes.values()
        )

    @property
    def service_available(self) -> bool:
        """Return True when session history service is available."""
        return bool(self._service_available and not self._service_backoff_active())

    @property
    def service_last_error(self) -> str | None:
        return self._service_last_error

    @property
    def service_failures(self) -> int:
        return self._service_failures

    @property
    def service_backoff_active(self) -> bool:
        return self._service_backoff_active()

    @property
    def service_backoff_ends_utc(self) -> datetime | None:
        return self._service_backoff_ends_utc

    @property
    def service_last_failure_utc(self) -> datetime | None:
        return self._service_last_failure_utc

    @property
    def service_using_stale(self) -> bool:
        return self._service_using_stale

    def _service_backoff_active(self) -> bool:
        return bool(
            self._service_backoff_until
            and time.monotonic() < self._service_backoff_until
        )

    def _mark_service_available(self) -> None:
        if self._service_available and not self._service_using_stale:
            return
        self._service_available = True
        self._service_failures = 0
        self._service_last_error = None
        self._service_last_failure_utc = None
        self._service_backoff_until = None
        self._service_backoff_ends_utc = None
        self._service_using_stale = False
        self._service_last_payload_signature = None

    def _note_service_unavailable(
        self,
        err: Exception | str | None,
        *,
        using_stale: bool = False,
    ) -> None:
        reason = self._redact_error(err) if err else ""
        if not reason:
            reason = "Session history unavailable"
        self._service_available = False
        self._service_failures += 1
        self._service_last_error = reason
        self._service_last_failure_utc = dt_util.utcnow()
        self._service_using_stale = using_stale or (
            self._service_using_stale and self._service_backoff_active()
        )
        self._service_last_payload_signature = (
            err.signature_dict() if isinstance(err, InvalidPayloadError) else None
        )
        delay = max(self._failure_backoff, MIN_SESSION_HISTORY_CACHE_TTL)
        if isinstance(err, BaseException):
            delay = max(delay, retry_after_delay(err) or 0.0)
        deadline = time.monotonic() + delay
        if (
            self._service_backoff_until is not None
            and self._service_backoff_until >= deadline
        ):
            return
        self._service_backoff_until = deadline
        try:
            self._service_backoff_ends_utc = dt_util.utcnow() + timedelta(seconds=delay)
        except Exception:
            self._service_backoff_ends_utc = None

    def _history_timezone(self) -> str | None:
        tz_name = self._hass.config.time_zone
        if tz_name:
            return str(tz_name)
        try:
            return str(dt_util.DEFAULT_TIME_ZONE)
        except Exception:
            return None

    def _entry_error_text(self, err: Exception | str | None) -> str | None:
        if err is None:
            return None
        reason = self._redact_error(err)
        if reason:
            return reason
        if isinstance(err, str):
            cleaned = err.strip()
            return cleaned or None
        return err.__class__.__name__

    def _coerce_cache_entry(
        self,
        value: SessionCacheEntry | tuple[float, list[dict[str, Any]]] | None,
    ) -> SessionCacheEntry | None:
        if isinstance(value, SessionCacheEntry):
            return value
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], list):
            cached_at_mono = value[0]
            if not isinstance(cached_at_mono, (int, float)):
                cached_at_mono = None
            return SessionCacheEntry(
                cached_at_mono=(
                    float(cached_at_mono) if cached_at_mono is not None else None
                ),
                sessions=value[1],
                state=SESSION_CACHE_STATE_VALID,
                last_error=None,
                has_valid_cache=True,
            )
        return None

    def _get_cache_entry(self, cache_key: tuple[str, str]) -> SessionCacheEntry | None:
        cached = self._coerce_cache_entry(self._cache.get(cache_key))
        if cached is not None:
            self._cache[cache_key] = cached
        return cached

    def _set_unavailable_entry(
        self,
        serial: str,
        day_key: str,
        now_mono: float,
        err: Exception | str | None,
    ) -> None:
        self._set_cache_entry(
            serial,
            day_key,
            SessionCacheEntry(
                cached_at_mono=now_mono,
                sessions=[],
                state=SESSION_CACHE_STATE_UNAVAILABLE,
                last_error=self._entry_error_text(err),
                has_valid_cache=False,
            ),
        )

    def _set_stale_reused_entry(
        self,
        serial: str,
        day_key: str,
        cached: SessionCacheEntry,
        err: Exception | str | None,
    ) -> None:
        self._set_cache_entry(
            serial,
            day_key,
            SessionCacheEntry(
                cached_at_mono=cached.cached_at_mono,
                sessions=list(cached.sessions),
                state=SESSION_CACHE_STATE_STALE_REUSED,
                last_error=self._entry_error_text(err),
                has_valid_cache=True,
            ),
        )

    def get_cache_view(
        self,
        serial: str,
        day_key: str,
        now_mono: float | None = None,
    ) -> SessionCacheView:
        """Return the cache state for a serial/day pair."""
        now = now_mono or time.monotonic()
        cache_key = (serial, day_key)
        cached = self._get_cache_entry(cache_key)
        sessions: list[dict[str, Any]] = []
        cache_age: float | None = None
        if cached:
            cached_ts = cached.cached_at_mono
            cache_age = None if cached_ts is None else now - cached_ts
            sessions = cached.sessions if cached.has_valid_cache else []
        has_valid_cache = bool(cached and cached.has_valid_cache)
        needs_refresh = not has_valid_cache or (
            cache_age is not None and cache_age >= self._cache_ttl
        )
        block_until = self._block_until.get(serial)
        blocked = (
            bool(block_until and block_until > now) or self._service_backoff_active()
        )
        return SessionCacheView(
            sessions=sessions or [],
            cache_age=cache_age,
            needs_refresh=needs_refresh,
            blocked=blocked,
            state=(
                cached.state if cached is not None else SESSION_CACHE_STATE_UNAVAILABLE
            ),
            has_valid_cache=has_valid_cache,
            last_error=cached.last_error if cached is not None else None,
        )

    def schedule_enrichment(self, serials: Iterable[str], day_local: datetime) -> None:
        """Launch a background enrichment task for the provided serials."""
        self.schedule_enrichment_with_options(serials, day_local=day_local)

    def schedule_enrichment_with_options(
        self,
        serials: Iterable[str],
        *,
        day_local: datetime,
        max_cache_age: float | None = None,
    ) -> None:
        """Launch a background enrichment task for the provided serials."""
        if not self._runtime_active():
            return
        day_key = self._as_local_history_day(day_local).strftime("%Y-%m-%d")
        pending = {}
        for sn in dict.fromkeys(serials):
            if sn:
                scope = self._session_read_scope(sn)
                if day_key not in scope.scheduled_days:
                    scope.scheduled_days.add(day_key)
                    pending[sn] = scope
        if not pending:
            return

        async def _run() -> None:
            updates = await self._async_enrich_sessions(
                (sn for sn, scope in pending.items() if not scope.retired),
                day_local=day_local,
                max_cache_age=max_cache_age,
            )
            if updates:
                self._apply_updates(updates, day_local)

        def _finished(task: asyncio.Task[None]) -> None:
            self._enrichment_tasks.discard(task)
            for sn, scope in pending.items():
                scope.scheduled_days.discard(day_key)
                self._discard_unused_read_scope(sn, scope)

        task = self._hass.async_create_task(
            _run(),
            name="enphase_ev_session_enrichment",
        )
        self._enrichment_tasks.add(task)
        task.add_done_callback(_finished)

    async def async_enrich(
        self,
        serials: Iterable[str],
        day_local: datetime,
        *,
        in_background: bool,
        max_cache_age: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch session history for the provided serials."""
        updates = await self._async_enrich_sessions(
            serials,
            day_local=day_local,
            max_cache_age=max_cache_age,
        )
        if in_background and updates:
            self._apply_updates(updates, day_local)
        return updates

    async def _async_enrich_sessions(
        self,
        serials: Iterable[str],
        *,
        day_local: datetime,
        max_cache_age: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        with request_metrics_scope("session_history"), enlighten_optional_read_scope():
            return await self._async_enrich_sessions_impl(
                serials,
                day_local=day_local,
                max_cache_age=max_cache_age,
            )

    async def _async_enrich_sessions_impl(
        self,
        serials: Iterable[str],
        *,
        day_local: datetime,
        max_cache_age: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        serial_list = [sn for sn in dict.fromkeys(serials) if sn]
        if not serial_list or not self._runtime_active():
            return {}
        with ExitStack() as stack:
            scopes = {
                sn: stack.enter_context(self._track_session_read(sn))
                for sn in serial_list
            }
            return await self._async_run_enrichment(
                scopes, day_local=day_local, max_cache_age=max_cache_age
            )

    async def _async_run_enrichment(
        self,
        scopes: dict[str, _SerialReadScope],
        *,
        day_local: datetime,
        max_cache_age: float | None,
    ) -> dict[str, list[dict[str, Any]]]:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def _refresh(sn: str) -> tuple[str, list[dict[str, Any]] | None]:
            async with semaphore:
                if not self._scope_current(scopes[sn]):
                    return sn, None
                try:
                    if max_cache_age is None:
                        sessions = await self._async_fetch_sessions_today(
                            sn,
                            day_local=day_local,
                        )
                    else:
                        sessions = await self._async_fetch_sessions_today(
                            sn,
                            day_local=day_local,
                            max_cache_age=max_cache_age,
                        )
                except asyncio.CancelledError as err:
                    self._logger.debug(
                        "Session history enrichment cancelled for %s: %s",
                        sn,
                        self._redact_error(err),
                    )
                    return sn, None
                except Unauthorized as err:
                    self._logger.debug(
                        "Session history unauthorized for %s during enrichment: %s",
                        sn,
                        self._redact_error(err),
                    )
                    return sn, None
                except Exception as err:  # noqa: BLE001
                    self._logger.debug(
                        "Session history enrichment failed for %s: %s",
                        sn,
                        self._redact_error(err),
                    )
                    return sn, None
                return sn, sessions

        tasks = [
            asyncio.create_task(
                _refresh(sn),
                name=f"enphase_ev_session_refresh_{redact_identifier(sn)}",
            )
            for sn in scopes
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        updates: dict[str, list[dict[str, Any]]] = {}
        for response in responses:
            if isinstance(response, BaseException):
                self._logger.debug(
                    "Session history enrichment task error: %s",
                    self._redact_error(response),
                )
                continue
            sn, sessions = response
            if sessions is None or not self._scope_current(scopes[sn]):
                continue
            updates[sn] = sessions
        return updates

    def _apply_updates(
        self, updates: dict[str, list[dict[str, Any]]], day_local: datetime
    ) -> None:
        """Merge enrichment results into the published coordinator data."""
        if (
            not updates
            or not self._publish_callback
            or not self._data_supplier
            or not self._runtime_active()
        ):
            return
        current = self._data_supplier()
        if not isinstance(current, dict):
            return
        merged: dict[str, dict[str, Any]] | None = None
        select_day = self._publication_day
        if select_day is not None:
            requested_day = self._as_local_history_day(day_local).date()
            current_day = self._as_local_history_day(dt_util.now())
        for sn, sessions in updates.items():
            existing = current.get(sn)
            if select_day is not None and (
                not isinstance(existing, dict)
                or select_day(existing, current_day).date() != requested_day
            ):
                continue
            entry = dict(existing) if isinstance(existing, dict) else {}
            sessions_kwh = self._sum_session_energy(sessions)
            if (
                entry.get("energy_today_sessions") == sessions
                and entry.get("energy_today_sessions_kwh") == sessions_kwh
            ):
                continue
            if merged is None:
                merged = dict(current)
            entry["energy_today_sessions"] = sessions
            entry["energy_today_sessions_kwh"] = sessions_kwh
            merged[sn] = entry
        if merged is not None:
            self._publish_callback(merged)

    def _session_read_scope(self, sn: str) -> _SerialReadScope:
        scope = self._serial_read_scopes.get(sn)
        if scope is None:
            scope = self._serial_read_scopes[sn] = _SerialReadScope(self._runtime_epoch)
        return scope

    def _discard_unused_read_scope(self, sn: str, scope: _SerialReadScope) -> None:
        if (
            not scope.scheduled_days
            and not scope.readers
            and self._serial_read_scopes.get(sn) is scope
        ):
            del self._serial_read_scopes[sn]

    @contextmanager
    def _track_session_read(self, sn: str) -> Iterator[_SerialReadScope]:
        scope = self._session_read_scope(sn)
        scope.readers += 1
        try:
            yield scope
        finally:
            scope.readers -= 1
            self._discard_unused_read_scope(sn, scope)

    def _sessions_during_backoff(
        self, sn: str, day_key: str, cached: SessionCacheEntry | None
    ) -> list[dict[str, Any]]:
        if cached is None or not cached.has_valid_cache:
            return []
        self._service_using_stale = True
        if cached.state != SESSION_CACHE_STATE_STALE_REUSED:
            self._set_stale_reused_entry(sn, day_key, cached, self._service_last_error)
        return cached.sessions

    async def _async_refresh_filter_criteria(
        self,
        criteria_fetcher: Callable[..., Awaitable[dict[str, Any]]],
        scope: _SerialReadScope,
    ) -> None:
        """Fetch site-scoped criteria once per cache window under its lock."""

        checked_mono = self._criteria_checked_mono
        if (
            checked_mono is not None
            and (time.monotonic() - checked_mono) < self._cache_ttl
        ):
            return

        with self._guard_session_transport(scope):
            await criteria_fetcher(request_id=str(uuid.uuid4()))
        self._criteria_checked_mono = time.monotonic()

    def _scope_runtime_current(self, scope: _SerialReadScope) -> bool:
        return scope.epoch is self._runtime_epoch and self._runtime_active()

    def _scope_current(self, scope: _SerialReadScope) -> bool:
        return not scope.retired and self._scope_runtime_current(scope)

    @contextmanager
    def _guard_session_read(self, scope: _SerialReadScope) -> Iterator[None]:
        """Discard results and failures from an invalidated acquisition."""
        if not self._scope_current(scope):
            raise asyncio.CancelledError
        try:
            yield
        finally:
            if not self._scope_current(scope):
                raise asyncio.CancelledError

    @contextmanager
    def _guard_session_transport(self, scope: _SerialReadScope) -> Iterator[None]:
        """Check queued dispatch and still honor a sent read's site-wide 429."""
        with (
            self._guard_session_read(scope),
            guarded_read_scope(
                lambda: self._scope_current(scope),
                can_dispatch=lambda: not self._service_backoff_active(),
                on_retired_rate_limit=lambda err: self._note_retired_rate_limit(
                    scope, err
                ),
            ),
        ):
            try:
                yield
            except EnlightenReadSuperseded:
                if self._scope_current(scope) and self._service_backoff_active():
                    raise _HistoryReadDeferred from None
                raise asyncio.CancelledError from None
            except aiohttp.ClientResponseError as err:
                self._note_retired_rate_limit(scope, err)
                raise

    def _note_retired_rate_limit(
        self, scope: _SerialReadScope, err: aiohttp.ClientResponseError
    ) -> None:
        if err.status == 429 and scope.retired and self._scope_runtime_current(scope):
            self._note_service_unavailable(err)

    async def _async_fetch_sessions_today(
        self,
        sn: str,
        *,
        day_local: datetime | None = None,
        max_cache_age: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return session history for the provided day, caching results."""
        if not sn and self._fetch_override is None:
            return []
        with self._track_session_read(sn) as scope, self._guard_session_read(scope):
            if self._fetch_override is not None:
                return await self._fetch_override(sn, day_local=day_local)
            return await self._async_fetch_scoped_sessions(
                sn, day_local, max_cache_age, scope
            )

    @staticmethod
    def _as_local_history_day(day_local: datetime) -> datetime:
        try:
            return cast(datetime, dt_util.as_local(day_local))
        except Exception:
            if day_local.tzinfo is None:
                day_local = day_local.replace(tzinfo=dt_util.UTC)
            return cast(datetime, dt_util.as_local(day_local))

    async def _async_fetch_scoped_sessions(
        self,
        sn: str,
        day_local: datetime | None,
        max_cache_age: float | None,
        scope: _SerialReadScope,
    ) -> list[dict[str, Any]]:
        if day_local is None:
            day_local = dt_util.now()
        local_dt = self._as_local_history_day(day_local)

        day_key = local_dt.strftime("%Y-%m-%d")
        cache_key = (sn, day_key)
        active_serials = self._active_serials_from_data_supplier()
        if active_serials is not None:
            active_serials.add(sn)
        self._prune_cached_entries(active_serials, {day_key})
        refresh_after = self._cache_ttl
        if max_cache_age is not None:
            try:
                refresh_after = max(
                    MIN_SESSION_HISTORY_CACHE_TTL,
                    min(self._cache_ttl, float(max_cache_age)),
                )
            except (TypeError, ValueError):
                refresh_after = self._cache_ttl
        while True:
            now_mono = time.monotonic()
            cached = self._get_cache_entry(cache_key)
            if (
                cached is not None
                and cached.has_valid_cache
                and cached.cached_at_mono is not None
                and (now_mono - cached.cached_at_mono) < refresh_after
            ):
                return cached.sessions
            if self._service_backoff_active():
                return self._sessions_during_backoff(sn, day_key, cached)
            block_until = self._block_until.get(sn)
            if block_until and now_mono < block_until:
                return cached.sessions if cached and cached.has_valid_cache else []

            pending = self._session_reads.get(cache_key)
            if pending is None:
                break
            with self._guard_session_read(scope):
                shared = await asyncio.shield(pending)
            if shared is not None:
                return list(shared)

        read: _SessionRead = asyncio.get_running_loop().create_future()
        self._session_reads[cache_key] = read
        result = None
        try:
            with self._guard_session_read(scope):
                result = await self._async_acquire_sessions(
                    sn, local_dt, day_key, cached, scope
                )
            return result
        finally:
            if self._session_reads.get(cache_key) is read:
                del self._session_reads[cache_key]
            if not read.done():
                # A cancelled owner lets its waiters elect a new owner.
                read.set_result(result)

    async def _async_acquire_sessions(
        self,
        sn: str,
        local_dt: datetime,
        day_key: str,
        cached: SessionCacheEntry | None,
        scope: _SerialReadScope,
    ) -> list[dict[str, Any]]:
        now_mono = time.monotonic()
        api_day = local_dt.strftime("%d-%m-%Y")
        client = self._client_getter()
        if client is None:
            self._logger.debug("Session history fetch skipped; client unavailable")
            return []
        timezone_name = self._history_timezone()

        criteria_fetcher = getattr(client, "session_history_filter_criteria", None)
        if callable(criteria_fetcher):
            async with self._criteria_lock:
                if not self._scope_current(scope):
                    raise asyncio.CancelledError
                if self._service_backoff_active():
                    return self._sessions_during_backoff(sn, day_key, cached)
                try:
                    await self._async_refresh_filter_criteria(criteria_fetcher, scope)
                except _HistoryReadDeferred:
                    return self._sessions_during_backoff(sn, day_key, cached)
                except SessionHistoryUnavailable as err:
                    self._logger.debug(
                        "Session history criteria unavailable for %s: %s",
                        sn,
                        self._redact_error(err),
                    )
                    if cached and cached.has_valid_cache:
                        self._note_service_unavailable(err, using_stale=True)
                        self._set_stale_reused_entry(sn, day_key, cached, err)
                        return cached.sessions
                    self._note_service_unavailable(err)
                    self._set_unavailable_entry(sn, day_key, now_mono, err)
                    return []
                except Unauthorized as err:
                    self._logger.debug(
                        "Session history criteria unauthorized for %s: %s",
                        sn,
                        self._redact_error(err),
                    )
                    self._set_unavailable_entry(sn, day_key, now_mono, err)
                    return []
                except aiohttp.ClientResponseError as err:
                    self._logger.debug(
                        "Session history criteria error for %s: %s (%s)",
                        sn,
                        err.status,
                        self._redact_error(err.message),
                    )
                    if err.status == 429:
                        self._note_service_unavailable(
                            err, using_stale=bool(cached and cached.has_valid_cache)
                        )
                        if cached and cached.has_valid_cache:
                            self._set_stale_reused_entry(sn, day_key, cached, err)
                            return cached.sessions
                    if err.status in (500, 502, 503, 504, 550):
                        self._block_until[sn] = now_mono + self._failure_backoff
                    self._set_unavailable_entry(sn, day_key, now_mono, err)
                    return []
                except Exception as err:  # noqa: BLE001
                    self._logger.debug(
                        "Session history criteria failed for %s: %s",
                        sn,
                        self._redact_error(err),
                    )
                    if cached and cached.has_valid_cache:
                        self._note_service_unavailable(err, using_stale=True)
                        self._set_stale_reused_entry(sn, day_key, cached, err)
                        return cached.sessions
                    self._set_unavailable_entry(sn, day_key, now_mono, err)
                    return []

        async def _fetch_page(offset: int, limit: int) -> object:
            return await client.session_history(
                sn,
                start_date=api_day,
                end_date=api_day,
                offset=offset,
                limit=limit,
                timezone=timezone_name,
                request_id=str(uuid.uuid4()),
            )

        try:
            results = await self._async_read_session_pages(_fetch_page, scope)
        except SessionHistoryUnavailable as err:
            self._logger.debug(
                "Session history unavailable for %s on %s: %s",
                sn,
                api_day,
                self._redact_error(err),
            )
            if cached and cached.has_valid_cache:
                self._note_service_unavailable(err, using_stale=True)
                self._set_stale_reused_entry(sn, day_key, cached, err)
                return cached.sessions
            self._note_service_unavailable(err)
            self._set_unavailable_entry(sn, day_key, now_mono, err)
            return []
        except Unauthorized as err:
            self._logger.debug(
                "Session history unauthorized for %s on %s: %s",
                sn,
                api_day,
                self._redact_error(err),
            )
            self._set_unavailable_entry(sn, day_key, now_mono, err)
            return []
        except aiohttp.ClientResponseError as err:
            self._logger.debug(
                "Session history server error for %s on %s: %s (%s)",
                sn,
                api_day,
                err.status,
                self._redact_error(err.message),
            )
            if err.status == 429:
                self._note_service_unavailable(
                    err, using_stale=bool(cached and cached.has_valid_cache)
                )
                if cached and cached.has_valid_cache:
                    self._set_stale_reused_entry(sn, day_key, cached, err)
                    return cached.sessions
            if err.status in (500, 502, 503, 504, 550):
                self._block_until[sn] = now_mono + self._failure_backoff
            self._set_unavailable_entry(sn, day_key, now_mono, err)
            return []
        except Exception as err:  # noqa: BLE001
            self._logger.debug(
                "Session history fetch failed for %s on %s: %s",
                sn,
                api_day,
                self._redact_error(err),
            )
            if cached and cached.has_valid_cache:
                self._note_service_unavailable(err, using_stale=True)
                self._set_stale_reused_entry(sn, day_key, cached, err)
                return cached.sessions
            if isinstance(err, InvalidPayloadError):
                self._note_service_unavailable(err)
            self._set_unavailable_entry(sn, day_key, now_mono, err)
            return []
        if results is None:
            return self._sessions_during_backoff(sn, day_key, cached)
        sessions = self._normalise_sessions_for_day(local_dt=local_dt, results=results)
        if not self._service_backoff_active():
            self._mark_service_available()
        self._block_until.pop(sn, None)
        self._set_cache_entry(
            sn,
            day_key,
            SessionCacheEntry(
                cached_at_mono=now_mono,
                sessions=list(sessions),
                state=SESSION_CACHE_STATE_VALID,
                last_error=None,
                has_valid_cache=True,
            ),
        )
        return sessions

    async def _async_read_session_pages(
        self,
        fetch_page: Callable[[int, int], Awaitable[object]],
        scope: _SerialReadScope,
    ) -> list[dict[str, Any]] | None:
        """Fetch a complete page sequence unless a shared cooldown starts."""
        pages = SessionHistoryPages()
        offset = 0
        limit = 50
        try:
            for _ in range(5):
                if self._service_backoff_active():
                    return None
                with self._guard_session_transport(scope):
                    payload = await fetch_page(offset, limit)
                if not pages.add(payload):
                    break
                offset += limit
            return pages.finish()
        except _HistoryReadDeferred:
            return None
        finally:
            if self._scope_current(scope):
                self._pagination_diagnostics = pages.diagnostics()

    @staticmethod
    def _normalize_serials(serials: Iterable[str] | None) -> set[str] | None:
        if serials is None:
            return None
        normalized: set[str] = set()
        for serial in serials:
            if serial is None:
                continue
            try:
                sn = str(serial).strip()
            except Exception:  # noqa: BLE001
                continue
            if sn:
                normalized.add(sn)
        return normalized

    def _active_serials_from_data_supplier(self) -> set[str] | None:
        if not callable(self._data_supplier):
            return None
        try:
            data = self._data_supplier()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        return self._normalize_serials(data.keys())

    def _retained_day_keys(self, keep_day_keys: Iterable[str] | None) -> set[str]:
        day_keys = {
            str(day_key).strip()
            for day_key in keep_day_keys or ()
            if day_key is not None and str(day_key).strip()
        }
        try:
            now_local = dt_util.as_local(dt_util.now())
        except Exception:
            now_local = datetime.now(tz=_tz.utc)
        for day_offset in range(self._cache_day_retention):
            day_keys.add((now_local - timedelta(days=day_offset)).strftime("%Y-%m-%d"))
        return day_keys

    def _set_cache_entry(
        self,
        serial: str,
        day_key: str,
        entry: SessionCacheEntry,
    ) -> None:
        self._cache[(serial, day_key)] = entry
        active_serials = self._active_serials_from_data_supplier()
        if active_serials is not None:
            active_serials.add(serial)
        self._prune_cached_entries(active_serials, {day_key})

    def prune(
        self,
        *,
        active_serials: Iterable[str] | None = None,
        keep_day_keys: Iterable[str] | None = None,
    ) -> None:
        """Prune stale serial/day cache entries and serial-scoped state."""
        active_set = self._normalize_serials(active_serials)
        self._prune_cached_entries(active_set, keep_day_keys)
        if active_set is not None:
            self._retire_session_reads(active_set)

    def _retire_session_reads(self, active_serials: set[str]) -> None:
        for sn in self._serial_read_scopes.keys() - active_serials:
            self._serial_read_scopes.pop(sn).retired = True
        for key in tuple(self._session_reads):
            if key[0] not in active_serials:
                self._session_reads.pop(key).cancel()

    def _prune_cached_entries(
        self,
        active_set: set[str] | None,
        keep_day_keys: Iterable[str] | None,
    ) -> None:
        retained_days = self._retained_day_keys(keep_day_keys)

        if self._cache:
            self._cache = {
                (sn, day_key): cache
                for (sn, day_key), cache in self._cache.items()
                if day_key in retained_days and (active_set is None or sn in active_set)
            }

        now_mono = time.monotonic()
        for sn, until in list(self._block_until.items()):
            if until <= now_mono or (active_set is not None and sn not in active_set):
                self._block_until.pop(sn, None)

    def clear(self) -> None:
        """Drop cached state and cancel in-flight background enrichment tasks."""
        self._runtime_epoch = object()
        self._retire_session_reads(set())
        for task in list(self._enrichment_tasks):
            task.cancel()
        self._enrichment_tasks.clear()
        self._cache.clear()
        self._block_until.clear()
        self._criteria_checked_mono = None

    def set_fetch_override(
        self,
        callback: SessionFetchCallback | None,
    ) -> None:
        """Allow callers to override the fetch implementation (legacy hook)."""
        self._fetch_override = callback

    def _normalise_sessions_for_day(
        self,
        *,
        local_dt: datetime,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Trim and normalise raw session history entries for a given local day."""

        try:
            now_local = dt_util.as_local(local_dt)
        except Exception:  # noqa: BLE001
            now_local = local_dt

        day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        def _as_float(val: object, *, precision: int | None = None) -> float | None:
            if val is None:
                return None
            try:
                out = float(cast(Any, val))
                if precision is not None:
                    return round(out, precision)
                return out
            except Exception:  # noqa: BLE001
                return None

        def _as_int(val: object) -> int | None:
            if val is None:
                return None
            if isinstance(val, bool):
                return int(val)
            try:
                return int(float(cast(Any, val)))
            except Exception:  # noqa: BLE001
                return None

        def _as_bool(val: object) -> bool:
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, float)):
                return val != 0
            if isinstance(val, str):
                return val.strip().lower() in ("true", "1", "yes", "y")
            return False

        sessions: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            start_dt = parse_session_timestamp(item.get("startTime"))
            end_dt = parse_session_timestamp(item.get("endTime"))

            if start_dt is None and end_dt is None:
                continue

            if start_dt is None:
                start_dt = end_dt
            if end_dt is None:
                end_dt = now_local

            window_start = start_dt or day_start
            window_end = end_dt or now_local

            if window_end < window_start:
                window_end = window_start

            if not (window_start < day_end and window_end >= day_start):
                continue

            energy_total_kwh = _as_float(item.get("aggEnergyValue"), precision=3)
            if energy_total_kwh is None:
                energy_total_kwh = _as_float(item.get("aggEnergyValue"))

            overlap_start = window_start if window_start > day_start else day_start
            overlap_end = window_end if window_end < day_end else day_end
            overlap_seconds = max((overlap_end - overlap_start).total_seconds(), 0.0)

            active_charge_seconds_raw = _as_int(item.get("activeChargeTime"))
            active_charge_seconds = active_charge_seconds_raw
            if (
                (active_charge_seconds is None or active_charge_seconds <= 0)
                and start_dt
                and end_dt
            ):
                active_charge_seconds = max(
                    int((end_dt - start_dt).total_seconds()),
                    0,
                )

            energy_window_kwh = energy_total_kwh
            if (
                energy_total_kwh is not None
                and active_charge_seconds
                and active_charge_seconds > 0
                and overlap_seconds
            ):
                fraction = min(max(overlap_seconds / active_charge_seconds, 0.0), 1.0)
                energy_window_kwh = round(energy_total_kwh * fraction, 3)
            elif energy_total_kwh is not None and overlap_seconds == 0:
                energy_window_kwh = 0.0

            overlap_active_seconds = (
                int(overlap_seconds) if overlap_seconds and overlap_seconds > 0 else 0
            )

            sessions.append(
                {
                    "session_id": str(item.get("sessionId") or item.get("id") or ""),
                    "start": start_dt.isoformat() if start_dt else None,
                    "end": end_dt.isoformat() if end_dt else None,
                    "auth_type": item.get("authType"),
                    "auth_identifier": item.get("authIdentifier"),
                    "auth_token": item.get("authToken"),
                    "active_charge_time_s": active_charge_seconds_raw,
                    "active_charge_time_overlap_s": overlap_active_seconds,
                    "energy_kwh_total": energy_total_kwh,
                    "energy_kwh": energy_window_kwh,
                    "miles_added": _as_float(item.get("milesAdded"), precision=3),
                    "session_cost": _as_float(item.get("sessionCost"), precision=3),
                    "avg_cost_per_kwh": _as_float(
                        item.get("avgCostPerUnitEnergy"), precision=3
                    ),
                    "cost_calculated": _as_bool(item.get("costCalculated")),
                    "manual_override": _as_bool(item.get("manualOverridden")),
                    "session_cost_state": item.get("sessionCostState"),
                    "charge_profile_stack_level": _as_int(
                        item.get("chargeProfileStackLevel")
                    ),
                }
            )

        sessions.sort(
            key=lambda entry: (
                entry.get("start") or "",
                entry.get("session_id") or "",
            )
        )
        return sessions

    def sum_energy(self, sessions: list[dict[str, Any]]) -> float:
        """Public wrapper for summing session energy."""
        return self._sum_session_energy(sessions)

    def _sum_session_energy(self, sessions: list[dict[str, Any]]) -> float:
        """Compute total energy from session entries."""
        total = 0.0
        for entry in sessions or []:
            val = entry.get("energy_kwh")
            if isinstance(val, (int, float)):
                try:
                    total += float(val)
                except Exception:  # noqa: BLE001
                    continue
        return round(total, 3)
