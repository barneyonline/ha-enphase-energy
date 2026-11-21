"""Offline regressions for retry pressure, shared reads and bounded retention."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
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


@pytest.mark.parametrize("force_recovery", [False, True])
async def test_coordinator_throttles_shared_no_flow_acquisition(
    coordinator_factory, monkeypatch, force_recovery
):
    from custom_components.enphase_ev.refresh_plan import (
        build_post_session_followup_plan,
    )

    now = clock(monkeypatch)
    coordinator = coordinator_factory()
    energy = coordinator.energy
    started, release = asyncio.Event(), asyncio.Event()

    async def acquire():
        if fetch.await_count == 2:
            started.set()
            await release.wait()
            return {"consumption": [None]}
        return {"consumption": [1000 * fetch.await_count]}

    fetch = AsyncMock(side_effect=acquire)
    energy._client_provider = lambda: SimpleNamespace(lifetime_energy=fetch)
    monkeypatch.setattr(coordinator.evse_timeseries, "refresh_due", lambda **_: False)
    monkeypatch.setattr(
        coordinator.inventory_runtime, "inverters_refresh_due", lambda: False
    )

    async def poll():
        plan = build_post_session_followup_plan(coordinator, DAY)
        await coordinator.refresh_runner.async_run_refresh_plan({}, plan=plan)

    await poll()
    baseline = energy.site_energy
    last_success = energy.site_energy_fetch_diagnostics["last_success_utc"]
    now.value += energy_mod.SITE_ENERGY_CACHE_TTL
    owner = asyncio.create_task(poll())
    await asyncio.wait_for(started.wait(), timeout=1)
    waiters = [
        asyncio.create_task(energy._async_refresh_site_energy(force=force))
        for force in (False, True)
    ]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(owner, *waiters)
    assert fetch.await_count == 2
    assert energy.site_energy_fetch_diagnostics["last_success_utc"] == last_success
    assert energy.site_energy_fetch_diagnostics["last_outcome"] == "invalid_payload"

    now.value += energy_mod.SITE_ENERGY_CACHE_TTL - 1
    await poll()
    assert fetch.await_count == 2
    assert energy.site_energy is baseline

    if force_recovery:
        await energy._async_refresh_site_energy(force=True)
    else:
        now.value += 1
        await poll()
    assert fetch.await_count == 3
    assert energy.site_energy["consumption"].value_kwh == 3
    assert energy.site_energy_cache_age == 0


async def test_invalidating_completed_empty_energy_read_releases_queued_force(
    coordinator_factory,
):
    energy = coordinator_factory().energy
    entered, release = asyncio.Event(), asyncio.Event()

    async def acquire():
        if acquire_mock.await_count == 1:
            entered.set()
            await release.wait()
            # The owner completes this read before the queued forced call resumes.
            asyncio.get_running_loop().call_soon(energy._invalidate_site_energy_cache)
            return {"consumption": []}
        return {"consumption": [3000]}

    acquire_mock = AsyncMock(side_effect=acquire)
    energy._client_provider = lambda: SimpleNamespace(lifetime_energy=acquire_mock)
    owner = asyncio.create_task(energy._async_refresh_site_energy())
    await asyncio.wait_for(entered.wait(), timeout=1)
    waiter = asyncio.create_task(energy._async_refresh_site_energy(force=True))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(owner, waiter)

    assert acquire_mock.await_count == 2
    assert energy.site_energy["consumption"].value_kwh == 3


@pytest.mark.parametrize(
    "superseded", ["primary", "hems_success", "hems_timeout", "hems_skip"]
)
async def test_superseded_energy_read_leaves_hems_available_for_queued_force(
    coordinator_factory,
    superseded,
):
    energy = coordinator_factory().energy
    entered, release = asyncio.Event(), asyncio.Event()
    auth_events = []
    energy._hems_auth_success = auth_events.append

    def auth_failure(_error, endpoint):
        auth_events.append(("failure", endpoint))
        return False

    energy._hems_auth_failure = auth_failure

    def auth_skip(_endpoint):
        if superseded == "hems_skip" and primary_mock.await_count == 1:
            energy._invalidate_site_energy_cache()
            return True
        return False

    energy._hems_auth_skip = auth_skip

    async def primary():
        if superseded in {"primary", "hems_skip"} and primary_mock.await_count == 1:
            entered.set()
            await release.wait()
        return {"consumption": [3000]}

    async def hems():
        if (
            superseded in {"hems_success", "hems_timeout"}
            and hems_mock.await_count == 1
        ):
            entered.set()
            await release.wait()
            if superseded == "hems_timeout":
                raise TimeoutError
        return {"heatpump": [2000]}

    primary_mock = AsyncMock(side_effect=primary)
    hems_mock = AsyncMock(side_effect=hems)
    energy._client_provider = lambda: SimpleNamespace(
        lifetime_energy=primary_mock, hems_consumption_lifetime=hems_mock
    )
    owner = asyncio.create_task(energy._async_refresh_site_energy())
    await asyncio.wait_for(entered.wait(), timeout=1)
    waiter = asyncio.create_task(energy._async_refresh_site_energy(force=True))
    await asyncio.sleep(0)
    if superseded != "hems_skip":
        energy._invalidate_site_energy_cache()
    release.set()
    await asyncio.gather(owner, waiter)

    assert primary_mock.await_count == 2
    assert hems_mock.await_count == (
        2 if superseded in {"hems_success", "hems_timeout"} else 1
    )
    assert auth_events == ["hems_consumption_lifetime"]
    assert energy.site_energy["heat_pump"].value_kwh == 2
    assert energy.site_energy_fetch_diagnostics["last_outcome"] == "success"


@pytest.mark.parametrize("reset_confirmation", [False, True])
async def test_cancelled_site_energy_read_does_not_consume_queued_refresh(
    coordinator_factory, monkeypatch, reset_confirmation
):
    clock(monkeypatch)
    energy = coordinator_factory().energy
    entered = asyncio.Event()
    attempts_before_block = 1 + int(reset_confirmation)

    async def acquire():
        if fetch.await_count == 1:
            return {"production": [10_000]}
        if fetch.await_count == attempts_before_block + 1:
            entered.set()
            await asyncio.Future()
        return {"production": [1000 if reset_confirmation else 20_000]}

    fetch = AsyncMock(side_effect=acquire)
    energy._client_provider = lambda: SimpleNamespace(lifetime_energy=fetch)
    await energy._async_refresh_site_energy()
    if reset_confirmation:
        await energy._async_refresh_site_energy(force=True)
        assert energy.site_energy["solar_production"].value_kwh == 10
        assert energy.site_energy_refresh_due()

    owner = asyncio.create_task(
        energy._async_refresh_site_energy(force=not reset_confirmation)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    ordinary = asyncio.create_task(energy._async_refresh_site_energy())
    queued = asyncio.create_task(
        energy._async_refresh_site_energy(force=not reset_confirmation)
    )
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    await asyncio.gather(ordinary, queued)

    assert fetch.await_count == attempts_before_block + 2
    assert energy.site_energy["solar_production"].value_kwh == (
        1 if reset_confirmation else 20
    )
    assert energy.site_energy_fetch_diagnostics["failure_count"] == 0


@pytest.mark.parametrize("obsolete_supported", [False, True])
async def test_superseded_hems_read_preserves_shared_client_support(
    coordinator_factory, monkeypatch, obsolete_supported
):
    coord = coordinator_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    primary_support = []
    hems_attempts = 0

    async def request(method, url, **kwargs):
        nonlocal hems_attempts
        if url.endswith("/lifetime_energy"):
            primary_support.append(coord.client.hems_site_supported)
            return {"consumption": [3000]}
        assert url.endswith("/hems_consumption_lifetime")
        hems_attempts += 1
        if hems_attempts == 1:
            entered.set()
            await release.wait()
            if not obsolete_supported:
                raise aiohttp.ClientResponseError(
                    SimpleNamespace(real_url=url),
                    (),
                    status=550,
                    message="INVALID_SITE",
                )
            return {"heatpump": [1000]}
        return {"heatpump": [2000]}

    monkeypatch.setattr(coord.client, "_json", request)
    owner = asyncio.create_task(coord.energy._async_refresh_site_energy())
    await asyncio.wait_for(entered.wait(), timeout=1)
    waiter = asyncio.create_task(coord.energy._async_refresh_site_energy(force=True))
    await asyncio.sleep(0)
    coord.energy._invalidate_site_energy_cache()
    release.set()
    await asyncio.gather(owner, waiter)

    assert primary_support == [None, None]
    assert hems_attempts == 2
    assert coord.client.hems_site_supported is True
    assert coord.energy.site_energy["heat_pump"].value_kwh == 2


@pytest.mark.parametrize("phase", ["primary", "hems"])
async def test_retired_entry_does_not_finish_or_restart_energy_acquisition(
    coordinator_factory, monkeypatch, phase
):
    coord = coordinator_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request(method, url, **kwargs):
        if url.endswith("/lifetime_energy"):
            calls.append("primary")
            payload = {"consumption": [3000]}
        else:
            assert url.endswith("/hems_consumption_lifetime")
            calls.append("hems")
            payload = {"heatpump": [2000]}
        if calls[-1] == phase:
            entered.set()
            await release.wait()
        return payload

    monkeypatch.setattr(coord.client, "_json", request)
    owner = asyncio.create_task(coord.energy._async_refresh_site_energy())
    await asyncio.wait_for(entered.wait(), timeout=1)
    queued = asyncio.create_task(coord.energy._async_refresh_site_energy(force=True))
    await asyncio.sleep(0)
    coord.mark_runtime_stopped()
    release.set()
    await asyncio.gather(owner, queued)
    await coord.energy._async_refresh_site_energy(force=True)

    assert calls == (["primary"] if phase == "primary" else ["primary", "hems"])
    assert not coord.energy.site_energy_refresh_due(force=True)
    assert coord.energy.site_energy == {}
    assert coord.energy.site_energy_fetch_diagnostics["last_outcome"] == "superseded"
    assert coord.client.hems_site_supported is None

    replacement = coordinator_factory()
    monkeypatch.setattr(
        replacement.client,
        "lifetime_energy",
        AsyncMock(
            return_value={
                "consumption": [4000],
                "evse": [100],
                "heatpump": [100],
                "water_heater": [100],
            }
        ),
    )
    await replacement.energy._async_refresh_site_energy()
    assert replacement.energy.site_energy["consumption"].value_kwh == 4


@pytest.mark.parametrize(
    ("phase", "expected_calls"),
    [
        ("queued_primary", []),
        ("queued_hems", ["lifetime_energy"]),
        ("late_401", ["lifetime_energy"]),
        ("late_login_wall", ["lifetime_energy"]),
        ("late_success", ["lifetime_energy"]),
        ("queued_retry", ["lifetime_energy"]),
    ],
)
async def test_retired_lifetime_transport_stops_dispatch_and_reauth(
    coordinator_factory, monkeypatch, phase, expected_calls
):
    from custom_components.enphase_ev.api_client import request_surface

    coord = coordinator_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def pause():
        entered.set()
        await release.wait()

    @asynccontextmanager
    async def read_guard(_method, url):
        if (
            (phase == "queued_primary" and url.endswith("/lifetime_energy"))
            or (phase == "queued_hems" and url.endswith("/hems_consumption_lifetime"))
            or (phase == "queued_retry" and calls)
        ):
            await pause()
        yield

    async def json_body():
        if phase == "late_login_wall":
            raise ValueError("invalid JSON")
        if phase == "late_success":
            await pause()
        return {"consumption": [3000]}

    async def login_wall():
        await pause()
        return "<html><form action='/login/login'><input type='password'></form></html>"

    @asynccontextmanager
    async def request(_method, url, **_kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        if phase == "late_401" and len(calls) == 1:
            await pause()
        status = (
            401 if phase in {"late_401", "queued_retry"} and len(calls) == 1 else 200
        )
        yield SimpleNamespace(
            status=status, headers={}, json=json_body, text=login_wall
        )

    @asynccontextmanager
    async def request_session(**_kwargs):
        yield SimpleNamespace(request=request)

    reauth = AsyncMock(return_value=True)
    coord.client.set_reauth_callback(reauth)
    monkeypatch.setattr(
        request_surface, "_timed_enlighten_read_request_guard", read_guard
    )
    monkeypatch.setattr(coord.client, "_request_session", request_session)
    mark_healthy = Mock()
    monkeypatch.setattr(coord.client, "_mark_payload_healthy", mark_healthy)

    refresh = asyncio.create_task(coord.energy._async_refresh_site_energy())
    await asyncio.wait_for(entered.wait(), timeout=1)
    coord.mark_runtime_stopped()
    release.set()
    await refresh

    assert calls == expected_calls
    assert reauth.await_count == (1 if phase == "queued_retry" else 0)
    assert mark_healthy.call_count == (1 if phase == "queued_hems" else 0)
    assert coord.energy.site_energy == {}


@pytest.mark.parametrize(
    "login_outcome", ["success", "rejected", "cancelled", "invalidated"]
)
async def test_lifetime_reauth_follows_shared_runtime_lifecycle(
    coordinator_factory, monkeypatch, login_outcome
):
    from custom_components.enphase_ev import auth_refresh_runtime
    from custom_components.enphase_ev.api_models import AuthTokens

    coord = coordinator_factory()
    coord._email = "offline@example.invalid"
    coord._stored_password = "fixture"
    coord._remember_password = True
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []

    async def authenticate(*_args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            if login_outcome == "cancelled":
                raise
        if login_outcome == "rejected":
            raise auth_refresh_runtime.EnlightenAuthInvalidCredentials
        return AuthTokens(cookie="session=fresh", access_token="fresh"), []

    @asynccontextmanager
    async def request(_method, url, **_kwargs):
        requests.append(url.rsplit("/", 1)[-1])
        yield SimpleNamespace(status=401, headers={})

    @asynccontextmanager
    async def request_session(**_kwargs):
        yield SimpleNamespace(request=request)

    persist_tokens = Mock()
    persist_counters = Mock()
    clear_repair = Mock()
    monkeypatch.setattr(auth_refresh_runtime, "async_authenticate", authenticate)
    monkeypatch.setattr(coord.client, "_request_session", request_session)
    monkeypatch.setattr(coord, "_persist_tokens", persist_tokens)
    monkeypatch.setattr(coord, "_persist_auth_refresh_counters", persist_counters)
    monkeypatch.setattr(coord.diagnostics, "clear_reauth_issue", clear_repair)
    revision = coord.client.credential_revision
    refresh = asyncio.create_task(coord.energy._async_refresh_site_energy())
    await asyncio.wait_for(entered.wait(), timeout=1)
    if login_outcome == "invalidated":
        sibling = asyncio.create_task(coord._attempt_auto_refresh())
        await asyncio.sleep(0)
        coord.energy._invalidate_site_energy_cache()
        release.set()
        assert await sibling is True
        await refresh
        persist_tokens.assert_called_once()
        assert coord.client.credential_revision == revision + 1
        assert not cancelled.is_set()
        assert requests == ["lifetime_energy"]
        return

    coord.mark_runtime_stopped()
    release.set()
    (result,) = await asyncio.gather(refresh, return_exceptions=True)

    persist_tokens.assert_not_called()
    persist_counters.assert_called_once()
    clear_repair.assert_not_called()
    assert coord.client.credential_revision == revision
    assert coord._auth_refresh_success_count == coord._auth_refresh_failure_count == 0
    assert cancelled.is_set()
    assert requests == ["lifetime_energy"]
    if login_outcome == "cancelled":
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert result is None


@pytest.mark.parametrize(
    "obsolete_outcome", ["unauthorized", "invalid_site", "success"]
)
async def test_old_hems_credentials_cannot_change_lifetime_fallback(
    coordinator_factory, monkeypatch, obsolete_outcome
):
    from custom_components.enphase_ev import auth_refresh_runtime
    from custom_components.enphase_ev.api_client.errors import HttpUnauthorized
    from custom_components.enphase_ev.api_models import AuthTokens

    coord = coordinator_factory()
    coord._email = "offline@example.invalid"
    coord._stored_password = "fixture"
    entered, release = asyncio.Event(), asyncio.Event()
    hems_attempts = 0
    monkeypatch.setattr(
        auth_refresh_runtime,
        "async_authenticate",
        AsyncMock(
            return_value=(
                AuthTokens(cookie="session=fresh", access_token="fresh"),
                None,
            )
        ),
    )

    async def request(_method, url, **_kwargs):
        nonlocal hems_attempts
        if url.endswith("/lifetime_energy"):
            return {"consumption": [3000]}
        assert url.endswith("/hems_consumption_lifetime")
        hems_attempts += 1
        if hems_attempts == 1:
            entered.set()
            await release.wait()
            if obsolete_outcome == "unauthorized":
                raise HttpUnauthorized()
            if obsolete_outcome == "invalid_site":
                raise aiohttp.ClientResponseError(
                    SimpleNamespace(real_url=url),
                    (),
                    status=550,
                    message="INVALID_SITE",
                )
            return {"heatpump": [1000]}
        return {"heatpump": [2000]}

    monkeypatch.setattr(coord.client, "_json", request)
    refresh = asyncio.create_task(coord.energy._async_refresh_site_energy())
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert await coord.auth_refresh_runtime.async_run_auto_refresh()
    release.set()
    await refresh

    assert coord._hems_auth_failure_count == 0
    assert coord.client.hems_site_supported is None
    assert not coord.energy._hems_lifetime_backoff_active()
    assert coord.energy.site_energy["consumption"].value_kwh == 3
    assert "heat_pump" not in coord.energy.site_energy

    await coord.energy._async_refresh_site_energy(force=True)
    assert hems_attempts == 2
    assert coord.energy.site_energy["heat_pump"].value_kwh == 2


async def test_retirement_during_startup_power_skips_warmup_discovery(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev import refresh_runner
    from custom_components.enphase_ev.refresh_plan import RefreshPlan

    coord = coordinator_factory()
    coord.begin_setup_tracking(
        asyncio.get_running_loop().time(), {}, {"core_ready": 0.0}
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def acquire():
        entered.set()
        await release.wait()
        return {"consumption": [3000]}

    plan = Mock(return_value=RefreshPlan())
    publish = Mock()
    monkeypatch.setattr(refresh_runner, "warmup_plan", plan)
    monkeypatch.setattr(coord, "publish_enrichment", publish)
    monkeypatch.setattr(coord, "_async_refresh_current_power_consumption", AsyncMock())
    monkeypatch.setattr(coord.client, "lifetime_energy", acquire)

    await coord.refresh_runner.async_start_startup_power()
    await asyncio.wait_for(entered.wait(), timeout=1)
    warmup = asyncio.create_task(coord.refresh_runner.async_startup_warmup_runner())
    await asyncio.sleep(0)
    coord.mark_runtime_stopped()
    release.set()
    await warmup

    plan.assert_not_called()
    publish.assert_not_called()
    assert coord.setup_milestones == {"core_ready": 0.0}
    assert coord.startup_power_phase_timings == {}


@pytest.mark.parametrize(
    "path", ["site_only", "first_site_only", "post_session", "warmup"]
)
async def test_retirement_during_energy_refresh_stops_followups(
    coordinator_factory, monkeypatch, path
):
    from custom_components.enphase_ev import coordinator as coordinator_mod
    from custom_components.enphase_ev import refresh_runner as runner_mod
    from custom_components.enphase_ev.refresh_plan import (
        RefreshPlan,
        RefreshStage,
        callback_task,
        object_method_task,
    )

    coord = coordinator_factory()
    coord._has_successful_refresh = path != "first_site_only"
    entered, release = asyncio.Event(), asyncio.Event()

    async def acquire():
        entered.set()
        await release.wait()
        return {"consumption": [3000]}

    monkeypatch.setattr(coord.client, "lifetime_energy", AsyncMock(side_effect=acquire))
    sync_discovery = Mock()
    sync_repair = Mock()
    save_discovery = Mock()
    monkeypatch.setattr(
        coord.discovery_snapshot, "sync_site_energy_discovery_state", sync_discovery
    )
    monkeypatch.setattr(coord, "_sync_site_energy_issue", sync_repair)
    monkeypatch.setattr(coord.discovery_snapshot, "schedule_save", save_discovery)

    first_followup_started = asyncio.Event()
    first_followup_tasks = []
    topology_rebuild = Mock(return_value=True)
    topology_queued = asyncio.Event()

    async def queue_topology():
        assert coord.inventory_runtime._topology_refresh_suppressed == 1
        coord.inventory_runtime._refresh_cached_topology()
        assert coord.inventory_runtime._topology_refresh_pending
        topology_queued.set()

    async def first_followup(_timings):
        first_followup_tasks.append(asyncio.current_task())
        first_followup_started.set()
        await asyncio.Future()

    if path in {"site_only", "first_site_only"}:
        coord.site_only = True
        monkeypatch.setattr(coord, "_async_run_first_refresh_followups", first_followup)
        followup = Mock(return_value=RefreshPlan())
        monkeypatch.setattr(coordinator_mod, "build_site_only_followup_plan", followup)
        refresh = coord._async_update_data()
    elif path == "post_session":
        followup = Mock(return_value=RefreshPlan())
        monkeypatch.setattr(coordinator_mod, "build_heatpump_followup_plan", followup)
        monkeypatch.setattr(coord.evse_timeseries, "refresh_due", lambda **_: False)
        monkeypatch.setattr(
            coord.inventory_runtime, "inverters_refresh_due", lambda: True
        )
        monkeypatch.setattr(coord, "_async_refresh_inverters", queue_topology)
        monkeypatch.setattr(coord, "_refresh_cached_topology", topology_rebuild)
        refresh = coord._async_run_post_session_refresh_pipeline(
            coord._start_refresh_pipeline(), {}, DAY
        )
    else:
        followup = AsyncMock()
        plan = RefreshPlan(
            stages=(
                RefreshStage(
                    parallel_tasks=(
                        object_method_task(
                            "site_energy_s",
                            "site energy",
                            "refresh_runner",
                            "async_refresh_site_energy_for_warmup",
                        ),
                    )
                ),
                RefreshStage(
                    ordered_tasks=(
                        callback_task(
                            "later_s", "later refresh", lambda _owner: followup()
                        ),
                    )
                ),
            )
        )
        monkeypatch.setattr(runner_mod, "warmup_plan", lambda *_args, **_kwargs: plan)
        monkeypatch.setattr(
            coord.refresh_runner, "async_start_startup_power", AsyncMock()
        )
        refresh = coord.refresh_runner.async_startup_warmup_runner()

    task = asyncio.create_task(refresh)
    await asyncio.wait_for(entered.wait(), timeout=1)
    if path == "first_site_only":
        await asyncio.wait_for(first_followup_started.wait(), timeout=1)
    if path == "post_session":
        await asyncio.wait_for(topology_queued.wait(), timeout=1)
    coord.mark_runtime_stopped()
    release.set()

    (result,) = await asyncio.gather(task, return_exceptions=True)
    sync_discovery.assert_not_called()
    sync_repair.assert_not_called()
    save_discovery.assert_not_called()
    followup.assert_not_called()
    if path == "warmup":
        assert result is None
    else:
        assert isinstance(result, asyncio.CancelledError)
    if path == "first_site_only":
        (followup_task,) = first_followup_tasks
        assert followup_task.cancelled()
    if path == "post_session":
        topology_rebuild.assert_not_called()
        assert not coord.inventory_runtime._topology_refresh_pending
        assert coord.inventory_runtime._topology_refresh_suppressed == 0


@pytest.mark.parametrize("path", ["first_site_only", "site_only", "post_session"])
async def test_retirement_during_later_followups_skips_refresh_finalization(
    coordinator_factory, monkeypatch, path
):
    from custom_components.enphase_ev import coordinator as coordinator_mod
    from custom_components.enphase_ev.refresh_plan import (
        RefreshPlan,
        RefreshStage,
        callback_task,
    )

    coord = coordinator_factory()
    coord._has_successful_refresh = path != "first_site_only"
    entered, release = asyncio.Event(), asyncio.Event()
    finalize = Mock()
    persist = Mock()
    monkeypatch.setattr(coord, "_finish_refresh_pipeline", finalize)
    monkeypatch.setattr(coord.discovery_snapshot, "schedule_save", persist)
    monkeypatch.setattr(
        coord.client,
        "lifetime_energy",
        AsyncMock(
            return_value={
                "consumption": [3000],
                "evse": [100],
                "heatpump": [100],
                "water_heater": [100],
            }
        ),
    )

    async def hold_followup(*_args):
        entered.set()
        await release.wait()

    plan = RefreshPlan(
        stages=(
            RefreshStage(
                ordered_tasks=(
                    callback_task(
                        "followup_s", "followup", lambda _owner: hold_followup()
                    ),
                )
            ),
        )
    )
    if path == "first_site_only":
        coord.site_only = True
        monkeypatch.setattr(coord, "_async_run_first_refresh_followups", hold_followup)
        refresh = coord._async_update_data()
    elif path == "site_only":
        coord.site_only = True
        monkeypatch.setattr(
            coordinator_mod,
            "build_site_only_followup_plan",
            lambda *_args, **_kwargs: plan,
        )
        refresh = coord._async_update_data()
    else:
        monkeypatch.setattr(
            coordinator_mod,
            "build_heatpump_followup_plan",
            lambda *_args, **_kwargs: plan,
        )
        monkeypatch.setattr(coord.evse_timeseries, "refresh_due", lambda **_: False)
        monkeypatch.setattr(
            coord.inventory_runtime, "inverters_refresh_due", lambda: False
        )
        refresh = coord._async_run_post_session_refresh_pipeline(
            coord._start_refresh_pipeline(), {}, DAY
        )

    task = asyncio.create_task(refresh)
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert coord.energy.site_energy["consumption"].value_kwh == 3
        coord.mark_runtime_stopped()
    finally:
        release.set()
        (result,) = await asyncio.gather(task, return_exceptions=True)
    finalize.assert_not_called()
    persist.assert_not_called()
    assert isinstance(result, asyncio.CancelledError)


@pytest.mark.parametrize("sequence", ["parallel", "ordered", "next_stage"])
async def test_retired_refresh_plan_does_not_start_the_next_endpoint(
    coordinator_factory, sequence
):
    from custom_components.enphase_ev.refresh_plan import (
        RefreshPlan,
        RefreshStage,
        callback_task,
    )

    coord = coordinator_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    followup = AsyncMock()

    async def hold_first():
        entered.set()
        await release.wait()

    first = callback_task("first_s", "first endpoint", lambda _owner: hold_first())
    later = callback_task("later_s", "later endpoint", lambda _owner: followup())
    if sequence == "parallel":
        stages = (RefreshStage(parallel_tasks=(first,), ordered_tasks=(later,)),)
    elif sequence == "ordered":
        stages = (RefreshStage(ordered_tasks=(first, later)),)
    else:
        stages = (
            RefreshStage(ordered_tasks=(first,)),
            RefreshStage(ordered_tasks=(later,)),
        )
    plan = RefreshPlan(stages=stages)
    refresh = asyncio.create_task(
        coord.refresh_runner.async_run_refresh_plan({}, plan=plan)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    coord.mark_runtime_stopped()
    release.set()

    (result,) = await asyncio.gather(refresh, return_exceptions=True)
    followup.assert_not_awaited()
    assert isinstance(result, asyncio.CancelledError)


async def test_retired_refresh_does_not_process_a_late_login_wall(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.api import EnphaseLoginWallUnauthorized

    coord = coordinator_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    activate_auth_block = Mock(return_value=False)
    monkeypatch.setattr(
        coord, "_activate_auth_block_from_login_wall", activate_auth_block
    )

    async def hold_request():
        entered.set()
        await release.wait()
        raise EnphaseLoginWallUnauthorized(
            endpoint="/fixture", request_label="GET /fixture"
        )

    refresh = asyncio.create_task(
        coord.refresh_runner.async_run_refresh_call(
            "fixture_s", "fixture", hold_request
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    coord.mark_runtime_stopped()
    release.set()

    (result,) = await asyncio.gather(refresh, return_exceptions=True)
    activate_auth_block.assert_not_called()
    assert isinstance(result, asyncio.CancelledError)


@pytest.mark.parametrize("paused_lookup", range(4))
async def test_retirement_stops_secondary_warmup_lookups(
    coordinator_factory, monkeypatch, paused_lookup
):
    coord = coordinator_factory()
    serial = next(iter(coord.serials))
    working_data = {serial: {"sn": serial}}
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_lookup(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {}

    resolvers = (
        "async_resolve_charge_modes",
        "async_resolve_green_battery_settings",
        "async_resolve_auth_settings",
        "async_resolve_charger_config",
    )
    lookups = []
    for index, name in enumerate(resolvers):
        lookup = (
            AsyncMock(side_effect=hold_lookup)
            if index == paused_lookup
            else AsyncMock(return_value={})
        )
        monkeypatch.setattr(coord.evse_runtime, name, lookup)
        lookups.append(lookup)

    runner = coord.refresh_runner
    refresh = asyncio.create_task(
        runner.async_run_refresh_call(
            "secondary_evse_state_s",
            "secondary EVSE state",
            lambda: runner.async_refresh_secondary_evse_state_for_warmup(
                working_data=working_data
            ),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        coord.mark_runtime_stopped()
    finally:
        release.set()
        (result,) = await asyncio.gather(refresh, return_exceptions=True)

    for lookup in lookups[paused_lookup + 1 :]:
        lookup.assert_not_awaited()
    assert isinstance(result, asyncio.CancelledError)
    assert working_data == {serial: {"sn": serial}}


@pytest.mark.parametrize("disconnect", [False, True])
async def test_retired_warmup_config_read_does_not_start_backoff(
    coordinator_factory, monkeypatch, loopback_sockets, disconnect
):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from custom_components.enphase_ev.api_client import evse_surface

    coord = coordinator_factory()
    serial = next(iter(coord.serials))
    entered, release = asyncio.Event(), asyncio.Event()
    for name in (
        "async_resolve_charge_modes",
        "async_resolve_green_battery_settings",
        "async_resolve_auth_settings",
    ):
        monkeypatch.setattr(coord.evse_runtime, name, AsyncMock(return_value={}))

    async def respond(request):
        entered.set()
        await release.wait()
        if disconnect:
            request.transport.close()
        return web.json_response({"data": []})

    app = web.Application()
    app.router.add_post("/{path:.*}", respond)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(
            evse_surface, "BASE_URL", str(server.make_url("/")).rstrip("/")
        )
        monkeypatch.setattr(coord.client, "_s", session)
        refresh = asyncio.create_task(
            coord.refresh_runner.async_refresh_secondary_evse_state_for_warmup(
                working_data={serial: {"sn": serial}}
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            coord.mark_runtime_stopped()
        finally:
            release.set()
            (result,) = await asyncio.gather(refresh, return_exceptions=True)

    assert isinstance(result, asyncio.CancelledError)
    assert serial not in coord.evse_runtime.state._charger_config_backoff_until


@pytest.mark.parametrize("disconnect", [False, True])
@pytest.mark.parametrize("held_endpoint", ["phase_map_multiple_envoy", "devices.json"])
async def test_retired_inventory_stops_requests_and_cache_updates(
    coordinator_factory, monkeypatch, loopback_sockets, held_endpoint, disconnect
):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from custom_components.enphase_ev.api_client import dashboard_surface

    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = []

    async def respond(request):
        endpoint = request.path.rsplit("/", 1)[-1]
        dispatched.append((endpoint, coord.runtime_active))
        if endpoint == held_endpoint and not entered.is_set():
            entered.set()
            await release.wait()
            if disconnect:
                request.transport.close()
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/{path:.*}", respond)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(
            dashboard_surface, "BASE_URL", str(server.make_url("/")).rstrip("/")
        )
        monkeypatch.setattr(coord.client, "_s", session)
        refresh = asyncio.create_task(
            coord.refresh_runner.async_run_refresh_call(
                "devices_inventory_s",
                "device inventory",
                runtime._async_refresh_devices_inventory,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            topology_health = deepcopy(
                coord._endpoint_family_health.get("inventory_topology")
            )
            coord.mark_runtime_stopped()
        finally:
            release.set()
            (result,) = await asyncio.gather(refresh, return_exceptions=True)
        with pytest.raises(asyncio.CancelledError):
            await runtime._async_refresh_devices_inventory(force=True)

    expected = [("phase_map_multiple_envoy", True)]
    if held_endpoint == "devices.json":
        expected.append(("devices.json", True))
    else:
        assert runtime._gateway_phase_map_cache_until is None
        assert runtime._gateway_phase_map_failure_backoff_until is None
    assert dispatched == expected
    assert isinstance(result, asyncio.CancelledError)
    assert runtime._devices_inventory_cache_until is None
    assert coord._endpoint_family_health.get("inventory_topology") == topology_health


@pytest.mark.parametrize("outcome", ["success", "login_wall", "timeout"])
async def test_retirement_during_status_skips_response_side_effects(
    coordinator_factory, monkeypatch, outcome
):
    from custom_components.enphase_ev.api import EnphaseLoginWallUnauthorized

    coord = coordinator_factory()
    serial = next(iter(coord.serials))
    entered, release = asyncio.Event(), asyncio.Event()
    followup_started = asyncio.Event()
    followup_release = asyncio.Event()
    followup_tasks = []

    async def hold_status():
        entered.set()
        await release.wait()
        if outcome == "login_wall":
            raise EnphaseLoginWallUnauthorized(
                endpoint="/fixture", request_label="GET /fixture"
            )
        if outcome == "timeout":
            raise asyncio.TimeoutError
        return {"evChargerData": [{"sn": serial, "pluggedIn": True}]}

    async def hold_first_followup(_timings):
        followup_tasks.append(asyncio.current_task())
        followup_started.set()
        await followup_release.wait()

    monkeypatch.setattr(coord.client, "status", hold_status)
    monkeypatch.setattr(
        coord, "_async_run_first_refresh_followups", hold_first_followup
    )
    clear_repairs = Mock(side_effect=followup_release.set)
    activate_auth_block = Mock(return_value=False)
    schedule_backoff = Mock()
    monkeypatch.setattr(coord, "_clear_auth_repair_issues_on_success", clear_repairs)
    monkeypatch.setattr(
        coord, "_activate_auth_block_from_login_wall", activate_auth_block
    )
    monkeypatch.setattr(coord, "_schedule_backoff_timer", schedule_backoff)

    refresh = asyncio.create_task(coord._async_update_data())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.wait_for(followup_started.wait(), timeout=1)
        coord.mark_runtime_stopped()
    finally:
        release.set()
        (result,) = await asyncio.wait_for(
            asyncio.gather(refresh, return_exceptions=True), timeout=1
        )

    clear_repairs.assert_not_called()
    activate_auth_block.assert_not_called()
    schedule_backoff.assert_not_called()
    assert coord._status_payload_cache is None
    assert isinstance(result, asyncio.CancelledError)
    (followup,) = followup_tasks
    assert followup.cancelled()


@pytest.mark.parametrize("first_refresh", [False, True])
async def test_retirement_after_charger_followup_skips_evse_enrichment(
    coordinator_factory, monkeypatch, first_refresh
):
    from custom_components.enphase_ev import coordinator as coordinator_mod
    from custom_components.enphase_ev.refresh_plan import (
        RefreshPlan,
        RefreshStage,
        callback_task,
    )

    coord = coordinator_factory()
    coord._has_successful_refresh = not first_refresh
    serial = next(iter(coord.serials))
    coord.client.status = AsyncMock(
        return_value={"evChargerData": [{"sn": serial, "pluggedIn": False}]}
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_followup(*_args):
        entered.set()
        await release.wait()

    if first_refresh:
        monkeypatch.setattr(coord, "_async_run_first_refresh_followups", hold_followup)
    else:
        plan = RefreshPlan(
            stages=(
                RefreshStage(
                    parallel_tasks=(
                        callback_task(
                            "battery_s", "battery", lambda _owner: hold_followup()
                        ),
                    )
                ),
            )
        )
        monkeypatch.setattr(
            coordinator_mod,
            "build_followup_plan",
            lambda *_args, **_kwargs: plan,
        )
    clear_auth = Mock()
    enrich = AsyncMock(return_value=({}, {}, {}, {}))
    monkeypatch.setattr(
        coord, "_clear_auth_refresh_rejection_state_if_unchanged", clear_auth
    )
    monkeypatch.setattr(coord, "_async_resolve_post_status_evse_enrichments", enrich)

    refresh = asyncio.create_task(coord._async_update_data())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        coord.mark_runtime_stopped()
    finally:
        release.set()
        (result,) = await asyncio.gather(refresh, return_exceptions=True)

    clear_auth.assert_not_called()
    enrich.assert_not_awaited()
    assert isinstance(result, asyncio.CancelledError)


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
