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
    # Sequential forced reads still process a correction at the same clock.
    client.lifetime_energy.side_effect = None
    client.lifetime_energy.return_value = {
        **_energy_payload(source),
        "production": [4000],
    }
    await energy._async_refresh_site_energy(force=True)
    assert client.lifetime_energy.await_count == 2
    assert energy.site_energy["solar_production"].value_kwh == 4
    assert energy.site_energy_fetch_diagnostics["source_progress"] == "unchanged"

    # An older report remains visible in diagnostics without replacing flows.
    regressed = source - timedelta(minutes=5)
    client.lifetime_energy.return_value = _energy_payload(regressed)
    await energy._async_refresh_site_energy(force=True)
    assert client.lifetime_energy.await_count == 3
    assert energy.site_energy["solar_production"].value_kwh == 4
    assert energy.site_energy_meta["last_report_date"] == source.replace(microsecond=0)
    diagnostics = energy.site_energy_fetch_diagnostics
    assert diagnostics["source_progress"] == "regressed"
    assert (
        diagnostics["source_timestamp"] == regressed.replace(microsecond=0).isoformat()
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


@pytest.mark.asyncio
async def test_queued_history_criteria_workers_honor_first_rate_limit(monkeypatch):
    from custom_components.enphase_ev import session_history as history

    now = 1000.0
    monkeypatch.setattr(history, "time", SimpleNamespace(monotonic=lambda: now))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def criteria(**kwargs):
        entered.set()
        await release.wait()
        raise _rate_error("1800")

    client = SimpleNamespace(
        session_history_filter_criteria=AsyncMock(side_effect=criteria),
        session_history=AsyncMock(),
    )
    manager, day, previous = _history(client)
    day_key = day.strftime("%Y-%m-%d")
    manager._cache[("EV-B", day_key)] = (now - 120, previous)
    async with asyncio.timeout(2):
        owner = asyncio.create_task(
            manager._async_fetch_sessions_today("EV-A", day_local=day)
        )
        await entered.wait()
        waiters = [
            asyncio.create_task(
                manager._async_fetch_sessions_today(serial, day_local=day)
            )
            for serial in ("EV-B", "EV-C")
        ]
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(owner, *waiters) == [[], previous, []]
    assert (
        manager.service_backoff_ends_utc - manager.service_last_failure_utc
    ).total_seconds() == pytest.approx(1800, abs=1)
    assert manager.service_failures == 1
    assert manager.service_using_stale
    view = manager.get_cache_view("EV-B", day_key)
    assert view.state == "stale_reused"
    assert view.last_error == manager.service_last_error
    assert view.cache_age == 120
    now += 1200
    assert await manager._async_fetch_sessions_today("EV-C", day_local=day) == []
    client.session_history_filter_criteria.assert_awaited_once()
    client.session_history.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_outcome", ["shorter_limit", "success", "more_pages"])
async def test_inflight_history_page_cannot_shorten_cooldown(late_outcome):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(serial, **kwargs):
        if serial == "EV-LATE":
            entered.set()
            await release.wait()
            if late_outcome == "shorter_limit":
                raise _rate_error("60")
            return {
                "data": {
                    "result": [_row(day, "late")],
                    "hasMore": late_outcome == "more_pages",
                }
            }
        raise _rate_error("1800")

    client = SimpleNamespace(session_history=AsyncMock(side_effect=fetch))
    manager, day, _ = _history(client)
    async with asyncio.timeout(2):
        late = asyncio.create_task(
            manager._async_fetch_sessions_today("EV-LATE", day_local=day)
        )
        await entered.wait()
        assert (
            await manager._async_fetch_sessions_today("EV-FIRST", day_local=day) == []
        )
        deadline = manager.service_backoff_ends_utc
        release.set()
        result = await late
    assert len(result) == (1 if late_outcome == "success" else 0)
    assert manager.service_backoff_ends_utc == deadline
    assert manager.service_backoff_active
    assert client.session_history.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_scheduled_and_direct_history_share_the_same_pages(complete):
    entered = asyncio.Event()
    release = asyncio.Event()
    offsets = []

    async def fetch(serial, *, offset, **kwargs):
        offsets.append(offset)
        if offset == 0:
            entered.set()
            await release.wait()
        rows = [_row(day, str(offset))] if offset == 0 or complete else []
        return {"data": {"result": rows, "hasMore": offset == 0 or not complete}}

    manager, day, _ = _history(SimpleNamespace(session_history=fetch))
    manager._hass.async_create_task = asyncio.create_task
    current = {"SN": {}}
    manager._data_supplier = lambda: current
    manager._publish_callback = current.update
    async with asyncio.timeout(2):
        manager.schedule_enrichment(["SN"], day)
        await entered.wait()
        (background,) = manager._enrichment_tasks
        direct = asyncio.create_task(
            manager.async_enrich(["SN"], day, in_background=False, max_cache_age=60)
        )
        await asyncio.sleep(0)
        assert not direct.done()
        release.set()
        direct_result, _ = await asyncio.gather(direct, background)
    assert offsets == [0, 50]
    assert len(direct_result["SN"]) == (2 if complete else 0)
    assert current["SN"]["energy_today_sessions"] == direct_result["SN"]
    assert (
        manager.get_cache_view("SN", day.strftime("%Y-%m-%d")).has_valid_cache
        == complete
    )
    assert not manager._session_reads
    assert not manager._enrichment_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_owner", [True, False])
async def test_session_history_cancellation_isolated_between_readers(cancel_owner):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"data": {"result": [_row(day, "shared")], "hasMore": False}}

    client = SimpleNamespace(session_history=AsyncMock(side_effect=fetch))
    manager, day, _ = _history(client)
    async with asyncio.timeout(2):
        owner = asyncio.create_task(
            manager._async_fetch_sessions_today("SN", day_local=day)
        )
        await entered.wait()
        follower = asyncio.create_task(
            manager._async_fetch_sessions_today("SN", day_local=day)
        )
        await asyncio.sleep(0)
        cancelled, survivor = (owner, follower) if cancel_owner else (follower, owner)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release.set()
        assert len(await survivor) == 1
    assert client.session_history.await_count == (2 if cancel_owner else 1)
    assert not manager._session_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("retire", ["clear", "prune"])
@pytest.mark.parametrize("stage", ["criteria", "page", "page_error"])
async def test_retired_history_discards_late_results(retire, stage):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    async def criteria(**kwargs):
        if stage == "criteria":
            await hold()
        return {}

    async def fetch(*args, **kwargs):
        await hold()
        if stage == "page_error":
            raise _rate_error("1800")
        return {"data": {"result": [_row(day, "late")], "hasMore": False}}

    client = SimpleNamespace(
        session_history=AsyncMock(side_effect=fetch),
        session_history_filter_criteria=AsyncMock(side_effect=criteria),
    )
    manager, day, _ = _history(client)
    async with asyncio.timeout(2):
        owner = asyncio.create_task(
            manager._async_fetch_sessions_today("SN", day_local=day)
        )
        await entered.wait()
        follower = asyncio.create_task(
            manager._async_fetch_sessions_today("SN", day_local=day)
        )
        await asyncio.sleep(0)
        if retire == "clear":
            manager.clear()
        else:
            manager.prune(active_serials=())
        with pytest.raises(asyncio.CancelledError):
            await follower
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
    if stage == "criteria":
        client.session_history.assert_not_awaited()
        assert manager._criteria_checked_mono is None
    assert manager.cache_key_count == 0
    assert manager.service_failures == (retire == "prune" and stage == "page_error")
    assert manager.pagination_diagnostics == {}
    assert not manager._session_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("other_serial", ["SN", "EV-B"])
async def test_independent_session_history_keys_can_complete_in_parallel(other_serial):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(serial, *, start_date, **kwargs):
        if (serial, start_date) == ("SN", day.strftime("%d-%m-%Y")):
            entered.set()
            await release.wait()
        return {"data": {"result": [], "hasMore": False}}

    client = SimpleNamespace(session_history=AsyncMock(side_effect=fetch))
    manager, day, _ = _history(client)
    other_day = day - timedelta(days=1) if other_serial == "SN" else day
    async with asyncio.timeout(2):
        first = asyncio.create_task(
            manager._async_fetch_sessions_today("SN", day_local=day)
        )
        await entered.wait()
        manager.prune()
        assert (
            await manager._async_fetch_sessions_today(other_serial, day_local=other_day)
            == []
        )
        assert not first.done()
        release.set()
        assert await first == []
    assert client.session_history.await_count == 2
    assert not manager._session_reads


@pytest.mark.asyncio
async def test_session_history_clear_discards_completed_result_before_delivery():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(*args, **kwargs):
        entered.set()
        await release.wait()
        asyncio.get_running_loop().call_soon(manager.clear)
        return {"data": {"result": [_row(day, "old")], "hasMore": False}}

    client = SimpleNamespace(session_history=AsyncMock(side_effect=fetch))
    manager, day, _ = _history(client)
    async with asyncio.timeout(2):
        owner = asyncio.create_task(
            manager._async_fetch_sessions_today("SN", day_local=day)
        )
        await entered.wait()
        batch = asyncio.create_task(
            manager.async_enrich(["SN", "EV-B"], day, in_background=False)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        assert len(await owner) == 1
        assert await batch == {}
    assert manager.cache_key_count == 0


@pytest.mark.asyncio
async def test_retired_history_waiting_for_criteria_does_not_dispatch():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def criteria(**kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("criteria unavailable")

    client = SimpleNamespace(
        session_history_filter_criteria=AsyncMock(side_effect=criteria),
        session_history=AsyncMock(),
    )
    manager, day, _ = _history(client)
    async with asyncio.timeout(2):
        retained = asyncio.create_task(
            manager._async_fetch_sessions_today("EV-A", day_local=day)
        )
        await entered.wait()
        retired = asyncio.create_task(
            manager._async_fetch_sessions_today("EV-B", day_local=day)
        )
        await asyncio.sleep(0)
        manager.prune(active_serials=["EV-A"])
        release.set()
        assert await retained == []
        with pytest.raises(asyncio.CancelledError):
            await retired
    client.session_history_filter_criteria.assert_awaited_once()
    client.session_history.assert_not_awaited()
    assert not manager._session_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("retire", ["clear", "prune"])
async def test_retired_queued_history_workers_do_not_dispatch(retire):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(serial, **kwargs):
        if serial == "EV-A":
            entered.set()
            await release.wait()
        return {"data": {"result": [_row(day, "session")], "hasMore": False}}

    client = SimpleNamespace(session_history=AsyncMock(side_effect=fetch))
    manager, day, _ = _history(client)
    manager._concurrency = 1
    async with asyncio.timeout(2):
        batch = asyncio.create_task(
            manager.async_enrich(["EV-A", "EV-B"], day, in_background=False)
        )
        await entered.wait()
        if retire == "clear":
            manager.clear()
        else:
            manager.prune(active_serials=["EV-A"])
        release.set()
        result = await batch

    assert set(result) == (set() if retire == "clear" else {"EV-A"})
    assert [call.args[0] for call in client.session_history.await_args_list] == ["EV-A"]
    assert not manager.get_cache_view("EV-B", day.strftime("%Y-%m-%d")).has_valid_cache
    assert not manager._serial_read_scopes


@pytest.mark.asyncio
async def test_retired_completed_worker_is_excluded_from_background_publication():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(serial, **kwargs):
        if serial == "EV-B":
            entered.set()
            await release.wait()
        return {"data": {"result": [_row(day, serial)], "hasMore": False}}

    client = SimpleNamespace(session_history=AsyncMock(side_effect=fetch))
    manager, day, _ = _history(client)
    manager._hass.async_create_task = asyncio.create_task
    manager._concurrency = 1
    current = {"EV-A": {}, "EV-B": {}}
    manager._data_supplier = lambda: current
    manager._publish_callback = current.update

    async with asyncio.timeout(2):
        manager.schedule_enrichment(["EV-A", "EV-B"], day)
        (task,) = manager._enrichment_tasks
        await entered.wait()
        assert manager.get_cache_view("EV-A", day.strftime("%Y-%m-%d")).has_valid_cache
        del current["EV-A"]
        manager.prune(active_serials=current)
        release.set()
        await task

    assert set(current) == {"EV-B"}
    assert current["EV-B"]["energy_today_sessions"][0]["session_id"] == "EV-B"
    assert manager.in_progress == 0
    assert not manager._serial_read_scopes


@pytest.mark.asyncio
async def test_pruning_and_rescheduling_before_background_worker_starts():
    client = SimpleNamespace(
        session_history=AsyncMock(
            return_value={"data": {"result": [], "hasMore": False}}
        )
    )
    manager, day, _ = _history(client)
    manager._hass.async_create_task = asyncio.create_task
    manager.schedule_enrichment(["", "SN"], day)
    manager.schedule_enrichment(["SN"], day)
    (retired,) = manager._enrichment_tasks
    retired_scope = manager._serial_read_scopes["SN"]
    manager.prune(active_serials=())
    assert manager.in_progress == 0

    manager.schedule_enrichment(["SN"], day)
    (current,) = manager._enrichment_tasks - {retired}
    assert manager._serial_read_scopes["SN"] is not retired_scope
    async with asyncio.timeout(2):
        await asyncio.gather(retired, current)

    client.session_history.assert_awaited_once()
    assert manager.in_progress == 0
    assert not manager._serial_read_scopes


@pytest.mark.asyncio
async def test_background_history_fetches_two_dates_and_publishes_only_selected_day(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev import session_history as history

    first_started, release_first = asyncio.Event(), asyncio.Event()
    first_day = history.dt_util.as_local(
        datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    ).replace(hour=0, minute=0, second=0, microsecond=0)
    second_day = first_day + timedelta(days=1)
    today = first_day
    monkeypatch.setattr(history.dt_util, "now", lambda: today)
    coordinator = coordinator_factory(serials=["SN"])
    manager = coordinator.session_history
    current = {"SN": {"charging": False}}
    manager._data_supplier = lambda: current
    manager._publish_callback = current.update

    async def fetch(serial, *, start_date, **kwargs):
        if start_date == first_day.strftime("%d-%m-%Y"):
            first_started.set()
            await release_first.wait()
            return {"data": {"result": [_row(first_day, "old")], "hasMore": False}}
        assert start_date == second_day.strftime("%d-%m-%Y")
        return {"data": {"result": [_row(second_day, "new")], "hasMore": False}}

    monkeypatch.setattr(
        coordinator.client,
        "session_history_filter_criteria",
        AsyncMock(return_value={}),
    )
    fetcher = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(coordinator.client, "session_history", fetcher)

    async with asyncio.timeout(5):
        manager.schedule_enrichment(["SN"], first_day)
        (old_task,) = manager._enrichment_tasks
        try:
            await first_started.wait()
            today = second_day
            manager.schedule_enrichment(["SN"], second_day)
            (new_task,) = manager._enrichment_tasks - {old_task}
            await new_task
            assert current["SN"]["energy_today_sessions"][0]["session_id"] == "new"
        finally:
            release_first.set()
            await old_task

    assert current["SN"]["energy_today_sessions"][0]["session_id"] == "new"
    assert (
        manager.get_cache_view("SN", first_day.strftime("%Y-%m-%d")).sessions[0][
            "session_id"
        ]
        == "old"
    )
    assert fetcher.await_count == 2
    assert manager.in_progress == 0
