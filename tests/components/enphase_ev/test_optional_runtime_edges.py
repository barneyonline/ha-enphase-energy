"""Optional runtime refreshes preserve state across sparse data and races."""

import asyncio
from datetime import datetime, timezone
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.energy import EnergyManager
from custom_components.enphase_ev.evse_timeseries import EVSETimeseriesManager


@pytest.mark.parametrize("manual", [False, True])
async def test_auth_refresh_reuses_task_started_while_waiting_for_lock(
    coordinator_factory, manual
):
    coord = coordinator_factory()
    coord._email = "user@example.com"
    coord._remember_password = True
    coord._stored_password = "password"
    runtime = coord.auth_refresh_runtime
    entered = asyncio.Event()
    completed = asyncio.Event()

    async def existing_login():
        entered.set()
        await completed.wait()
        return True

    await runtime._task_lock.acquire()
    request = asyncio.create_task(
        runtime.attempt_manual_refresh() if manual else runtime.attempt_auto_refresh()
    )
    try:
        await asyncio.sleep(0)
        shared = asyncio.create_task(existing_login())
        await entered.wait()
        runtime.state._auth_refresh_task = shared
        runtime._task_lock.release()
        completed.set()
        result = await request
        assert (result.success if manual else result) is True
        assert runtime.state._auth_refresh_task is shared
        await shared
        runtime.clear_auth_refresh_task(shared)
        assert runtime.state._auth_refresh_task is None
        runtime.clear_auth_refresh_task(shared)
        assert runtime.state._auth_refresh_task is None
    finally:
        completed.set()
        if runtime._task_lock.locked():
            runtime._task_lock.release()
        await request


@pytest.mark.parametrize("fresh", ["daily", "lifetime"])
async def test_timeseries_refresh_only_fetches_expired_family(hass, fresh):
    client = SimpleNamespace(
        evse_timeseries_daily_energy=AsyncMock(return_value=None),
        evse_timeseries_lifetime_energy=AsyncMock(return_value=None),
    )
    manager = EVSETimeseriesManager(hass, lambda: client)
    now = time.monotonic()
    day = datetime(2026, 10, 31, tzinfo=timezone.utc)
    if fresh == "daily":
        manager._daily_cache["2026-10-31"] = (now, {})
    else:
        manager._lifetime_cache = (now, {})
    await manager.async_refresh(day_local=day)
    assert client.evse_timeseries_daily_energy.await_count == (fresh != "daily")
    assert client.evse_timeseries_lifetime_energy.await_count == (fresh != "lifetime")


async def test_timeseries_without_fetcher_extensions_does_not_create_data(hass):
    manager = EVSETimeseriesManager(hass, lambda: SimpleNamespace())
    await manager.async_refresh(force=True)
    assert manager._daily_cache == {}
    assert manager._lifetime_cache is None


def test_timeseries_sparse_entries_do_not_overwrite_energy_values(hass):
    manager = EVSETimeseriesManager(hass, lambda: None)
    day = datetime(2026, 10, 31, tzinfo=timezone.utc)
    manager._daily_cache["2026-10-31"] = (time.monotonic() + 60, {"EV": {}})
    manager._lifetime_cache = (time.monotonic(), {"EV": {}})
    assert manager.daily_cache_age == {}
    payloads = {"EV": {"evse_daily_energy_kwh": 1.0, "evse_lifetime_energy_kwh": 10.0}}
    manager.merge_charger_payloads(payloads, day_local=day)
    assert payloads["EV"] == {
        "evse_daily_energy_kwh": 1.0,
        "evse_lifetime_energy_kwh": 10.0,
        "evse_timeseries_source": "evse_timeseries",
    }


@pytest.mark.parametrize("hems_error", [False, True])
async def test_site_energy_fallback_without_auth_callbacks(hems_error):
    fallback = AsyncMock(
        side_effect=RuntimeError("temporary") if hems_error else None,
        return_value={"heatpump": [10]},
    )
    client = SimpleNamespace(
        lifetime_energy=AsyncMock(return_value={"production": [1000]}),
        hems_consumption_lifetime=fallback,
    )
    manager = EnergyManager(
        client_provider=lambda: client,
        site_id="SITE",
        logger=logging.getLogger(__name__),
    )
    await manager._async_refresh_site_energy(force=True)
    assert manager.site_energy["solar_production"].value_kwh == 1
    fallback.assert_awaited_once()


async def test_site_energy_rejects_non_mapping_payload():
    client = SimpleNamespace(lifetime_energy=AsyncMock(return_value=[]))
    manager = EnergyManager(
        client_provider=lambda: client,
        site_id="SITE",
        logger=logging.getLogger(__name__),
    )
    await manager._async_refresh_site_energy(force=True)
    assert manager.site_energy == {}
    assert manager.site_energy_fetch_diagnostics["last_outcome"] == "invalid_payload"


def test_lifetime_guard_without_summary_invalidator_holds_suspected_reset():
    manager = EnergyManager(
        client_provider=lambda: None, site_id="SITE", logger=logging.getLogger(__name__)
    )
    assert manager._apply_lifetime_guard("EV", 1000000, None) == 1000
    assert manager._apply_lifetime_guard("EV", 1, None) == 1000
