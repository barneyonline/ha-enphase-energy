"""The latest-power endpoint remains independently validated for diagnostics."""

import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.util import dt as dt_util
from custom_components.enphase_ev.current_power_runtime import CurrentPowerRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 2500])
async def test_same_timestamp_value_corrections_are_retained(
    coordinator_factory, monkeypatch, caplog, value
):
    """Without an interval contract, preserve returned watts and log safe evidence."""
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.last_success_utc = now[0]
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    payload = {
        "value": value,
        "time": now[0].timestamp(),
        "units": "W",
        "precision": 0,
        "token": "private-token",
        "site_id": "private-site",
    }
    coord.client.latest_power = AsyncMock(return_value=payload)
    runtime = coord.current_power_runtime
    with caplog.at_level(
        logging.DEBUG, logger="custom_components.enphase_ev.current_power_runtime"
    ):
        await runtime.async_refresh()
        now[0] += timedelta(minutes=5)
        payload["value"] = 3971
        runtime._cache_until_mono = None
        await runtime.async_refresh()
    assert runtime.snapshot.w == 3971
    assert runtime.snapshot.sample_utc.timestamp() == payload["time"]
    assert "value_w=3971.0" in caplog.text
    assert "received_at_utc=" in caplog.text
    assert "private-token" not in caplog.text
    assert "private-site" not in caplog.text


@pytest.mark.asyncio
async def test_runtime_uses_only_public_host_services(monkeypatch):
    """A minimal host can own the runtime without a coordinator instance."""
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    host = SimpleNamespace(
        site_id="site-test",
        client=SimpleNamespace(latest_power=AsyncMock(return_value={"value": 42})),
        endpoint_family_should_run=Mock(return_value=True),
        note_endpoint_family_success=Mock(),
        note_endpoint_family_failure=Mock(return_value=True),
    )
    runtime = CurrentPowerRuntime(host)
    assert runtime.refresh_due()
    await runtime.async_refresh()
    assert runtime.snapshot.w == 42
    assert runtime.received_utc == now
    host.note_endpoint_family_success.assert_called_once_with("current_power")
    runtime._cache_until_mono = None
    host.client.latest_power.side_effect = TimeoutError()
    await runtime.async_refresh()
    host.note_endpoint_family_failure.assert_called_once()
    assert runtime.received_utc == now
    runtime.clear()
    assert runtime.received_utc is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 2, 2500])
async def test_repeated_future_source_timestamp_is_rejected_and_recovers(
    coordinator_factory, monkeypatch, value
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    coord.last_success_utc = now[0]
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    coord.client.latest_power = AsyncMock(
        return_value={"value": value, "time": (now[0] + timedelta(days=1)).timestamp()}
    )
    runtime = coord.current_power_runtime
    await runtime.async_refresh()
    received = runtime.received_utc
    assert received is None
    assert runtime.snapshot.w is None
    assert runtime.diagnostics()["validation_state"] == "future_timestamp"
    for minutes in (5, 26):
        now[0] += timedelta(minutes=minutes)
        coord.last_success_utc = now[0]
        runtime._cache_until_mono = None
        await runtime.async_refresh()
        assert runtime.received_utc == received
    assert runtime.snapshot.w is None
    coord.client.latest_power.return_value["time"] = now[0].timestamp()
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.snapshot.w == value
    # Compatibility setters may seed a snapshot without a receipt timestamp.
    runtime.received_utc = None
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.received_utc == now[0]


@pytest.mark.parametrize(
    "value,units",
    [(0, "W"), (1, "W"), (-1, "W"), (0.001, "kW"), (2, "W"), (-2, "W"), (0.002, "kW")],
)
@pytest.mark.parametrize(
    "failure", ["network", "invalid", "unit", "extreme", "backoff"]
)
async def test_diagnostic_idle_receipt_changes_only_after_valid_acquisition(
    coordinator_factory, monkeypatch, value, units, failure
):
    """Retain raw-endpoint safeguards independently of the energy-derived sensor."""
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    sampled = now[0] - timedelta(minutes=5)
    payload = {"value": value, "units": units, "time": sampled.timestamp()}
    coord.client.latest_power = AsyncMock(return_value=payload)
    runtime = coord.current_power_runtime
    for _ in range(3):
        runtime._cache_until_mono = None
        await runtime.async_refresh()
        assert runtime.received_utc == now[0]
        assert runtime.snapshot.sample_utc == sampled
        now[0] += timedelta(minutes=15)
    accepted_receipt = runtime.received_utc
    if failure == "network":
        coord.client.latest_power.side_effect = TimeoutError()
    elif failure == "backoff":
        monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: False)
    else:
        coord.client.latest_power.return_value = {
            "invalid": {"value": "invalid"},
            "unit": {"value": 0, "units": "unknown"},
            "extreme": {"value": 12_000_000, "time": sampled.timestamp()},
        }[failure]
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.received_utc == accepted_receipt
    revision = runtime.snapshot.freshness_revision
    now[0] += timedelta(minutes=31)
    coord.client.latest_power.side_effect = None
    coord.client.latest_power.return_value = payload
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.received_utc == now[0]
    assert runtime.snapshot.freshness_revision == revision + 1


@pytest.mark.parametrize("value", [0, 2, 100])
async def test_diagnostic_equal_sample_recovery_updates_snapshot_revision(
    coordinator_factory, monkeypatch, value
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    monkeypatch.setattr(coord, "endpoint_family_should_run", lambda _family: True)
    coord.client.latest_power = AsyncMock(
        return_value={
            "value": value,
            "time": now[0].timestamp() if value in (0, 2) else None,
        }
    )
    runtime = coord.current_power_runtime
    await runtime.async_refresh()
    revision = runtime.snapshot.freshness_revision
    now[0] += timedelta(seconds=60)
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.snapshot.freshness_revision == revision
    now[0] += timedelta(minutes=31)
    runtime._cache_until_mono = None
    await runtime.async_refresh()
    assert runtime.snapshot.freshness_revision == revision + 1
