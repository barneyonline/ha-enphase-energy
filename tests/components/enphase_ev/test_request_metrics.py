"""Tests for operation-scoped request and snapshot performance metrics."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.enphase_ev.request_metrics import (
    record_request_attempt,
    record_request_timings,
    request_metrics_scope,
)
from custom_components.enphase_ev.coordinator_refresh_metrics import (
    refresh_performance_history_summary,
)
from custom_components.enphase_ev.discovery_snapshot import (
    _compact_discovery_record,
    _compact_keyed_records,
    _compact_type_buckets,
)


def test_request_metrics_scopes_are_isolated() -> None:
    """Nested and unscoped requests must not pollute an outer operation."""

    record_request_attempt()
    record_request_timings(queue_s=1, network_s=1, parsing_s=1)

    with request_metrics_scope("core_refresh") as outer:
        record_request_attempt()
        record_request_timings(queue_s=0.125, network_s=0.25, parsing_s=0.375)
        with request_metrics_scope("session_history") as nested:
            record_request_attempt()
            record_request_attempt()
            record_request_timings(queue_s=-1, network_s=0.5)

        assert outer.attempts == 1
        assert outer.phase_timings() == {
            "request_queue_s": 0.125,
            "request_network_s": 0.25,
            "response_parsing_s": 0.375,
        }
        assert nested.attempts == 2
        assert nested.phase_timings() == {"request_network_s": 0.5}


def test_discovery_compaction_defensive_paths(coordinator_factory) -> None:
    """Compaction accepts partial persisted and upstream inventory shapes."""

    assert _compact_discovery_record("bad") == {}
    assert _compact_keyed_records("bad") == {}
    assert _compact_type_buckets("bad") == {}
    assert _compact_type_buckets(
        {
            "bad": "bucket",
            "envoy": {"count": 1, "devices": "bad"},
            "iqevse": {"devices": ["bad", {"serial_number": "EVSE-1"}]},
        }
    ) == {
        "envoy": {"count": 1, "devices": []},
        "iqevse": {"devices": [{"serial_number": "EVSE-1"}]},
    }
    coord = coordinator_factory()
    coord._type_device_buckets = {"bad": "bucket"}  # type: ignore[dict-item]  # noqa: SLF001
    assert coord.discovery_snapshot._discovery_key()  # noqa: SLF001


@pytest.mark.asyncio
async def test_failed_and_cancelled_refreshes_record_scoped_metrics(
    coordinator_factory,
) -> None:
    """Every attempted coordinator refresh contributes a history sample."""

    coord = coordinator_factory()

    async def _fail(_context) -> dict[str, dict[str, object]]:
        record_request_attempt()
        record_request_timings(queue_s=0.1, network_s=0.2, parsing_s=0.3)
        raise RuntimeError("failed")

    coord._async_update_data_impl = _fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="failed"):
        await coord._async_update_data()  # noqa: SLF001

    failed = coord._refresh_performance_history[-1]  # noqa: SLF001
    assert failed["outcome"] == "failed"
    assert failed["cloud_calls"] == 1
    assert failed["phase_timings"]["request_queue_s"] == 0.1
    assert failed["phase_timings"]["request_network_s"] == 0.2
    assert failed["phase_timings"]["response_parsing_s"] == 0.3
    assert failed["phase_timings"]["total_s"] >= 0

    async def _cancel(_context) -> dict[str, dict[str, object]]:
        record_request_attempt()
        raise asyncio.CancelledError

    coord._async_update_data_impl = _cancel  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await coord._async_update_data()  # noqa: SLF001

    cancelled = coord._refresh_performance_history[-1]  # noqa: SLF001
    assert cancelled["outcome"] == "cancelled"
    assert cancelled["cloud_calls"] == 1
    summary = refresh_performance_history_summary(
        coord._refresh_performance_history  # noqa: SLF001
    )
    assert summary["failed_count"] == 1
    assert summary["cancelled_count"] == 1


@pytest.mark.asyncio
async def test_snapshot_skips_telemetry_only_updates_at_scale(
    coordinator_factory,
) -> None:
    """Large inverter telemetry updates must not rebuild persisted discovery."""

    coord = coordinator_factory()
    inverter_data = {
        f"INV-{index:04d}": {
            "serial_number": f"INV-{index:04d}",
            "name": f"Inverter {index}",
            "sku_id": "IQ8",
            "lifetime_production_wh": index * 1000,
            "rssi": -50,
        }
        for index in range(500)
    }
    coord._inverter_order = list(inverter_data)  # noqa: SLF001
    coord._inverter_data = inverter_data  # noqa: SLF001
    coord.inventory_runtime._inverter_order = coord._inverter_order  # noqa: SLF001
    coord.inventory_runtime._inverter_data = inverter_data  # noqa: SLF001

    manager = coord.discovery_snapshot
    original_capture = manager.capture
    manager.capture = Mock(wraps=original_capture)  # type: ignore[method-assign]
    coord._discovery_snapshot_save_cancel = lambda: None  # noqa: SLF001

    manager.schedule_save()
    assert manager.capture.call_count == 1  # type: ignore[attr-defined]
    pending = manager._pending_snapshot  # noqa: SLF001
    assert pending is not None
    compact = pending["inverter_data"]
    assert isinstance(compact, dict)
    assert len(compact) == 500
    assert "lifetime_production_wh" not in compact["INV-0001"]
    assert "rssi" not in compact["INV-0001"]

    for index, record in enumerate(inverter_data.values()):
        record["lifetime_production_wh"] = index * 2000
        record["rssi"] = -60
    manager.schedule_save()
    assert manager.capture.call_count == 1  # type: ignore[attr-defined]

    inverter_data["INV-0001"]["name"] = "Renamed inverter"
    manager.schedule_save()
    assert manager.capture.call_count == 2  # type: ignore[attr-defined]

    coord._discovery_snapshot_save_cancel = None  # noqa: SLF001
    manager._store = SimpleNamespace(async_save=AsyncMock())  # noqa: SLF001
    await manager.async_save()
    assert manager.capture.call_count == 2  # type: ignore[attr-defined]
    manager._store.async_save.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_snapshot_save_coalesces_mutation_during_write(
    coordinator_factory, monkeypatch
) -> None:
    """A revision arriving during storage I/O remains queued for one later save."""

    coord = coordinator_factory()
    manager = coord.discovery_snapshot
    coord._inverter_order = ["INV-1"]  # noqa: SLF001
    coord._inverter_data = {  # noqa: SLF001
        "INV-1": {"serial_number": "INV-1", "name": "Before"}
    }
    coord.inventory_runtime._inverter_order = coord._inverter_order  # noqa: SLF001
    coord.inventory_runtime._inverter_data = coord._inverter_data  # noqa: SLF001
    coord._discovery_snapshot_save_cancel = lambda: None  # noqa: SLF001
    manager.schedule_save()

    scheduled: list[object] = []

    def _call_later(_hass, _delay, callback):
        scheduled.append(callback)
        return lambda: None

    from custom_components.enphase_ev import discovery_snapshot as snapshot_module

    monkeypatch.setattr(snapshot_module, "async_call_later", _call_later)

    async def _save(_snapshot) -> None:
        coord._inverter_data["INV-1"]["name"] = "After"  # noqa: SLF001
        manager.schedule_save()

    coord._discovery_snapshot_save_cancel = None  # noqa: SLF001
    manager._store = SimpleNamespace(
        async_save=AsyncMock(side_effect=_save)
    )  # noqa: SLF001
    await manager.async_save()

    assert coord._discovery_snapshot_pending is True  # noqa: SLF001
    assert manager._pending_snapshot is not None  # noqa: SLF001
    assert scheduled


def test_unchanged_snapshot_revision_matches_persisted_payload(
    coordinator_factory,
) -> None:
    """A newly observed but already persisted revision remains a no-op."""

    coord = coordinator_factory()
    manager = coord.discovery_snapshot
    snapshot = manager.capture()
    manager._last_persisted_signature = manager._snapshot_signature(
        snapshot
    )  # noqa: SLF001
    manager.schedule_save()

    assert manager._persisted_revision == manager._revision  # noqa: SLF001
    assert manager._pending_snapshot is None  # noqa: SLF001
