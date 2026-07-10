"""Tests for CurrentPowerRuntime."""

from __future__ import annotations

from datetime import datetime, timezone
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_clear_resets_fields(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._current_power_consumption_w = 1.0
    coord._current_power_consumption_sample_utc = datetime.now(timezone.utc)
    coord._current_power_consumption_reported_units = "W"
    coord._current_power_consumption_reported_precision = 0
    coord._current_power_consumption_source = "x"
    coord.current_power_runtime._cache_until_mono = 123.0  # noqa: SLF001

    coord.current_power_runtime.clear()

    assert coord._current_power_consumption_w is None
    assert coord._current_power_consumption_sample_utc is None
    assert coord._current_power_consumption_reported_units is None
    assert coord._current_power_consumption_reported_precision is None
    assert coord._current_power_consumption_source is None
    assert coord.current_power_runtime._cache_until_mono is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_refresh_no_fetcher(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace()
    coord._current_power_consumption_w = 100.0

    await coord.current_power_runtime.async_refresh()

    assert coord._current_power_consumption_w is None


def test_refresh_due_requests_cleanup_when_fetcher_missing_with_cached_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace()
    coord._current_power_consumption_w = 100.0

    assert coord.current_power_runtime.refresh_due() is True


def test_refresh_due_respects_success_cache_ttl(
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(latest_power=AsyncMock())
    coord.current_power_runtime._cache_until_mono = 160.0  # noqa: SLF001

    monkeypatch.setattr(
        "custom_components.enphase_ev.current_power_runtime.time.monotonic",
        lambda: 120.0,
    )
    assert coord.current_power_runtime.refresh_due() is False

    monkeypatch.setattr(
        "custom_components.enphase_ev.current_power_runtime.time.monotonic",
        lambda: 160.0,
    )
    assert coord.current_power_runtime.refresh_due() is True


@pytest.mark.asyncio
async def test_async_refresh_fetcher_raises(coordinator_factory) -> None:
    coord = coordinator_factory()

    async def _boom():
        raise RuntimeError("network")

    coord.client = SimpleNamespace(latest_power=_boom)
    coord._current_power_consumption_w = 100.0

    await coord.current_power_runtime.async_refresh()

    assert coord._current_power_consumption_w == 100.0
    assert coord.current_power_runtime.using_stale is True
    assert (
        coord._endpoint_family_state("current_power").consecutive_failures == 1
    )  # noqa: SLF001


@pytest.mark.parametrize("payload", [None, "x", {}, {"value": "nope"}])
@pytest.mark.asyncio
async def test_async_refresh_invalid_payload_shapes(
    coordinator_factory, payload
) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(latest_power=AsyncMock(return_value=payload))

    await coord.current_power_runtime.async_refresh()

    assert coord._current_power_consumption_w is None
    assert (
        coord._endpoint_family_state("current_power").consecutive_failures == 1
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_refresh_non_finite_cleared(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(return_value={"value": float("nan")})
    )

    await coord.current_power_runtime.async_refresh()
    assert coord._current_power_consumption_w is None


@pytest.mark.asyncio
async def test_async_refresh_success_ms_timestamp_and_units(
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(
            return_value={
                "value": 42.5,
                "time": 1_700_000_000_000,
                "units": "  W ",
                "precision": "0",
            }
        )
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.current_power_runtime.time.monotonic",
        lambda: 100.0,
    )

    await coord.current_power_runtime.async_refresh()

    assert coord._current_power_consumption_w == 42.5
    assert coord._current_power_consumption_reported_units == "W"
    assert coord._current_power_consumption_reported_precision == 0
    assert coord._current_power_consumption_source == "app-api:get_latest_power"
    assert coord._current_power_consumption_sample_utc is not None
    assert coord.current_power_runtime._cache_until_mono == 160.0  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_refresh_skips_fetch_inside_success_cache_ttl(
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory()
    fetcher = AsyncMock(return_value={"value": 42.5})
    coord.client = SimpleNamespace(latest_power=fetcher)

    monkeypatch.setattr(
        "custom_components.enphase_ev.current_power_runtime.time.monotonic",
        lambda: 100.0,
    )
    await coord.current_power_runtime.async_refresh()

    monkeypatch.setattr(
        "custom_components.enphase_ev.current_power_runtime.time.monotonic",
        lambda: 120.0,
    )
    await coord.current_power_runtime.async_refresh()

    assert fetcher.await_count == 1
    assert coord._current_power_consumption_w == 42.5


@pytest.mark.asyncio
async def test_async_refresh_failure_preserves_sample_and_suppresses_retry(
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory()
    fetcher = AsyncMock(
        side_effect=[{"value": 42.5, "units": "W"}, RuntimeError("network")]
    )
    coord.client = SimpleNamespace(latest_power=fetcher)
    monotonic = 100.0
    monkeypatch.setattr(
        "custom_components.enphase_ev.current_power_runtime.time.monotonic",
        lambda: monotonic,
    )

    await coord.current_power_runtime.async_refresh()
    coord._endpoint_family_state("current_power").next_retry_mono = None  # noqa: SLF001
    monotonic = 161.0
    await coord.current_power_runtime.async_refresh()
    monotonic = 200.0
    await coord.current_power_runtime.async_refresh()

    assert fetcher.await_count == 2
    assert coord._current_power_consumption_w == 42.5
    assert coord._current_power_consumption_reported_units == "W"
    assert coord.current_power_runtime.using_stale is True


def test_refresh_due_suppresses_current_power_during_failure_backoff(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(latest_power=AsyncMock())
    coord._endpoint_family_state("current_power").next_retry_mono = (  # noqa: SLF001
        time.monotonic() + 300.0
    )

    assert coord.current_power_runtime.refresh_due() is False


@pytest.mark.asyncio
async def test_async_refresh_units_str_failure(coordinator_factory) -> None:
    coord = coordinator_factory()

    class _BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    coord.client = SimpleNamespace(
        latest_power=AsyncMock(
            return_value={
                "value": 1.0,
                "units": _BadStr(),
            }
        )
    )

    await coord.current_power_runtime.async_refresh()
    assert coord._current_power_consumption_w == 1.0
    assert coord._current_power_consumption_reported_units is None


@pytest.mark.asyncio
async def test_async_refresh_precision_invalid(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(return_value={"value": 2.0, "precision": object()})
    )

    await coord.current_power_runtime.async_refresh()
    assert coord._current_power_consumption_w == 2.0
    assert coord._current_power_consumption_reported_precision is None


@pytest.mark.asyncio
async def test_async_refresh_sample_time_invalid(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(return_value={"value": 3.0, "time": "not-a-number"})
    )

    await coord.current_power_runtime.async_refresh()
    assert coord._current_power_consumption_w == 3.0
    assert coord._current_power_consumption_sample_utc is None
