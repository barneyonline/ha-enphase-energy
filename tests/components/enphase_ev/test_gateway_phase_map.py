"""Tests for multi-gateway phase-map topology support."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.inventory_runtime import (
    GATEWAY_PHASE_MAP_CACHE_TTL,
    GATEWAY_PHASE_MAP_FAILURE_BACKOFF_S,
    InventoryRuntime,
)
from custom_components.enphase_ev.sensor import EnphaseGatewayConnectivityStatusSensor

PHASE_MAP = {
    "GW-1": {
        "isPrimaryGateway": False,
        "isDefaultGateway": False,
        "isProductionOnly": True,
        "isConsumptionOnly": False,
        "isSplitPhase": False,
        "totalPhase": 1,
        "hasIqBattery": False,
        "hasEncharge": False,
        "showStorage": False,
        "gatewayStatus": "normal",
        "phases": {"0": True, "1": False},
        "any": True,
        "all": False,
    },
    "GW-2": {
        "isPrimaryGateway": True,
        "isDefaultGateway": True,
        "isProductionOnly": False,
        "isConsumptionOnly": False,
        "isSplitPhase": False,
        "totalPhase": 3,
        "hasIqBattery": True,
        "hasEncharge": True,
        "showStorage": True,
        "hasEnpower": True,
        "isEnsemble": True,
        "gatewayStatus": "normal",
        "phases": {"0": True, "1": True, "2": True},
        "any": True,
        "all": True,
    },
}


def _set_gateway_members(coord) -> None:
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "type_key": "envoy",
            "type_label": "IQ Gateway",
            "count": 2,
            "devices": [
                {"name": "Gateway One", "serial_number": "GW-1"},
                {"name": "Gateway Two", "serial_number": "GW-2"},
            ],
        }
    }
    coord._type_device_order = ["envoy"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001


def test_normalize_gateway_phase_map() -> None:
    normalized = InventoryRuntime._normalize_gateway_phase_map(
        PHASE_MAP
    )  # noqa: SLF001

    assert normalized["GW-2"] == {
        "has_iq_battery": True,
        "is_split_phase": False,
        "is_production_only": False,
        "is_consumption_only": False,
        "has_enpower": True,
        "has_encharge": True,
        "show_storage": True,
        "is_ensemble": True,
        "is_default_gateway": True,
        "is_primary_gateway": True,
        "any_phase": True,
        "all_phases": True,
        "total_phase": 3,
        "gateway_status": "normal",
        "phases": {"0": True, "1": True, "2": True},
    }


def test_normalize_gateway_phase_map_skips_invalid_values() -> None:
    class _BadText:
        def __str__(self) -> str:
            raise RuntimeError("bad")

    payload = {
        _BadText(): {},
        "": {},
        "GW": {
            "totalPhase": "bad",
            "gatewayStatus": " ",
            "phases": {"0": "yes", _BadText(): True, "1": "unknown"},
        },
        "BAD": [],
    }

    assert InventoryRuntime._normalize_gateway_phase_map(payload) == {  # noqa: SLF001
        "GW": {"phases": {"0": True}}
    }
    assert InventoryRuntime._normalize_gateway_phase_map([]) == {}  # noqa: SLF001


def test_phase_map_prefers_primary_gateway_for_inventory_and_grid_control(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _set_gateway_members(coord)
    coord.inventory_runtime._gateway_phase_map = (  # noqa: SLF001
        coord.inventory_runtime._normalize_gateway_phase_map(PHASE_MAP)  # noqa: SLF001
    )

    assert coord.inventory_view.primary_gateway_serial() == "GW-2"
    assert coord.battery_runtime.grid_envoy_serial() == "GW-2"


def test_phase_map_falls_back_to_default_then_single_gateway(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _set_gateway_members(coord)
    coord.inventory_runtime._gateway_phase_map = {  # noqa: SLF001
        "GW-1": {"is_default_gateway": True},
        "GW-2": {"is_primary_gateway": False},
    }
    assert coord.inventory_view.primary_gateway_serial() == "GW-1"

    coord.inventory_runtime._gateway_phase_map = {  # noqa: SLF001
        "GW-2": {"total_phase": 3}
    }
    assert coord.inventory_runtime.gateway_phase_map_preferred_serial() == "GW-2"


def test_gateway_phase_map_summary_and_sensor_attributes(coordinator_factory) -> None:
    coord = coordinator_factory()
    _set_gateway_members(coord)
    coord.inventory_runtime._gateway_phase_map = (  # noqa: SLF001
        coord.inventory_runtime._normalize_gateway_phase_map(PHASE_MAP)  # noqa: SLF001
    )

    summary = coord.inventory_runtime.gateway_phase_map_summary()
    assert summary == {
        "gateway_count": 2,
        "multi_gateway": True,
        "primary_gateway_serial": "GW-2",
        "default_gateway_serial": "GW-2",
        "preferred_gateway_serial": "GW-2",
        "preferred_gateway_phase_count": 3,
        "split_phase_gateway_count": 0,
        "three_phase_gateway_count": 1,
        "production_only_gateway_count": 1,
        "consumption_only_gateway_count": 0,
        "storage_gateway_count": 1,
    }

    sensor = EnphaseGatewayConnectivityStatusSensor(coord)
    attributes = sensor.extra_state_attributes
    assert attributes["gateway_count"] == 2
    assert attributes["multi_gateway"] is True
    assert attributes["preferred_gateway_serial"] == "GW-2"
    assert attributes["preferred_gateway_phase_count"] == 3
    assert attributes["storage_gateway_count"] == 1


def test_gateway_phase_map_accessors_return_defensive_copies(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.inventory_runtime._gateway_phase_map = {  # noqa: SLF001
        "GW-1": {"phases": {"0": True}}
    }

    copied = coord.inventory_runtime.gateway_phase_map()
    copied["GW-1"]["total_phase"] = 3

    assert coord.inventory_runtime.gateway_phase_map_for_serial("GW-1") == {
        "phases": {"0": True}
    }
    assert coord.inventory_runtime.gateway_phase_map_for_serial("missing") is None
    assert coord.inventory_runtime.gateway_phase_map_for_serial(None) is None

    coord.inventory_runtime._gateway_phase_map = "bad"  # type: ignore[assignment]  # noqa: SLF001
    assert coord.inventory_runtime.gateway_phase_map() == {}
    coord.inventory_runtime._gateway_phase_map = {"GW-1": "bad"}  # type: ignore[dict-item]  # noqa: SLF001
    assert coord.inventory_runtime.gateway_phase_map() == {}


@pytest.mark.asyncio
async def test_refresh_gateway_phase_map_success(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    fetcher = AsyncMock(return_value=PHASE_MAP)
    coord.client = SimpleNamespace(phase_map_multiple_envoy=fetcher)
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.time.monotonic", lambda: 100.0
    )

    await coord.inventory_runtime._async_refresh_gateway_phase_map()  # noqa: SLF001

    fetcher.assert_awaited_once_with()
    assert coord.inventory_runtime.gateway_phase_map_preferred_serial() == "GW-2"
    assert coord._gateway_phase_map_cache_until == (  # noqa: SLF001
        100.0 + GATEWAY_PHASE_MAP_CACHE_TTL
    )
    assert coord._gateway_phase_map_failure_backoff_until is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_gateway_phase_map_preserves_stale_on_error(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.inventory_runtime._gateway_phase_map = {  # noqa: SLF001
        "GW-OLD": {"is_default_gateway": True}
    }
    coord.client = SimpleNamespace(
        phase_map_multiple_envoy=AsyncMock(side_effect=RuntimeError("network"))
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.time.monotonic", lambda: 200.0
    )

    await coord.inventory_runtime._async_refresh_gateway_phase_map()  # noqa: SLF001

    assert coord.inventory_runtime.gateway_phase_map_preferred_serial() == "GW-OLD"
    assert coord._gateway_phase_map_failure_backoff_until == (  # noqa: SLF001
        200.0 + GATEWAY_PHASE_MAP_FAILURE_BACKOFF_S
    )


@pytest.mark.asyncio
async def test_refresh_gateway_phase_map_invalid_payload_sets_backoff(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(
        phase_map_multiple_envoy=AsyncMock(return_value=["bad"])
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.time.monotonic", lambda: 300.0
    )

    await coord.inventory_runtime._async_refresh_gateway_phase_map()  # noqa: SLF001

    assert coord._gateway_phase_map_failure_backoff_until == (  # noqa: SLF001
        300.0 + GATEWAY_PHASE_MAP_FAILURE_BACKOFF_S
    )


@pytest.mark.asyncio
async def test_inventory_refresh_runs_due_phase_map_inside_inventory_cache(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    phase_fetcher = AsyncMock(return_value=PHASE_MAP)
    inventory_fetcher = AsyncMock(return_value={"result": []})
    coord.client = SimpleNamespace(
        phase_map_multiple_envoy=phase_fetcher,
        devices_inventory=inventory_fetcher,
    )
    coord.inventory_runtime._devices_inventory_cache_until = 1000.0  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.time.monotonic", lambda: 100.0
    )

    await coord.inventory_runtime._async_refresh_devices_inventory()  # noqa: SLF001

    phase_fetcher.assert_awaited_once_with()
    inventory_fetcher.assert_not_awaited()


def test_devices_inventory_refresh_due_when_only_phase_map_is_due(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.client = SimpleNamespace(phase_map_multiple_envoy=AsyncMock())
    coord.inventory_runtime._devices_inventory_cache_until = 1000.0  # noqa: SLF001
    coord.inventory_runtime._gateway_phase_map_cache_until = None  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.time.monotonic", lambda: 100.0
    )
    monkeypatch.setattr(coord, "_endpoint_family_should_run", lambda *_a, **_k: False)

    assert coord.inventory_runtime.devices_inventory_refresh_due() is True


def test_gateway_phase_map_refresh_due_cache_backoff_and_missing_fetcher(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.time.monotonic", lambda: 100.0
    )
    coord.client = SimpleNamespace()
    assert coord.inventory_runtime.gateway_phase_map_refresh_due() is False

    coord.client = SimpleNamespace(phase_map_multiple_envoy=AsyncMock())
    coord.inventory_runtime._gateway_phase_map_cache_until = 101.0  # noqa: SLF001
    assert coord.inventory_runtime.gateway_phase_map_refresh_due() is False

    coord.inventory_runtime._gateway_phase_map_cache_until = None  # noqa: SLF001
    coord.inventory_runtime._gateway_phase_map_failure_backoff_until = (  # noqa: SLF001
        101.0
    )
    assert coord.inventory_runtime.gateway_phase_map_refresh_due() is False
    assert coord.inventory_runtime.gateway_phase_map_refresh_due(force=True) is True
