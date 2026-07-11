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
async def test_async_refresh_units_str_failure_is_rejected(coordinator_factory) -> None:
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
    assert coord._current_power_consumption_w is None
    assert coord.current_power_runtime.diagnostics()["validation_state"] == (
        "invalid_unit"
    )


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


@pytest.mark.parametrize(
    ("value", "units", "expected_w"),
    [
        (2500, "W", 2500.0),
        (2.5, "kW", 2500.0),
        (2_500_000, "mW", 2500.0),
        (2500, None, 2500.0),
        (2500, "", 2500.0),
    ],
)
@pytest.mark.asyncio
async def test_async_refresh_normalizes_supported_power_units(
    coordinator_factory, value, units, expected_w
) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(
            return_value={
                "value": value,
                "units": units,
                "time": 1_700_000_000,
            }
        )
    )

    await coord.current_power_runtime.async_refresh()

    assert coord._current_power_consumption_w == expected_w
    assert coord.current_power_runtime.diagnostics()["last_normalized_value_w"] == (
        expected_w
    )


@pytest.mark.asyncio
async def test_async_refresh_unknown_unit_preserves_last_good_sample(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._current_power_consumption_w = 2500.0
    coord.client = SimpleNamespace(
        latest_power=AsyncMock(
            return_value={"value": 2.5, "units": "MW", "time": 1_700_000_000}
        )
    )

    await coord.current_power_runtime.async_refresh()

    diagnostics = coord.current_power_runtime.diagnostics()
    assert coord._current_power_consumption_w == 2500.0
    assert diagnostics["validation_state"] == "invalid_unit"
    assert diagnostics["validation_reason"] == "unsupported_power_unit"
    assert diagnostics["using_stale"] is True


@pytest.mark.asyncio
async def test_async_refresh_extreme_requires_new_source_timestamp(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._current_power_consumption_w = 2500.0
    fetcher = AsyncMock(
        side_effect=[
            {"value": -12_000_000, "units": "W", "time": 1_700_000_000},
            {"value": -13_000_000, "units": "W", "time": 1_700_000_000},
            {"value": -15_000_000, "units": "W", "time": 1_700_000_060},
        ]
    )
    coord.client = SimpleNamespace(latest_power=fetcher)

    await coord.current_power_runtime.async_refresh()
    assert coord._current_power_consumption_w == 2500.0
    assert coord.current_power_runtime.using_stale is True
    assert coord.current_power_runtime.diagnostics()["pending_extreme_count"] == 1

    coord.current_power_runtime._cache_until_mono = None  # noqa: SLF001
    coord._endpoint_family_state("current_power").next_retry_mono = None  # noqa: SLF001
    await coord.current_power_runtime.async_refresh()
    assert coord._current_power_consumption_w == 2500.0

    coord.current_power_runtime._cache_until_mono = None  # noqa: SLF001
    coord._endpoint_family_state("current_power").next_retry_mono = None  # noqa: SLF001
    await coord.current_power_runtime.async_refresh()
    diagnostics = coord.current_power_runtime.diagnostics()
    assert coord._current_power_consumption_w == -15_000_000
    assert diagnostics["validation_state"] == "confirmed_extreme"
    assert diagnostics["pending_extreme_count"] == 0
    assert diagnostics["using_stale"] is False


@pytest.mark.asyncio
async def test_async_refresh_normal_sample_clears_pending_extreme(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    fetcher = AsyncMock(
        side_effect=[
            {"value": 12_000_000, "units": "W", "time": 1_700_000_000},
            {"value": 2500, "units": "W", "time": 1_700_000_060},
        ]
    )
    coord.client = SimpleNamespace(latest_power=fetcher)

    await coord.current_power_runtime.async_refresh()
    coord.current_power_runtime._cache_until_mono = None  # noqa: SLF001
    coord._endpoint_family_state("current_power").next_retry_mono = None  # noqa: SLF001
    await coord.current_power_runtime.async_refresh()

    diagnostics = coord.current_power_runtime.diagnostics()
    assert coord._current_power_consumption_w == 2500
    assert diagnostics["validation_state"] == "accepted"
    assert diagnostics["pending_extreme_count"] == 0


@pytest.mark.asyncio
async def test_async_refresh_timestamp_less_extreme_remains_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    fetcher = AsyncMock(
        side_effect=[
            {"value": 12_000_000, "units": "W"},
            {"value": 13_000_000, "units": "W"},
        ]
    )
    coord.client = SimpleNamespace(latest_power=fetcher)

    await coord.current_power_runtime.async_refresh()
    coord.current_power_runtime._cache_until_mono = None  # noqa: SLF001
    coord._endpoint_family_state("current_power").next_retry_mono = None  # noqa: SLF001
    await coord.current_power_runtime.async_refresh()

    diagnostics = coord.current_power_runtime.diagnostics()
    assert coord._current_power_consumption_w is None
    assert diagnostics["validation_reason"] == "extreme_sample_missing_timestamp"
    assert diagnostics["pending_extreme_count"] == 1
