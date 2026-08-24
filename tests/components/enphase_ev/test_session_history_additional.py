"""Additional coverage for SessionHistoryManager helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.enphase_ev import session_history as sh_mod
from custom_components.enphase_ev.api import SessionHistoryUnavailable, Unauthorized

pytest.importorskip("homeassistant")


def _close_task(coro, name=None):
    try:
        coro.close()
    except AttributeError:
        pass
    return None


def _make_hass():
    sh_mod.dt_util.set_default_time_zone(timezone.utc)
    return SimpleNamespace(
        async_create_task=_close_task,
        config=SimpleNamespace(time_zone="UTC"),
    )


def test_history_timezone_falls_back_when_default_invalid(monkeypatch):
    hass = _make_hass()
    hass.config.time_zone = None
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
    )

    class BadTZ:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    # Restore Home Assistant's process-global timezone before its autouse
    # cleanup fixture validates test isolation.
    with monkeypatch.context() as patch_context:
        patch_context.setattr(sh_mod.dt_util, "DEFAULT_TIME_ZONE", BadTZ())
        assert manager._history_timezone() is None


@pytest.mark.asyncio
async def test_async_enrich_sessions_handles_task_errors(monkeypatch):
    hass = _make_hass()
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
    )
    serial = "123456789012"
    names: list[str | None] = []

    async def fake_gather(*tasks, **kwargs):
        return [RuntimeError("boom")]

    def fake_task(coro, *, name=None):
        names.append(name)
        coro.close()
        return None

    monkeypatch.setattr(sh_mod.asyncio, "gather", fake_gather)
    monkeypatch.setattr(sh_mod.asyncio, "create_task", fake_task)

    updates = await manager._async_enrich_sessions(
        [serial], day_local=datetime.now(timezone.utc)
    )
    assert updates == {}
    assert len(names) == 1
    assert serial not in str(names[0])
    assert "1234...9012" in str(names[0])


@pytest.mark.asyncio
async def test_fetch_sessions_respects_block_until(monkeypatch):
    hass = _make_hass()
    client = SimpleNamespace(session_history=AsyncMock())
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: client,
        cache_ttl=60,
    )

    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: now)
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    cache_key = ("SN", now.strftime("%Y-%m-%d"))
    current = time.monotonic()
    manager._cache[cache_key] = (current, ["cached"])
    manager._block_until["SN"] = current + 10

    result = await manager._async_fetch_sessions_today("SN", day_local=now)
    assert result == ["cached"]


@pytest.mark.asyncio
async def test_fetch_sessions_blocked_without_cache(monkeypatch):
    hass = _make_hass()
    client = SimpleNamespace(session_history=AsyncMock())
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: client,
        cache_ttl=60,
    )

    now = datetime(2025, 1, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: now)
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    manager._block_until["SN"] = time.monotonic() + 5
    result = await manager._async_fetch_sessions_today("SN", day_local=now)
    assert result == []


@pytest.mark.asyncio
async def test_fetch_sessions_handles_paging(monkeypatch):
    hass = _make_hass()
    calls = {"count": 0}

    async def fake_history(sn, start_date, end_date, offset, limit, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "data": {
                    "result": [
                        {
                            "startTime": "2025-01-01T00:00:00Z",
                            "endTime": "2025-01-01T01:00:00Z",
                            "aggEnergyValue": 1.0,
                        }
                    ]
                    * limit,
                    "hasMore": True,
                }
            }
        return {
            "data": {
                "result": [],
                "hasMore": False,
            }
        }

    client = SimpleNamespace(session_history=AsyncMock(side_effect=fake_history))
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: client,
        cache_ttl=60,
    )
    monkeypatch.setattr(
        sh_mod.dt_util, "now", lambda: datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    sessions = await manager._async_fetch_sessions_today(
        "SN", day_local=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    assert sessions


@pytest.mark.asyncio
async def test_fetch_sessions_criteria_unavailable(monkeypatch, caplog):
    hass = _make_hass()
    caplog.set_level(logging.DEBUG, logger=sh_mod._LOGGER.name)

    async def fake_criteria(**_kwargs):
        raise SessionHistoryUnavailable(
            "GET /service/enho_historical_events_ms/9990001/filter_criteria"
        )

    client = SimpleNamespace(
        session_history_filter_criteria=fake_criteria,
        session_history=AsyncMock(),
    )
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: client,
        cache_ttl=60,
        site_id_getter=lambda: "9990001",
    )
    now = datetime(2025, 1, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: now)
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    result = await manager._async_fetch_sessions_today("SN", day_local=now)
    assert result == []
    assert manager.service_available is False
    assert "9990001" not in str(manager.service_last_error)
    assert "9990001" not in caplog.text
    assert "/enho_historical_events_ms/[site]/filter_criteria" in caplog.text
    assert "/enho_historical_events_ms/[site]/filter_criteria" in str(
        manager.service_last_error
    )
    entry = manager._cache[("SN", now.strftime("%Y-%m-%d"))]
    assert entry.state == sh_mod.SESSION_CACHE_STATE_UNAVAILABLE
    assert entry.has_valid_cache is False
    assert "9990001" not in str(entry.last_error)


@pytest.mark.asyncio
async def test_fetch_sessions_criteria_unauthorized(monkeypatch):
    hass = _make_hass()

    async def fake_criteria(**_kwargs):
        raise Unauthorized("nope")

    client = SimpleNamespace(
        session_history_filter_criteria=fake_criteria,
        session_history=AsyncMock(),
    )
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: client,
        cache_ttl=60,
    )
    now = datetime(2025, 1, 6, tzinfo=timezone.utc)
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: now)
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    result = await manager._async_fetch_sessions_today("SN", day_local=now)
    assert result == []
    assert manager.service_available is True
    entry = manager._cache[("SN", now.strftime("%Y-%m-%d"))]
    assert entry.state == sh_mod.SESSION_CACHE_STATE_UNAVAILABLE
    assert entry.has_valid_cache is False


@pytest.mark.asyncio
async def test_fetch_sessions_criteria_http_error(monkeypatch):
    hass = _make_hass()

    async def fake_criteria(**_kwargs):
        req_info = SimpleNamespace(real_url="https://example.test")
        raise aiohttp.ClientResponseError(
            request_info=req_info, history=(), status=550, message="boom"
        )

    client = SimpleNamespace(
        session_history_filter_criteria=fake_criteria,
        session_history=AsyncMock(),
    )
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: client,
        cache_ttl=60,
    )
    now = datetime(2025, 1, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: now)
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    result = await manager._async_fetch_sessions_today("SN", day_local=now)
    assert result == []
    assert "SN" in manager._block_until
    entry = manager._cache[("SN", now.strftime("%Y-%m-%d"))]
    assert entry.state == sh_mod.SESSION_CACHE_STATE_UNAVAILABLE
    assert entry.has_valid_cache is False


@pytest.mark.asyncio
async def test_fetch_sessions_marks_service_unavailable(monkeypatch, caplog):
    hass = _make_hass()
    caplog.set_level(logging.DEBUG, logger=sh_mod._LOGGER.name)
    client = SimpleNamespace(
        session_history=AsyncMock(
            side_effect=SessionHistoryUnavailable(
                "POST /service/enho_historical_events_ms/9990001/sessions/SN/history"
            )
        )
    )
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: client,
        cache_ttl=60,
        site_id_getter=lambda: "9990001",
    )
    now = datetime(2025, 1, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: now)
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    result = await manager._async_fetch_sessions_today("SN", day_local=now)
    assert result == []
    assert manager.service_available is False
    assert manager.service_backoff_active is True
    assert manager.service_last_error
    assert "9990001" not in str(manager.service_last_error)
    assert "9990001" not in caplog.text
    assert "/enho_historical_events_ms/[site]/sessions/SN/history" in caplog.text
    entry = manager._cache[("SN", now.strftime("%Y-%m-%d"))]
    assert entry.state == sh_mod.SESSION_CACHE_STATE_UNAVAILABLE
    assert entry.has_valid_cache is False


def test_mark_service_available_resets_state():
    hass = _make_hass()
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
    )
    manager._service_available = False
    manager._service_failures = 2
    manager._service_last_error = "down"
    manager._service_backoff_until = time.monotonic() + 60
    manager._mark_service_available()
    assert manager.service_available is True
    assert manager.service_failures == 0
    assert manager.service_last_error is None


def test_note_service_unavailable_default_and_backoff_error(monkeypatch):
    hass = _make_hass()
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
    )

    calls = {"count": 0}

    def fake_utcnow():
        calls["count"] += 1
        if calls["count"] == 1:
            return datetime(2025, 1, 1, tzinfo=timezone.utc)
        raise RuntimeError("boom")

    monkeypatch.setattr(sh_mod.dt_util, "utcnow", fake_utcnow)

    manager._note_service_unavailable(None)

    assert manager.service_last_error == "Session history unavailable"
    assert manager.service_backoff_ends_utc is None


@pytest.mark.asyncio
async def test_fetch_sessions_returns_cached_during_service_backoff(monkeypatch):
    hass = _make_hass()
    client = SimpleNamespace(session_history=AsyncMock())
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: client,
        cache_ttl=60,
    )
    now = datetime(2025, 1, 4, tzinfo=timezone.utc)
    monkeypatch.setattr(sh_mod.dt_util, "now", lambda: now)
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)

    cache_key = ("SN", now.strftime("%Y-%m-%d"))
    current = time.monotonic()
    manager._cache[cache_key] = (current - (manager.cache_ttl + 1), ["cached"])
    manager._service_backoff_until = current + 60

    result = await manager._async_fetch_sessions_today("SN", day_local=now)
    assert result == ["cached"]


def test_normalise_sessions_handles_invalid_values(monkeypatch):
    hass = _make_hass()
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
    )
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)
    local_dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
    sessions = manager._normalise_sessions_for_day(
        local_dt=local_dt,
        results=[
            {
                "startTime": ["bad"],
                "endTime": "2025-01-01T01:00:00Z",
                "aggEnergyValue": "bad",
                "activeChargeTime": "bad",
                "costCalculated": "Yes",
                "manualOverridden": "no",
            },
            {
                "startTime": "2025-01-01T02:00:00Z",
                "endTime": "2025-01-01T03:00:00Z",
                "aggEnergyValue": 1.0,
                "activeChargeTime": 3600,
                "costCalculated": "True",
            },
        ],
    )
    assert isinstance(sessions, list)


def test_normalise_sessions_rounds_energy_values(monkeypatch):
    hass = _make_hass()
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
    )
    monkeypatch.setattr(sh_mod.dt_util, "as_local", lambda value: value)
    local_dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
    sessions = manager._normalise_sessions_for_day(
        local_dt=local_dt,
        results=[
            {
                "startTime": "2025-01-01T00:00:00Z",
                "endTime": "2025-01-01T01:00:00Z",
                "aggEnergyValue": "1.2349",
                "activeChargeTime": 3600,
            }
        ],
    )
    assert sessions[0]["energy_kwh_total"] == pytest.approx(1.235)


def test_sum_session_energy_skips_invalid_entries():
    hass = _make_hass()
    manager = sh_mod.SessionHistoryManager(
        hass,
        client_getter=lambda: None,
        cache_ttl=60,
    )

    class BadFloat(float):
        def __new__(cls, value):
            return super().__new__(cls, value)

        def __float__(self):
            raise ValueError("boom")

    total = manager._sum_session_energy(
        [
            {"energy_kwh": 1.0},
            {"energy_kwh": BadFloat(2.0)},
        ]
    )
    assert total == 1.0
