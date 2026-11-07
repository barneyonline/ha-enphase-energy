"""Offline regressions for retry pressure, shared reads and bounded retention."""

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from custom_components.enphase_ev import cloud_retry, energy as energy_mod
from custom_components.enphase_ev import (
    evse_timeseries as ts_mod,
    summary as summary_mod,
)
from custom_components.enphase_ev.evse_timeseries import EVSETimeseriesManager
from custom_components.enphase_ev.summary import SummaryStore

DAY = datetime(2026, 1, 1, tzinfo=timezone.utc)
PAYLOAD = {"EV": {"energy_kwh": 2.0}}


def clock(monkeypatch):
    now = SimpleNamespace(value=1000.0)
    for module in (energy_mod, ts_mod, summary_mod):
        monkeypatch.setattr(
            module, "time", SimpleNamespace(monotonic=lambda: now.value)
        )
    monkeypatch.setattr(cloud_retry.random, "uniform", lambda low, high: low)
    return now


def http_error(header=None, *, status=429):
    return aiohttp.ClientResponseError(
        SimpleNamespace(real_url="https://example.test/mock"),
        (),
        status=status,
        headers={"Retry-After": header} if header is not None else {},
    )


@pytest.mark.parametrize("jitter", [1.0, 1.1])
def test_transient_backoff_bounds(monkeypatch, jitter):
    monkeypatch.setattr(cloud_retry.random, "uniform", lambda low, high: jitter)
    assert [
        cloud_retry.transient_backoff_delay(n) for n in range(1, 8)
    ] == pytest.approx(
        [min(900, delay * jitter) for delay in (60, 120, 240, 480, 960, 960, 960)]
    )
    assert cloud_retry.transient_backoff_delay(0) == 60 * jitter


async def test_energy_timeout_backoff_recovery_preserves_cache(
    coordinator_factory, monkeypatch
):
    now = clock(monkeypatch)
    coord = coordinator_factory()
    client = SimpleNamespace(
        lifetime_energy=AsyncMock(
            return_value={"consumption": [1000], "last_report_date": DAY.timestamp()}
        )
    )
    energy = coord.energy
    energy._client_provider = lambda: client
    await energy._async_refresh_site_energy()
    accepted, source = energy.site_energy, energy.site_energy_meta
    client.lifetime_energy.side_effect = TimeoutError()
    for delay in (60, 120, 240, 480, 900, 900):
        now.value += 1000
        await energy._async_refresh_site_energy(force=True)
        assert energy._service_backoff_until == now.value + delay
        assert energy.site_energy is accepted
        assert energy.site_energy_meta == source
        calls = client.lifetime_energy.await_count
        await energy._async_refresh_site_energy(force=True)
        assert client.lifetime_energy.await_count == calls
        assert not energy.site_energy_refresh_due(force=True)
    now.value += 1000
    client.lifetime_energy.side_effect = None
    await energy._async_refresh_site_energy(force=True)
    assert energy.service_available
    assert energy._transient_failures == 0
    client.lifetime_energy.side_effect = RuntimeError("synthetic")
    await energy._async_refresh_site_energy(force=True)
    assert energy._service_backoff_until == now.value + 60


@pytest.mark.parametrize("endpoint", ["daily", "lifetime"])
async def test_timeseries_independent_transient_backoff(hass, monkeypatch, endpoint):
    now = clock(monkeypatch)
    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(return_value=PAYLOAD),
        evse_timeseries_lifetime_energy=AsyncMock(return_value=PAYLOAD),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    await manager.async_refresh(day_local=DAY)
    fetch = getattr(client, f"evse_timeseries_{endpoint}_energy")
    fetch.side_effect = TimeoutError()
    for delay in (60, 120, 240, 480, 900, 900):
        now.value += 1000
        await manager.async_refresh(day_local=DAY, force=True)
        assert manager._endpoint_state[endpoint]["backoff_until"] == now.value + delay
        assert manager._endpoint_state[endpoint]["using_stale"]
        calls = fetch.await_count
        await manager.async_refresh(day_local=DAY, force=True)
        assert fetch.await_count == calls
    fetch.side_effect = None
    now.value += 1000
    await manager.async_refresh(day_local=DAY, force=True)
    assert manager._transient_failures[endpoint] == 0
    assert manager.daily_available and manager.lifetime_available
    assert manager._acquisitions == {}


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("header", [None, "invalid", "7200", "date"])
async def test_hems_fallback_honors_deadline(
    coordinator_factory, monkeypatch, wrapped, header
):
    now = clock(monkeypatch)
    utc = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(energy_mod.dt_util, "utcnow", lambda: utc)
    if header == "date":
        header = format_datetime(utc + timedelta(hours=2), usegmt=True)
    error = http_error(header)
    if wrapped:
        cause = error
        error = RuntimeError("wrapped")
        error.__cause__ = cause
    coord = coordinator_factory()
    energy = coord.energy
    client = SimpleNamespace(
        lifetime_energy=AsyncMock(
            return_value={
                "production": [1000],
                "evse": [],
                "heatpump": [],
                "water_heater": [],
            }
        ),
        hems_consumption_lifetime=AsyncMock(side_effect=error),
    )
    energy._client_provider = lambda: client
    await energy._async_refresh_site_energy(force=True)
    delay = 7200 if header not in (None, "invalid") else 3600
    assert energy._hems_lifetime_backoff_until == now.value + delay
    now.value += delay - 1
    await energy._async_refresh_site_energy(force=True)
    assert client.hems_consumption_lifetime.await_count == 1
    now.value += 2
    client.hems_consumption_lifetime.side_effect = None
    client.hems_consumption_lifetime.return_value = {"evse": [100]}
    await energy._async_refresh_site_energy(force=True)
    assert client.hems_consumption_lifetime.await_count == 2
    assert energy._hems_lifetime_backoff_until is None


def test_existing_deadlines_never_shorten(coordinator_factory, monkeypatch, hass):
    now = clock(monkeypatch)
    energy = coordinator_factory().energy
    energy._hems_lifetime_backoff_until = now.value + 10000
    energy._note_hems_lifetime_unavailable("synthetic")
    assert energy._hems_lifetime_backoff_until == now.value + 10000
    energy._service_backoff_until = now.value + 10000
    energy._note_service_unavailable(TimeoutError(), transient=True)
    assert energy._service_backoff_until == now.value + 10000
    manager = EVSETimeseriesManager(hass, lambda: None)
    manager._note_service_unavailable("daily", http_error("7200"))
    assert manager._endpoint_state["daily"]["backoff_until"] == now.value + 7200
    manager._note_service_unavailable("daily", "synthetic", transient=True)
    assert manager._endpoint_state["daily"]["backoff_until"] == now.value + 7200
    assert not manager.refresh_due(force=True)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("different_days", [False, True])
async def test_timeseries_overlap_and_sequential_force(hass, force, different_days):
    entered, release = asyncio.Event(), asyncio.Event()

    async def lifetime():
        entered.set()
        await release.wait()
        return PAYLOAD

    client = SimpleNamespace(
        evse_timeseries_lifetime_energy=AsyncMock(side_effect=lifetime),
        evse_timeseries_daily_energy=AsyncMock(return_value=PAYLOAD),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    first = asyncio.create_task(manager.async_refresh(day_local=DAY, force=force))
    await entered.wait()
    second_day = DAY + timedelta(days=int(different_days))
    second = asyncio.create_task(
        manager.async_refresh(day_local=second_day, force=force)
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert client.evse_timeseries_lifetime_energy.await_count == 1
    assert client.evse_timeseries_daily_energy.await_count == 1 + different_days
    assert manager._acquisitions == {}
    await manager.async_refresh(day_local=DAY, force=True)
    assert client.evse_timeseries_lifetime_energy.await_count == 2
    assert client.evse_timeseries_daily_energy.await_count == 2 + different_days


@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_timeseries_cancellation_releases_callers(hass, cancel_owner):
    entered, release = asyncio.Event(), asyncio.Event()

    async def lifetime():
        entered.set()
        await release.wait()
        return PAYLOAD

    client = SimpleNamespace(
        evse_timeseries_lifetime_energy=AsyncMock(side_effect=lifetime),
        evse_timeseries_daily_energy=AsyncMock(return_value=PAYLOAD),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    first = asyncio.create_task(manager.async_refresh(day_local=DAY))
    await entered.wait()
    second = asyncio.create_task(manager.async_refresh(day_local=DAY))
    await asyncio.sleep(0)
    cancelled, survivor = (first, second) if cancel_owner else (second, first)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    await survivor
    assert manager.lifetime_entry("EV") == PAYLOAD["EV"]
    assert manager.service_failures == 0
    assert manager._acquisitions == {}


@pytest.mark.parametrize("operation", ["clear", "stop", "prune"])
@pytest.mark.parametrize("failed", [False, True])
async def test_timeseries_rejects_invalidated_results(hass, operation, failed):
    entered, release = asyncio.Event(), asyncio.Event()

    async def daily(**kwargs):
        entered.set()
        await release.wait()
        if failed:
            raise RuntimeError("late failure")
        return PAYLOAD

    client = SimpleNamespace(evse_timeseries_daily_energy=AsyncMock(side_effect=daily))
    manager = EVSETimeseriesManager(hass, lambda: client)
    manager.prune(active_serials={"EV"})
    first = asyncio.create_task(manager.async_refresh(day_local=DAY))
    await entered.wait()
    if operation == "prune":
        manager.prune(active_serials=set())
    else:
        getattr(manager, operation)()
    release.set()
    await first
    assert manager.daily_entry("EV", day_local=DAY) is None
    assert manager.service_failures == 0
    assert manager._acquisitions == {}


@pytest.mark.parametrize("zone", ["UTC", "Australia/Melbourne"])
async def test_daily_retention_is_bounded_across_calendar_changes(hass, zone):
    day = DAY.astimezone(ZoneInfo(zone))

    async def daily(*, start_date):
        return {
            f"EV-{n}": {
                "energy_kwh": float(n),
                "last_report_date": start_date.isoformat(),
            }
            for n in range(32)
        }

    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(side_effect=daily),
        evse_timeseries_lifetime_energy=AsyncMock(
            return_value={"EV-0": {"energy_kwh": 5.0}, "retired": {"energy_kwh": 1.0}}
        ),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    for offset in range(730):
        await manager.async_refresh(day_local=day + timedelta(days=offset))
        assert len(manager.daily_cache_days) <= 3
        assert not manager._acquisitions
    expected = [(day + timedelta(days=n)).strftime("%Y-%m-%d") for n in (727, 728, 729)]
    assert manager.daily_cache_days == expected
    await manager.async_refresh(day_local=day, force=True)
    assert manager.daily_cache_days == expected
    manager.prune(active_serials={"EV-0"})
    assert all(set(payload) == {"EV-0"} for _, payload in manager._daily_cache.values())
    assert manager.lifetime_entry("retired") is None
    manager.stop()
    assert manager._daily_cache == {}
    assert manager._lifetime_cache is None
    assert not manager.refresh_due(force=True)


@pytest.mark.parametrize("force", [False, True])
async def test_summary_overlap_coalesces_and_sequential_force_fetches(force):
    entered, release = asyncio.Event(), asyncio.Event()

    async def summary():
        entered.set()
        await release.wait()
        return [{"serialNumber": "EV"}]

    client = SimpleNamespace(summary_v2=AsyncMock(side_effect=summary))
    store = SummaryStore(lambda: client)
    first = asyncio.create_task(store.async_fetch(force=force))
    await entered.wait()
    second = asyncio.create_task(store.async_fetch(force=force))
    await asyncio.sleep(0)
    release.set()
    assert await first == await second == [{"serialNumber": "EV"}]
    assert client.summary_v2.await_count == 1
    await store.async_fetch(force=True)
    assert client.summary_v2.await_count == 2


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("new_cache", [False, True])
async def test_summary_invalidation_rejects_late_result(failed, new_cache):
    entered, release = asyncio.Event(), asyncio.Event()

    async def summary():
        entered.set()
        await release.wait()
        if failed:
            raise RuntimeError("late failure")
        return [{"serialNumber": "old"}]

    client = SimpleNamespace(summary_v2=AsyncMock(side_effect=summary))
    store = SummaryStore(lambda: client)
    task = asyncio.create_task(store.async_fetch(force=True))
    await entered.wait()
    store.invalidate()
    if new_cache:
        store._cache = (0, [{"serialNumber": "new"}], 600)
    release.set()
    assert await task == ([{"serialNumber": "new"}] if new_cache else [])
    assert store.diagnostics()["failures"] == 0


async def test_coordinator_prunes_and_retires_shared_caches(coordinator_factory):
    coord = coordinator_factory(serials=["EV"])
    coord.evse_timeseries._daily_cache["2026-01-01"] = (
        0,
        {**PAYLOAD, "retired": {"energy_kwh": 1.0}},
    )
    coord.evse_timeseries._lifetime_cache = (
        0,
        {**PAYLOAD, "retired": {"energy_kwh": 1.0}},
    )
    coord._prune_runtime_caches(active_serials=["EV"])
    assert coord.evse_timeseries.lifetime_entry("retired") is None
    await coord.async_cleanup_runtime_state()
    assert coord.evse_timeseries._stopped
    assert coord.evse_timeseries.daily_cache_days == []


async def test_queued_daily_read_rechecks_cooldown(hass):
    entered, release = asyncio.Event(), asyncio.Event()

    async def lifetime():
        entered.set()
        await release.wait()
        return PAYLOAD

    client = SimpleNamespace(
        evse_timeseries_lifetime_energy=AsyncMock(side_effect=lifetime),
        evse_timeseries_daily_energy=AsyncMock(return_value=PAYLOAD),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    task = asyncio.create_task(manager.async_refresh(day_local=DAY, force=True))
    await entered.wait()
    manager._note_service_unavailable(
        "daily", "cooldown started while lifetime was pending"
    )
    release.set()
    await task
    client.evse_timeseries_daily_energy.assert_not_awaited()
    assert manager._acquisitions == {}


@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_summary_cancellation_releases_waiter(cancel_owner):
    entered, release = asyncio.Event(), asyncio.Event()

    async def summary():
        entered.set()
        await release.wait()
        return []

    client = SimpleNamespace(summary_v2=AsyncMock(side_effect=summary))
    store = SummaryStore(lambda: client)
    first = asyncio.create_task(store.async_fetch(force=True))
    await entered.wait()
    second = asyncio.create_task(store.async_fetch(force=True))
    await asyncio.sleep(0)
    cancelled, survivor = (first, second) if cancel_owner else (second, first)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    assert await survivor == []
    assert store.diagnostics()["failures"] == 0


@pytest.mark.parametrize("kind", ["summary", "timeseries"])
async def test_invalidating_owner_preserves_queued_force(kind, hass):
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def acquire(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await release.wait()
        return [] if kind == "summary" else PAYLOAD

    if kind == "summary":
        store = SummaryStore(lambda: SimpleNamespace(summary_v2=acquire))

        def read():
            return store.async_fetch(force=True)

        invalidate = store.invalidate
    else:
        store = EVSETimeseriesManager(
            hass, lambda: SimpleNamespace(evse_timeseries_daily_energy=acquire)
        )

        def read():
            return store.async_refresh(day_local=DAY, force=True)

        invalidate = store.clear
    first = asyncio.create_task(read())
    await entered.wait()
    second = asyncio.create_task(read())
    await asyncio.sleep(0)
    invalidate()
    release.set()
    await asyncio.gather(first, second)
    assert attempts == 2


async def test_warmup_and_poll_share_timeseries_acquisition(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.refresh_plan import (
        build_post_session_followup_plan,
    )

    coord = coordinator_factory(serials=["EV"])
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(ts_mod.dt_util, "now", lambda: DAY)
    monkeypatch.setattr(ts_mod.dt_util, "as_local", lambda value: value)

    async def lifetime():
        entered.set()
        await release.wait()
        return PAYLOAD

    coord.client.evse_timeseries_lifetime_energy = AsyncMock(side_effect=lifetime)
    coord.client.evse_timeseries_daily_energy = AsyncMock(return_value=PAYLOAD)
    monkeypatch.setattr(coord.energy, "site_energy_refresh_due", lambda: False)
    monkeypatch.setattr(coord.inventory_runtime, "inverters_refresh_due", lambda: False)
    working = {"EV": {}}
    warmup = asyncio.create_task(
        coord.refresh_runner.async_refresh_evse_timeseries_for_warmup(
            working_data=working
        )
    )
    await entered.wait()
    plan = build_post_session_followup_plan(coord, DAY)
    poll = asyncio.create_task(
        coord.refresh_runner.async_run_refresh_plan({}, plan=plan)
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(warmup, poll)
    coord.client.evse_timeseries_lifetime_energy.assert_awaited_once()
    coord.client.evse_timeseries_daily_energy.assert_awaited_once_with(start_date=DAY)
    assert working["EV"]["evse_daily_energy_kwh"] == 2.0
    assert coord.evse_timeseries._acquisitions == {}


@pytest.mark.parametrize("outage", [False, True])
async def test_invalidated_energy_failure_cannot_block_queued_force(
    coordinator_factory, outage
):
    from custom_components.enphase_ev.api import SiteEnergyUnavailable

    coord = coordinator_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def acquire():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await release.wait()
            raise SiteEnergyUnavailable("obsolete outage") if outage else TimeoutError()
        return {"consumption": [1000], "last_report_date": DAY.timestamp()}

    coord.energy._client_provider = lambda: SimpleNamespace(lifetime_energy=acquire)
    owner = asyncio.create_task(coord.energy._async_refresh_site_energy())
    await entered.wait()
    waiter = asyncio.create_task(coord.energy._async_refresh_site_energy(force=True))
    await asyncio.sleep(0)
    coord.energy._invalidate_site_energy_cache()
    release.set()
    await asyncio.gather(owner, waiter)
    assert attempts == 2
    assert coord.energy.site_energy_fetch_diagnostics["last_outcome"] == "success"
    assert coord.energy.site_energy_fetch_diagnostics["failure_count"] == 0
    assert coord.energy.service_available


@pytest.mark.parametrize("failed", [False, True])
async def test_stopped_summary_rejects_late_and_queued_readers(failed):
    entered, release = asyncio.Event(), asyncio.Event()

    async def acquire():
        entered.set()
        await release.wait()
        if failed:
            raise TimeoutError()
        return [{"serialNumber": "old"}]

    client = SimpleNamespace(summary_v2=AsyncMock(side_effect=acquire))
    store = SummaryStore(lambda: client)
    owner = asyncio.create_task(store.async_fetch(force=True))
    await entered.wait()
    waiter = asyncio.create_task(store.async_fetch(force=True))
    await asyncio.sleep(0)
    store.stop()
    release.set()
    assert await owner == await waiter == []
    assert await store.async_fetch(force=True) == []
    client.summary_v2.assert_awaited_once()
    assert store._cache is None
    assert store.diagnostics()["failures"] == 0


async def test_retirement_rejects_inflight_timeseries(coordinator_factory):
    coord = coordinator_factory(serials=["EV"])
    entered, release = asyncio.Event(), asyncio.Event()

    async def daily(**kwargs):
        entered.set()
        await release.wait()
        return PAYLOAD

    coord.client.evse_timeseries_lifetime_energy = AsyncMock(return_value=PAYLOAD)
    coord.client.evse_timeseries_daily_energy = AsyncMock(side_effect=daily)
    task = asyncio.create_task(coord.evse_timeseries.async_refresh(day_local=DAY))
    await entered.wait()
    coord.mark_runtime_stopped()
    release.set()
    await task
    assert coord.evse_timeseries._daily_cache == {}
    assert coord.evse_timeseries._lifetime_cache is None
    assert coord.evse_timeseries._acquisitions == {}


def test_retirement_handles_partially_constructed_read_managers(coordinator_factory):
    coord = coordinator_factory()
    coord.summary = SimpleNamespace()
    coord.evse_timeseries = None
    coord.mark_runtime_stopped()
    assert not coord.runtime_active


@pytest.mark.parametrize("kind", ["summary", "timeseries"])
async def test_invalidation_after_completion_does_not_suppress_queued_force(kind, hass):
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def acquire(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await release.wait()
            # Run after the owner commits but before its queued reader resumes.
            asyncio.get_running_loop().call_soon(invalidate)
        return [] if kind == "summary" else PAYLOAD

    if kind == "summary":
        store = SummaryStore(lambda: SimpleNamespace(summary_v2=acquire))

        def read():
            return store.async_fetch(force=True)

        invalidate = store.invalidate
    else:
        store = EVSETimeseriesManager(
            hass, lambda: SimpleNamespace(evse_timeseries_daily_energy=acquire)
        )

        def read():
            return store.async_refresh(day_local=DAY, force=True)

        invalidate = store.clear
    first = asyncio.create_task(read())
    await entered.wait()
    second = asyncio.create_task(read())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert attempts == 2
