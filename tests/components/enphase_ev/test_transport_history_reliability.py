"""Cloud-read correctness under malformed responses, overlap and rate limits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.enphase_ev.api_client import common
from custom_components.enphase_ev.energy import EnergyManager
from custom_components.enphase_ev.session_history import SessionHistoryManager
from custom_components.enphase_ev.session_history_pages import parse_session_timestamp


def _history(client, *, cached=False):
    day = datetime.now(timezone.utc)
    manager = SessionHistoryManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="UTC")),
        client_getter=lambda: client,
        cache_ttl=60,
    )
    previous = [{"session_id": "existing", "energy_kwh": 7.0}]
    if cached:
        manager._cache[("SN", day.strftime("%Y-%m-%d"))] = (
            time.monotonic() - 120,
            previous,
        )
    return manager, day, previous


def _row(day, identity):
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "sessionId": identity,
        "startTime": start.isoformat(),
        "endTime": (start + timedelta(minutes=1)).isoformat(),
        "aggEnergyValue": 1.0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"data": None},
        {"data": {"result": None}},
        {"data": {"result": [None]}},
        {"data": {"result": [], "hasMore": "true"}},
    ],
)
async def test_malformed_history_preserves_valid_cache(payload, cached):
    client = SimpleNamespace(session_history=AsyncMock(return_value=payload))
    manager, day, previous = _history(client, cached=cached)
    result = await manager._async_fetch_sessions_today("SN", day_local=day)
    view = manager.get_cache_view("SN", day_key=day.strftime("%Y-%m-%d"))
    assert result == (previous if cached else [])
    assert view.state == ("stale_reused" if cached else "unavailable")
    assert manager.service_available is False
    assert manager.pagination_diagnostics["outcome"] == "invalid_payload"
    assert manager.pagination_diagnostics["complete"] is False
    diagnostics = manager.pagination_diagnostics
    diagnostics["outcome"] = "caller mutation"
    assert manager.pagination_diagnostics["outcome"] == "invalid_payload"


@pytest.mark.asyncio
async def test_valid_empty_history_clears_previous_sessions():
    client = SimpleNamespace(
        session_history=AsyncMock(return_value={"data": {"result": []}})
    )
    manager, day, _previous = _history(client, cached=True)
    assert await manager._async_fetch_sessions_today("SN", day_local=day) == []
    assert (
        manager.get_cache_view("SN", day_key=day.strftime("%Y-%m-%d")).state == "valid"
    )
    assert manager.pagination_diagnostics["complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_nonempty_history_requires_explicit_completeness(cached):
    client = SimpleNamespace(session_history=AsyncMock())
    manager, day, previous = _history(client, cached=cached)
    client.session_history.return_value = {"data": {"result": [_row(day, "A")]}}
    assert await manager._async_fetch_sessions_today("SN", day_local=day) == (
        previous if cached else []
    )
    assert manager.pagination_diagnostics["outcome"] == "invalid_payload"
    assert manager.service_available is False


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"startTime": "bad"},
        {"startTime": None, "endTime": None},
        {"startTime": float("inf"), "endTime": []},
    ],
)
async def test_unusable_history_rows_cannot_replace_complete_totals(cached, invalid):
    client = SimpleNamespace(session_history=AsyncMock())
    manager, day, previous = _history(client, cached=cached)
    client.session_history.return_value = {
        "data": {"result": [_row(day, "A"), invalid], "hasMore": False}
    }
    assert await manager._async_fetch_sessions_today("SN", day_local=day) == (
        previous if cached else []
    )
    assert manager.pagination_diagnostics["outcome"] == "invalid_rows"
    assert manager.service_available is False


@pytest.mark.asyncio
async def test_valid_outside_day_sessions_allow_authoritative_empty_result():
    client = SimpleNamespace(session_history=AsyncMock())
    manager, day, _previous = _history(client, cached=True)
    client.session_history.return_value = {
        "data": {"result": [_row(day - timedelta(days=1), "A")], "hasMore": False}
    }
    assert await manager._async_fetch_sessions_today("SN", day_local=day) == []
    assert manager.pagination_diagnostics["outcome"] == "success"
    assert manager.service_available is True


@pytest.mark.parametrize(
    "value",
    ["2026-10-01T10:00:00Z[UTC]", "2026-10-01T10:00:00", 1790848800],
)
def test_history_timestamp_formats_share_validation_and_normalization(value):
    assert parse_session_timestamp(value).timestamp() == 1790848800


@pytest.mark.parametrize("value", [1790848800, "2026-10-01T10:00:00Z"])
def test_history_timestamp_timezone_failure_is_invalid(monkeypatch, value):
    from custom_components.enphase_ev import session_history_pages

    def invalid_timezone(_value):
        raise ValueError("invalid timezone")

    monkeypatch.setattr(session_history_pages.dt_util, "as_local", invalid_timezone)
    assert parse_session_timestamp(value) is None


@pytest.mark.asyncio
async def test_overlapping_history_pages_deduplicate_ids_and_continue_short_page():
    client = SimpleNamespace(session_history=AsyncMock())
    manager, day, _previous = _history(client)
    first = _row(day, "A")
    second = _row(day, "B")
    unidentified = _row(day, None)
    client.session_history.side_effect = [
        {"data": {"result": [first, first], "hasMore": True}},
        {"data": {"result": [first, second, unidentified], "hasMore": False}},
    ]
    result = await manager._async_fetch_sessions_today("SN", day_local=day)
    assert len(result) == 3
    assert manager.sum_energy(result) == 3.0
    assert client.session_history.await_args_list[1].kwargs["offset"] == 50
    assert manager.pagination_diagnostics == {
        "pages": 2,
        "unique_rows": 3,
        "duplicates": 2,
        "complete": True,
        "outcome": "success",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["repeated_page", "page_limit", "incomplete_page"])
async def test_incomplete_history_does_not_publish_partial_total(reason):
    client = SimpleNamespace(session_history=AsyncMock())
    manager, day, previous = _history(client, cached=True)
    if reason == "repeated_page":
        response = {"data": {"result": [_row(day, "A")], "hasMore": True}}
        client.session_history.side_effect = [response, response]
    elif reason == "incomplete_page":
        client.session_history.return_value = {"data": {"result": [], "hasMore": True}}
    else:
        client.session_history.side_effect = [
            {"data": {"result": [_row(day, str(i))], "hasMore": True}} for i in range(5)
        ]
    assert await manager._async_fetch_sessions_today("SN", day_local=day) == previous
    assert manager.pagination_diagnostics["outcome"] == reason
    assert manager.pagination_diagnostics["complete"] is False
    assert manager.service_using_stale is True


def _rate_error(header):
    return aiohttp.ClientResponseError(
        SimpleNamespace(real_url="https://example.test"),
        (),
        status=429,
        headers={"Retry-After": header},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("criteria", [False, True])
async def test_history_rate_limit_honors_provider_deadline(criteria, cached):
    client = SimpleNamespace(session_history=AsyncMock())
    error = _rate_error("3600")
    if criteria:
        client.session_history_filter_criteria = AsyncMock(side_effect=error)
    else:
        client.session_history.side_effect = error
    manager, day, previous = _history(client, cached=cached)
    assert await manager._async_fetch_sessions_today("SN", day_local=day) == (
        previous if cached else []
    )
    deadline = manager.service_backoff_ends_utc
    assert (deadline - datetime.now(timezone.utc)).total_seconds() > 3500
    assert manager.service_available is False
    assert await manager._async_fetch_sessions_today("SN", day_local=day) == (
        previous if cached else []
    )
    fetcher = (
        client.session_history_filter_criteria if criteria else client.session_history
    )
    assert fetcher.await_count == 1


def _energy(client):
    return EnergyManager(
        client_provider=lambda: client, site_id="1", logger=logging.getLogger(__name__)
    )


def _energy_payload(source):
    return {
        "consumption": [1000, 2000],
        "last_report_date": source.timestamp(),
        "interval_minutes": 5,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("http_date", [False, True])
async def test_energy_rate_limit_retains_cache_and_skips_later_reads(http_date):
    source = datetime.now(timezone.utc) - timedelta(minutes=5)
    client = SimpleNamespace(
        lifetime_energy=AsyncMock(return_value=_energy_payload(source))
    )
    energy = _energy(client)
    await energy._async_refresh_site_energy()
    previous = dict(energy.site_energy)
    header = (
        format_datetime(datetime.now(timezone.utc) + timedelta(hours=1))
        if http_date
        else "3600"
    )
    client.lifetime_energy.side_effect = _rate_error(header)
    await energy._async_refresh_site_energy(force=True)
    await energy._async_refresh_site_energy(force=True)
    assert energy.site_energy == previous
    assert client.lifetime_energy.await_count == 2
    assert energy.service_available is False
    assert (
        energy.service_backoff_ends_utc - datetime.now(timezone.utc)
    ).total_seconds() > 3500


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_overlapping_energy_reads_coalesce_before_cloud_acquisition(force):
    entered = asyncio.Event()
    release = asyncio.Event()
    source = datetime.now(timezone.utc) - timedelta(minutes=5)

    async def acquire():
        entered.set()
        await release.wait()
        return _energy_payload(source)

    client = SimpleNamespace(lifetime_energy=AsyncMock(side_effect=acquire))
    energy = _energy(client)
    first = asyncio.create_task(energy._async_refresh_site_energy(force=force))
    await asyncio.wait_for(entered.wait(), timeout=1)
    joined = asyncio.create_task(energy._async_refresh_site_energy(force=force))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, joined)
    assert client.lifetime_energy.await_count == 1
    assert energy.site_energy_meta["last_report_date"] == source.replace(microsecond=0)
    # Sequential requests still accept provider corrections (including dates).
    corrected = source - timedelta(minutes=5)
    client.lifetime_energy.side_effect = None
    client.lifetime_energy.return_value = _energy_payload(corrected)
    await energy._async_refresh_site_energy(force=True)
    assert energy.site_energy_meta["last_report_date"] == corrected.replace(
        microsecond=0
    )


@pytest.mark.asyncio
async def test_invalidating_inflight_energy_prevents_superseded_commit():
    entered = asyncio.Event()
    release = asyncio.Event()
    source = datetime.now(timezone.utc) - timedelta(minutes=5)

    async def acquire():
        entered.set()
        await release.wait()
        return _energy_payload(source)

    client = SimpleNamespace(lifetime_energy=AsyncMock(side_effect=acquire))
    energy = _energy(client)
    task = asyncio.create_task(energy._async_refresh_site_energy())
    await asyncio.wait_for(entered.wait(), timeout=1)
    energy._invalidate_site_energy_cache()
    release.set()
    await task
    assert energy.site_energy == {}
    assert energy.site_energy_fetch_diagnostics["last_outcome"] == "superseded"
    await energy._async_refresh_site_energy()
    assert energy.site_energy


@pytest.mark.asyncio
async def test_invalidated_energy_owner_cannot_consume_queued_force_refresh():
    entered = asyncio.Event()
    release = asyncio.Event()
    old_source = datetime.now(timezone.utc) - timedelta(minutes=10)
    new_source = old_source + timedelta(minutes=5)

    async def acquire():
        if client.lifetime_energy.await_count == 1:
            entered.set()
            await release.wait()
            return _energy_payload(old_source)
        return _energy_payload(new_source)

    client = SimpleNamespace(lifetime_energy=AsyncMock(side_effect=acquire))
    energy = _energy(client)
    owner = asyncio.create_task(energy._async_refresh_site_energy())
    await asyncio.wait_for(entered.wait(), timeout=1)
    waiter = asyncio.create_task(energy._async_refresh_site_energy(force=True))
    await asyncio.sleep(0)
    energy._invalidate_site_energy_cache()
    release.set()
    await asyncio.gather(owner, waiter)
    assert client.lifetime_energy.await_count == 2
    assert energy.site_energy_meta["last_report_date"] == new_source.replace(
        microsecond=0
    )
    assert energy.site_energy_fetch_diagnostics["last_outcome"] == "success"


@pytest.mark.asyncio
async def test_anonymous_history_pages_preserve_distinct_row_contents():
    client = SimpleNamespace(session_history=AsyncMock())
    manager, day, _previous = _history(client)
    row = _row(day, None)
    corrected = {**row, "aggEnergyValue": 2.0}
    client.session_history.side_effect = [
        {"data": {"result": [row], "hasMore": True}},
        {"data": {"result": [corrected], "hasMore": False}},
    ]
    result = await manager._async_fetch_sessions_today("SN", day_local=day)
    assert len(result) == 2
    assert manager.sum_energy(result) == 3.0
    assert manager.pagination_diagnostics["outcome"] == "success"


@pytest.mark.asyncio
async def test_cancelled_energy_owner_releases_waiter_without_losing_refresh():
    entered = asyncio.Event()
    never = asyncio.Event()
    source = datetime.now(timezone.utc) - timedelta(minutes=5)

    async def acquire():
        if client.lifetime_energy.await_count == 1:
            entered.set()
            await never.wait()
        return _energy_payload(source)

    client = SimpleNamespace(lifetime_energy=AsyncMock(side_effect=acquire))
    energy = _energy(client)
    first = asyncio.create_task(energy._async_refresh_site_energy())
    await asyncio.wait_for(entered.wait(), timeout=1)
    waiting = asyncio.create_task(energy._async_refresh_site_energy())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(waiting, timeout=1)
    assert client.lifetime_energy.await_count == 2
    assert energy.site_energy


@pytest.mark.asyncio
async def test_history_post_uses_read_budget_while_controls_remain_available(
    monkeypatch,
):
    monkeypatch.setattr(common, "_enlighten_read_semaphore", asyncio.Semaphore(0))
    history = f"{common.BASE_URL}/service/enho_historical_events_ms/site/sessions/serial/history"
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            async with common._enlighten_read_request_guard("POST", history):
                pytest.fail("history bypassed the cloud read limit")
    for method, url in [
        ("POST", f"{common.BASE_URL}/service/control"),
        ("PUT", history),
        ("POST", "https://example.test/history"),
    ]:
        async with asyncio.timeout(1):
            async with common._enlighten_read_request_guard(method, url):
                pass
