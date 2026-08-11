from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.enphase_ev import api, inventory_runtime as inventory_runtime_mod
from custom_components.enphase_ev.inventory_runtime import (
    DEVICES_INVENTORY_CACHE_TTL,
    HEMS_DEVICES_CACHE_TTL,
    HEMS_DEVICES_STALE_AFTER_S,
    HEMS_INVENTORY_ENDPOINT_FAMILY,
    InventoryRuntime,
)


def _clear_hems_inventory_endpoint_family(coord) -> None:
    health = coord._endpoint_family_state(
        HEMS_INVENTORY_ENDPOINT_FAMILY
    )  # noqa: SLF001
    health.consecutive_failures = 0
    health.last_success_utc = None
    health.last_success_mono = None
    health.last_failure_utc = None
    health.last_status = None
    health.next_retry_mono = None
    health.next_retry_utc = None
    health.cooldown_active = False
    health.support_state = "unknown"
    health.last_error = None


def test_inventory_runtime_helper_paths(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert runtime._router_record_key("bad") is None  # noqa: SLF001
    assert runtime._router_record_key({"key": None}) is None  # noqa: SLF001
    assert runtime._router_record_key({"key": BadStr()}) is None  # noqa: SLF001
    assert runtime._router_record_key({"key": " router "}) == "router"  # noqa: SLF001

    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {"type_key": "envoy", "count": 1}
    }
    assert runtime._summary_type_bucket_source("envoy") == {  # noqa: SLF001
        "type_key": "envoy",
        "count": 1,
    }

    grouped = {"envoy": {"count": 1}}
    ordered = ["envoy"]
    snapshot = runtime.topology_snapshot()

    assert runtime._debug_devices_inventory_summary(
        grouped, ordered
    ) == {  # noqa: SLF001
        "ordered_type_keys": ["envoy"],
        "type_count": 1,
        "types": {"envoy": {"count": 1, "field_keys": []}},
    }
    hems_summary = runtime._debug_hems_inventory_summary()  # noqa: SLF001
    assert hems_summary["site_supported"] is None
    assert hems_summary["router_count"] == 0
    assert runtime._debug_system_dashboard_summary({}, {}, {}, {}) == {  # noqa: SLF001
        "tree_keys": [],
        "hierarchy_total_nodes": 0,
        "hierarchy_counts_by_type": {},
        "types": {},
    }
    assert runtime._debug_topology_summary(snapshot) == {  # noqa: SLF001
        "inventory_ready": False,
        "devices_inventory_ready": True,
        "hems_inventory_ready": False,
        "charger_count": 0,
        "battery_count": 0,
        "ac_battery_count": 0,
        "inverter_count": 0,
        "inverter_telemetry_count": 0,
        "active_type_keys": [],
        "gateway_iq_router_count": 0,
        "site_energy_channels": [],
    }
    assert runtime._build_system_dashboard_summaries(None, {}) == (  # noqa: SLF001
        {
            "envoy": {"hierarchy": {"count": 0, "relationships": []}},
            "encharge": {"hierarchy": {"count": 0, "relationships": []}},
            "microinverter": {"hierarchy": {"count": 0, "relationships": []}},
        },
        {"total_nodes": 0, "counts_by_type": {}, "relationships": []},
        {},
    )
    assert runtime._coerce_optional_bool("true") is True  # noqa: SLF001
    assert (
        runtime._normalize_inverter_status("unpaired") == "not_reporting"
    )  # noqa: SLF001
    assert runtime._normalize_inverter_status("pending") == "warning"  # noqa: SLF001

    router_records = runtime._gateway_iq_energy_router_summary_records(  # noqa: SLF001
        [{"name": "Router"}, {"name": "Router"}]
    )
    assert [record["key"] for record in router_records] == [
        "name_router",
        "name_router_2",
    ]
    assert runtime.system_dashboard_battery_detail("") is None  # noqa: SLF001

    runtime.__dict__["_inverter_data"] = {
        "INV-LOCAL": {"serial_number": "INV-LOCAL"}
    }  # noqa: SLF001
    assert runtime._coordinator_backed_attr("_inverter_data") == {  # noqa: SLF001
        "INV-LOCAL": {"serial_number": "INV-LOCAL"}
    }
    runtime.__dict__.pop("_inverter_data", None)
    coord._inverter_data = {
        "INV-FALLBACK": {"serial_number": "INV-FALLBACK"}
    }  # noqa: SLF001
    assert runtime.inverter_data("INV-FALLBACK") == {  # noqa: SLF001
        "serial_number": "INV-FALLBACK"
    }


def test_inventory_runtime_hems_refresh_floor_falls_back_on_bad_runtime_value(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.heatpump_runtime.hems_refresh_floor_s = lambda: "bad"  # type: ignore[method-assign]  # noqa: SLF001

    assert runtime._hems_refresh_floor_s() == 30.0  # noqa: SLF001
    assert HEMS_DEVICES_CACHE_TTL == 60.0
    assert runtime._hems_devices_cache_ttl_s() == 60.0  # noqa: SLF001
    assert DEVICES_INVENTORY_CACHE_TTL == 600.0


def test_inventory_runtime_summary_and_inverter_helper_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime
    coord.inventory_view.type_bucket = lambda type_key: {  # type: ignore[assignment]
        "envoy": {
            "count": 3,
            "devices": [
                {
                    "name": "Gateway A",
                    "statusText": "online",
                    "connected": "yes",
                    "model": "IQ Gateway",
                    "envoy_sw_version": "8.2.0",
                    "ip_address": "192.0.2.10",
                    "last_report": "2026-02-15T10:00:00Z",
                },
                {
                    "name": "Gateway B",
                    "status": "offline",
                    "connected": "no",
                    "ip_address": "192.0.2.11",
                },
                {
                    "name": "Gateway C",
                    "status": "mystery",
                    "connected": "maybe",
                },
            ],
        },
        "microinverter": {
            "count": 3,
            "devices": [],
            "status_counts": {"total": 3, "unknown": 1},
        },
    }.get(type_key, {})

    gateway_snapshot = runtime._build_gateway_inventory_summary()  # noqa: SLF001
    assert gateway_snapshot["connected_devices"] == 1
    assert gateway_snapshot["disconnected_devices"] == 1
    assert gateway_snapshot["unknown_connection_devices"] == 1
    assert gateway_snapshot["ip_address"] == "192.0.2.10"
    assert (
        runtime._gateway_summary_ip_address(  # noqa: SLF001
            [{"name": "Production Meter", "ip_address": "192.0.2.12"}],
            None,
        )
        is None
    )
    assert (
        runtime._gateway_summary_ip_address(  # noqa: SLF001
            [{"name": "Unknown Device", "ip_address": "192.0.2.12"}],
            None,
        )
        == "192.0.2.12"
    )
    assert (
        runtime._gateway_summary_ip_address(  # noqa: SLF001
            [
                {
                    "name": "Production Meter",
                    "channel_type": "production_meter",
                    "show_connection_details": True,
                    "ip_address": "192.0.2.13",
                },
                {
                    "name": "System Controller",
                    "show_connection_details": True,
                    "ip_address": "192.0.2.14",
                },
                {
                    "name": "Communications device",
                    "show_connection_details": True,
                    "ip_address": "192.0.2.15",
                },
            ],
            None,
        )
        == "192.0.2.15"
    )

    micro_snapshot = runtime._build_microinverter_inventory_summary()  # noqa: SLF001
    assert micro_snapshot["connectivity_state"] == "degraded"

    coord.energy._site_energy_meta = {}  # noqa: SLF001
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"lifetime_query_start_date": "2022-08-10"},
        "INV-B": {"lifetime_query_start_date": "2023-01-01"},
        "INV-C": {"lifetime_query_start_date": "not-a-date"},
    }
    assert runtime._inverter_start_date() == "2022-08-10"  # noqa: SLF001

    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"sku_id": "IQ7A-SKU"}],
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
            "extra_list": ["a", "b"],
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    coord.inventory_view.type_bucket = type(coord.inventory_view).type_bucket.__get__(coord.inventory_view, type(coord.inventory_view))  # type: ignore[method-assign]
    bucket = coord.inventory_view.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["extra_list"] == ["a", "b"]

    info = coord.inventory_view.type_device_info("microinverter")
    assert info is not None
    assert info["hw_version"] == "IQ7A-SKU"
    assert info.get("model_id") is None
    assert coord.inventory_view.type_device_model_id("microinverter") is None
    assert coord.inventory_view.type_device_model(None) is None
    assert coord.inventory_view.type_device_serial_number(None) is None
    assert coord.inventory_view.type_device_model_id(None) is None
    assert coord.inventory_view.type_device_sw_version(None) is None
    assert coord.inventory_view.type_device_hw_version(None) is None
    coord._type_device_buckets = {"microinverter": "bad"}  # noqa: SLF001
    assert coord.inventory_view.type_device_hw_version("microinverter") is None

    coord.inventory_view.type_bucket = lambda _key: {"devices": "bad"}  # type: ignore[assignment]
    assert coord.inventory_view._type_bucket_members("envoy") == []  # noqa: SLF001
    coord.inventory_view.type_bucket = type(coord.inventory_view).type_bucket.__get__(coord.inventory_view, type(coord.inventory_view))  # type: ignore[method-assign]

    class BadText:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert (
        coord.inventory_view._type_member_text({"name": BadText()}, "name") is None
    )  # noqa: SLF001
    assert (
        coord.inventory_view._type_summary_from_values(
            [None, BadText(), "  ", "A", "A"]
        )  # noqa: SLF001
        == "A x2"
    )

    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"sku_id": "IQ8M"}],
            "firmware_summary": "4.0 x1",
        }
    }
    assert coord.inventory_view.type_device_sw_version("microinverter") is None
    coord._type_device_buckets = {  # noqa: SLF001
        "encharge": {
            "type_key": "encharge",
            "type_label": "Battery",
            "count": 3,
            "devices": [
                {"serial_number": "BAT-1", "sw_version": "1.0"},
                {"serial_number": "BAT-2", "sw_version": "1.0"},
                {"serial_number": "BAT-3", "sw_version": "2.0"},
            ],
        },
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 3,
            "devices": [
                {"serial_number": "INV-1", "fw1": "4.0"},
                {"serial_number": "INV-2", "fw1": "4.0"},
                {"serial_number": "INV-3", "fw2": "5.0"},
            ],
        },
    }
    battery_info = coord.inventory_view.type_device_info("encharge")
    inverter_info = coord.inventory_view.type_device_info("microinverter")
    assert battery_info is not None
    assert inverter_info is not None
    assert battery_info["sw_version"] == "1.0 x2, 2.0 x1"
    assert inverter_info["sw_version"] == "4.0 x2, 5.0 x1"
    coord._type_device_buckets = {"encharge": "bad"}  # noqa: SLF001
    assert coord.inventory_view.type_device_hw_version("encharge") is None
    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "type_key": "envoy",
            "type_label": "Gateway",
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
        }
    }
    assert coord.inventory_view.type_device_hw_version("envoy") is None

    coord._inverter_data = None  # type: ignore[assignment]  # noqa: SLF001
    assert coord.iter_inverter_serials() == []
    assert coord.inverter_data("INV-A") is None

    class BadSerial:
        def __str__(self) -> str:
            raise ValueError("bad")

    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    assert coord.inverter_data(BadSerial()) is None
    assert coord.inverter_data("") is None


def test_microinverter_summary_uses_newest_valid_member_timestamp(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime
    bucket = {
        "count": 3,
        "status_counts": {"total": 3, "normal": 3},
        "latest_reported_utc": "2026-07-12T06:00:00Z",
        "latest_reported_device": {
            "serial_number": "TOPOLOGY",
            "name": "Topology summary",
            "status": "normal",
        },
        "devices": [
            {
                "serial_number": "INV-A",
                "last_report": "not-a-timestamp",
            },
            {
                "serial_number": "INV-B",
                "name": "Roof B",
                "statusText": "Normal",
                "last_reported_at": "2026-07-12T10:00:00Z",
            },
            {
                "serial_number": "INV-C",
                "name": "Roof C",
                "status": "normal",
                "last-report": "2026-07-12T11:00:00Z",
            },
        ],
    }
    coord._type_device_buckets = {"microinverter": bucket}  # noqa: SLF001

    snapshot = runtime._build_microinverter_inventory_summary()  # noqa: SLF001

    assert snapshot["latest_reported_utc"] == "2026-07-12T11:00:00+00:00"
    assert snapshot["latest_reported_device"] == {
        "serial_number": "INV-C",
        "name": "Roof C",
        "status": "normal",
    }

    bucket["latest_reported_utc"] = "2026-07-12T12:00:00Z"
    snapshot = runtime._build_microinverter_inventory_summary()  # noqa: SLF001

    assert snapshot["latest_reported_utc"] == "2026-07-12T12:00:00+00:00"
    assert snapshot["latest_reported_device"] == {
        "serial_number": "TOPOLOGY",
        "name": "Topology summary",
        "status": "normal",
    }


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_preserves_previous_lifetime_on_regression(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "serial_number": "INV-A",
            "inverter_id": "1001",
            "device_id": 11,
            "status_code": "normal",
            "show_sig_str": False,
            "emu_version": "8.3.5232",
            "issi": {"sig_str": 1},
            "rssi": {"sig_str": 2},
            "lifetime_production_wh": 2_000_000.0,
            "lifetime_query_start_date": "2022-08-10",
            "lifetime_query_end_date": "2026-02-09",
        }
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "not_reporting": 0,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "inverters": [
                {
                    "name": "IQ7A",
                    "array_name": "North",
                    "serial_number": "INV-A",
                    "status": "normal",
                    "statusText": "Normal",
                }
            ],
        }
    )
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": 1_000_000}}
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    payload = coord.inverter_data("INV-A")
    assert payload is not None
    assert payload["inverter_id"] == "1001"
    assert payload["device_id"] == 11
    assert payload["lifetime_production_wh"] == 2_000_000.0
    assert payload["lifetime_query_start_date"] == "2022-08-10"
    assert payload["lifetime_query_end_date"] == "2026-02-09"


def test_inventory_runtime_summary_helpers_reuse_stable_cache_markers(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
            },
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 1,
                "devices": [{"serial_number": "INV-1", "name": "Inverter"}],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"serial_number": "HP-1", "name": "Heat Pump"}],
            },
        },
        ["envoy", "microinverter", "heatpump"],
    )
    coord._system_dashboard_devices_details_raw = {  # noqa: SLF001
        "envoy": {"envoy": {"status": "normal"}}
    }
    coord._hems_devices_payload = {"result": {"devices": []}}  # noqa: SLF001

    gateway_builder = MagicMock(return_value={"gateway": 1})
    micro_builder = MagicMock(return_value={"micro": 1})
    heatpump_builder = MagicMock(return_value={"heatpump": 1})
    heatpump_type_builder = MagicMock(return_value={"HEAT_PUMP": {"count": 1}})

    monkeypatch.setattr(runtime, "_build_gateway_inventory_summary", gateway_builder)
    monkeypatch.setattr(
        runtime, "_build_microinverter_inventory_summary", micro_builder
    )
    monkeypatch.setattr(runtime, "_build_heatpump_inventory_summary", heatpump_builder)
    monkeypatch.setattr(
        runtime, "_build_heatpump_type_summaries", heatpump_type_builder
    )

    assert coord.gateway_inventory_summary() == {"gateway": 1}
    assert coord.gateway_inventory_summary() == {"gateway": 1}
    assert coord.microinverter_inventory_summary() == {"micro": 1}
    assert coord.microinverter_inventory_summary() == {"micro": 1}
    assert coord.heatpump_inventory_summary() == {"heatpump": 1}
    assert coord.heatpump_inventory_summary() == {"heatpump": 1}
    assert coord.heatpump_type_summary("heat_pump") == {"count": 1}
    assert coord.heatpump_type_summary("heat_pump") == {"count": 1}

    assert gateway_builder.call_count == 1
    assert micro_builder.call_count == 1
    assert heatpump_builder.call_count == 1
    assert heatpump_type_builder.call_count == 1


def test_inventory_runtime_debug_cache_and_gateway_fallback_edges(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime

    class BadText:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert runtime._debug_sorted_keys({BadText(): 1, " ok ": 2}) == [
        "ok"
    ]  # noqa: SLF001
    assert runtime._debug_field_keys([{"a": 1}, "bad"]) == ["a"]  # noqa: SLF001
    rendered = runtime._debug_render_summary({"bad": object()})  # noqa: SLF001
    assert isinstance(rendered, str)
    assert "'bad':" in rendered

    monkeypatch.setattr(
        runtime,
        "_hems_grouped_devices",
        lambda: ["bad", {BadText(): []}, {"router": []}],
    )
    monkeypatch.setattr(runtime, "_hems_group_members", lambda *_args: [])
    hems_summary = runtime._debug_hems_inventory_summary()  # noqa: SLF001
    assert hems_summary["router_count"] == 0

    coord.energy = None
    assert coord.discovery_snapshot.live_site_energy_channels() == set()
    coord.energy = SimpleNamespace(
        site_energy={BadText(): 1, " grid_import ": 1},
        site_energy_meta={
            "bucket_lengths": {
                BadText(): 2,
                "": 1,
                "heatpump": "2",
                "evse": 0,
                "ignored": [],
                "battery_discharge": "bad",
                "consumption": False,
            }
        },
    )
    assert coord.discovery_snapshot.live_site_energy_channels() == {
        "battery_discharge",
        "grid_import",
        "heat_pump",
    }

    monkeypatch.setattr(runtime, "_gateway_inventory_summary_marker", lambda: "gw")
    monkeypatch.setattr(
        runtime, "_microinverter_inventory_summary_marker", lambda: "mi"
    )
    monkeypatch.setattr(runtime, "_heatpump_inventory_summary_marker", lambda: "hp")
    monkeypatch.setattr(
        runtime, "_gateway_iq_energy_router_records_marker", lambda: "router"
    )
    gateway_builder = MagicMock(return_value={"gateway": 1})
    micro_builder = MagicMock(return_value={"micro": 1})
    heatpump_builder = MagicMock(return_value={"heatpump": 1})
    heatpump_type_builder = MagicMock(return_value={"HEAT_PUMP": {"count": 1}})
    router_builder = MagicMock(return_value=[{"key": "router-1", "name": "Router"}])
    monkeypatch.setattr(runtime, "_build_gateway_inventory_summary", gateway_builder)
    monkeypatch.setattr(
        runtime, "_build_microinverter_inventory_summary", micro_builder
    )
    monkeypatch.setattr(runtime, "_build_heatpump_inventory_summary", heatpump_builder)
    monkeypatch.setattr(
        runtime, "_build_heatpump_type_summaries", heatpump_type_builder
    )
    monkeypatch.setattr(
        runtime, "_gateway_iq_energy_router_summary_records", router_builder
    )
    monkeypatch.setattr(
        runtime,
        "gateway_iq_energy_router_records",
        lambda: [{"serial_number": "router-1"}],
    )

    assert runtime.gateway_inventory_summary() == {"gateway": 1}
    assert runtime.gateway_inventory_summary() == {"gateway": 1}
    assert runtime.microinverter_inventory_summary() == {"micro": 1}
    assert runtime.microinverter_inventory_summary() == {"micro": 1}
    assert runtime.heatpump_inventory_summary() == {"heatpump": 1}
    assert runtime.heatpump_inventory_summary() == {"heatpump": 1}
    assert runtime.heatpump_type_summary(BadText()) == {}
    assert runtime.heatpump_type_summary("heat_pump") == {"count": 1}
    assert runtime.gateway_iq_energy_router_summary_records() == [
        {"key": "router-1", "name": "Router"}
    ]
    assert runtime.gateway_iq_energy_router_summary_records() == [
        {"key": "router-1", "name": "Router"}
    ]
    assert runtime.gateway_iq_energy_router_record(" router-1 ") == {
        "key": "router-1",
        "name": "Router",
    }
    assert runtime.gateway_iq_energy_router_record(BadText()) is None
    assert runtime.gateway_iq_energy_router_record("   ") is None

    assert gateway_builder.call_count == 1
    assert micro_builder.call_count == 1
    assert heatpump_builder.call_count == 1
    assert heatpump_type_builder.call_count == 1
    assert router_builder.call_count >= 1

    monkeypatch.setattr(
        runtime,
        "_build_gateway_inventory_summary",
        type(runtime)._build_gateway_inventory_summary.__get__(runtime, type(runtime)),
    )

    monkeypatch.setattr(
        runtime,
        "type_bucket",
        lambda _key: {"type_key": "envoy", "count": "bad", "devices": []},
    )
    monkeypatch.setattr(
        runtime,
        "system_dashboard_envoy_detail",
        lambda: {
            "name": "Gateway",
            "serial_number": "GW-1",
            "status": "normal",
            "firmware_version": "8.2.0",
            "last_interval_end_date": "2026-02-15T10:00:00Z",
        },
    )
    gateway_summary = runtime._build_gateway_inventory_summary()  # noqa: SLF001
    assert gateway_summary["firmware_summary"] == "8.2.0 x1"
    assert gateway_summary["latest_reported_device"] == {
        "name": "Gateway",
        "serial_number": "GW-1",
        "status": "normal",
    }

    monkeypatch.setattr(
        runtime,
        "type_bucket",
        lambda _key: {
            "type_key": "envoy",
            "count": 1,
            "devices": [{"name": "Online Gateway", "status": "normal"}],
        },
    )
    monkeypatch.setattr(runtime, "system_dashboard_envoy_detail", lambda: None)
    online_summary = runtime._build_gateway_inventory_summary()  # noqa: SLF001
    assert online_summary["connected_devices"] == 1

    monkeypatch.setattr(
        runtime,
        "type_bucket",
        lambda _key: {
            "type_key": "envoy",
            "count": 1,
            "devices": [{"name": "Offline Gateway", "status": "not_reporting"}],
        },
    )
    monkeypatch.setattr(runtime, "system_dashboard_envoy_detail", lambda: None)
    offline_summary = runtime._build_gateway_inventory_summary()  # noqa: SLF001
    assert offline_summary["disconnected_devices"] == 1


def test_inventory_runtime_debug_summary_helpers_cover_optional_counts(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    device_summary = runtime._debug_devices_inventory_summary(  # noqa: SLF001
        {
            "envoy": {
                "devices": [{"serial": "1"}],
                "count": 0,
                "status_counts": {"online": "2"},
                "device_type_counts": {"gateway": "1"},
            },
            "skip": "bad",
        },
        ["envoy", "skip"],
    )
    assert device_summary["types"]["envoy"]["status_counts"] == {"online": 2}
    assert device_summary["types"]["envoy"]["device_type_counts"] == {"gateway": 1}

    monkeypatch.setattr(
        runtime,
        "_hems_grouped_devices",
        lambda: ["skip", {"router": []}],
    )
    monkeypatch.setattr(runtime, "_hems_group_members", lambda *_args: [])
    monkeypatch.setattr(
        runtime,
        "_build_heatpump_inventory_summary",
        lambda: {"total_devices": 0},
    )
    monkeypatch.setattr(
        runtime,
        "gateway_iq_energy_router_summary_records",
        lambda: [],
    )
    hems_summary = runtime._debug_hems_inventory_summary()  # noqa: SLF001
    assert hems_summary["group_keys"] == ["router"]
    assert hems_summary["router_count"] == 0
    assert runtime._heatpump_member_device_type({"device_type": "heat_pump"}) == (
        "HEAT_PUMP"
    )  # noqa: SLF001
    assert runtime._heatpump_worst_status_text({"not_reporting": 1}) == (
        "Not Reporting"
    )  # noqa: SLF001
    assert (
        runtime._debug_topology_summary(runtime.topology_snapshot())["charger_count"]
        == 0
    )  # noqa: SLF001
    dashboard_summary = runtime._debug_system_dashboard_summary(  # noqa: SLF001
        {},
        {"envoy": {"envoys": {}}},
        {
            "envoy": {
                "hierarchy": {"count": "2"},
                "counts_by_type": {"gateway": "1"},
                "status_counts": {"normal": "3"},
            }
        },
        {"total_nodes": 1, "counts_by_type": {"envoy": 1}},
    )
    assert dashboard_summary["types"]["envoy"]["counts_by_type"] == {"gateway": 1}
    assert dashboard_summary["types"]["envoy"]["status_counts"] == {"normal": 3}


def test_devices_inventory_runtime_parser_shapes_and_buckets(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import inventory_runtime as inv_mod

    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    payload = {
        "result": [
            {
                "type": "wind-turbine",
                "devices": [
                    {"name": "Wind 1", "status": "normal"},
                    {"name": "Retired Wind", "statusText": "Retired"},
                ],
            },
            {
                "type": "encharge",
                "devices": [
                    {"serial_number": "BAT-1", "name": "IQ Battery 5P"},
                    {"serial_number": "BAT-2", "name": "IQ Battery 5P"},
                    {"serial_number": "BAT-3", "name": "   "},
                ],
            },
            {
                "deviceType": "inverters",
                "members": [
                    {"serial_number": "INV-1", "name": "Micro 1"},
                    {"serial_number": "INV-2", "name": "Micro 2"},
                ],
            },
            {
                "device_type": "microinverter",
                "items": [{"serial_number": "INV-3", "name": "Micro 3"}],
            },
            {
                "type": "generator",
                "devices": [{"name": "Generator 1", "status": "RETIRED"}],
            },
        ]
    }

    valid, grouped, ordered = runtime._parse_devices_inventory_payload(
        payload
    )  # noqa: SLF001

    assert valid is True
    assert ordered == ["wind_turbine", "encharge", "microinverter", "generator"]
    runtime._set_type_device_buckets(grouped, ordered)  # noqa: SLF001

    assert coord.inventory_view.iter_type_keys() == [
        "wind_turbine",
        "encharge",
        "microinverter",
    ]
    assert coord.inventory_view.type_device_name("wind-turbine") == "Wind Turbine"
    assert coord.inventory_view.type_bucket("encharge")["count"] == 3
    assert (
        coord.inventory_view.type_bucket("encharge")["model_summary"]
        == "IQ Battery 5P x2"
    )
    assert coord.inventory_view.type_bucket("microinverter")["count"] == 3
    assert coord.inventory_view.has_type("generator") is False

    valid, grouped, ordered = runtime._parse_devices_inventory_payload(
        {
            "result": [
                {"type": "envoy", "devices": [{"serial_number": "GW-1"}]},
                {"type": "meter", "devices": [{"serial_number": "MTR-1"}]},
                {"type": "enpower", "devices": [{"serial_number": "SC-1"}]},
            ]
        }
    )
    assert valid is True
    assert ordered == ["envoy"]
    runtime._set_type_device_buckets(grouped, ordered)  # noqa: SLF001
    assert coord.inventory_view.type_bucket(
        "meter"
    ) == coord.inventory_view.type_bucket("envoy")
    assert coord.inventory_view.type_bucket(
        "enpower"
    ) == coord.inventory_view.type_bucket("envoy")

    class _BadName:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    class _WeirdSanitized:
        def get(self, key, default=None):
            if key == "name":
                return "Weird Battery"
            return default

    def _fake_sanitize(member):
        marker = member.get("name")
        if marker == "WEIRD_NON_DICT":
            return _WeirdSanitized()
        if marker == "WEIRD_BAD_STR":
            return {"name": _BadName()}
        return {"name": "IQ Battery 5P"}

    monkeypatch.setattr(inv_mod, "sanitize_member", _fake_sanitize)
    valid, grouped, _ordered = runtime._parse_devices_inventory_payload(
        {
            "result": [
                {
                    "type": "encharge",
                    "devices": [
                        {"name": "WEIRD_NON_DICT"},
                        {"name": "WEIRD_BAD_STR"},
                        {"name": "IQ Battery 5P"},
                    ],
                }
            ]
        }
    )
    assert valid is True
    assert grouped["encharge"]["model_summary"] == "IQ Battery 5P x1"


def test_inventory_view_iter_type_keys_infers_ac_battery_when_no_buckets(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.serials = set()
    coord.data = {}
    coord.iter_serials = lambda: []
    coord._type_device_order = None  # noqa: SLF001
    coord._type_device_buckets = None  # noqa: SLF001
    coord._selected_type_keys = None  # noqa: SLF001
    coord._battery_has_encharge = False  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001

    assert coord.inventory_view.iter_type_keys() == ["envoy", "ac_battery"]


def test_devices_inventory_runtime_dry_contact_dedupe_and_helpers(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import inventory_runtime as inv_mod

    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    valid, grouped, ordered = runtime._parse_devices_inventory_payload(
        {
            "result": [
                {
                    "type": "drycontactloads",
                    "devices": [
                        {"serial_number": "DRY-1", "name": "Inventory"},
                        {"serial_number": "DRY-1", "name": "Inventory"},
                        {"channel_type": "NC1", "meta": {"ignored": True}},
                        {"channel_type": "NC1", "meta": {"ignored": True}},
                        {"id": "2"},
                    ],
                }
            ]
        }
    )

    assert valid is True
    assert ordered == ["dry_contact"]
    assert grouped["dry_contact"]["devices"] == [
        {"name": "Inventory", "serial_number": "DRY-1"},
        {"channel_type": "NC1"},
        {"id": "2"},
    ]

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    values = iter(
        [
            {"name": "Inventory"},
            {"name": "Inventory"},
            {"nested": {"ignored": True}},
        ]
    )
    monkeypatch.setattr(inv_mod, "normalize_type_key", lambda _raw: "dry_contact")
    monkeypatch.setattr(inv_mod, "type_display_label", lambda _raw: "Dry Contacts")
    monkeypatch.setattr(inv_mod, "sanitize_member", lambda _member: next(values))

    valid, grouped, ordered = runtime._parse_devices_inventory_payload(
        {"result": [{"type": BadStr(), "devices": [{}, {}, {}]}]}
    )
    assert valid is True
    assert ordered == ["dry_contact"]
    assert grouped["dry_contact"]["devices"] == [
        {"name": "Inventory"},
        {"name": "Inventory"},
        {"nested": {"ignored": True}},
    ]

    assert runtime._parse_devices_inventory_payload("bad") == (
        False,
        {},
        [],
    )  # noqa: SLF001
    assert runtime._parse_devices_inventory_payload({}) == (
        False,
        {},
        [],
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_devices_and_hems_refresh_cache_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    runtime._devices_inventory_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.devices_inventory = AsyncMock(side_effect=AssertionError("no fetch"))
    await runtime._async_refresh_devices_inventory()

    runtime._devices_inventory_cache_until = None  # noqa: SLF001
    coord.client.devices_inventory = AsyncMock(return_value={})
    await runtime._async_refresh_devices_inventory()

    monkeypatch.setattr(
        inventory_runtime_mod, "redact_battery_payload", lambda payload: "raw"
    )
    coord.client.devices_inventory = AsyncMock(
        return_value={
            "result": [{"type": "envoy", "devices": [{"name": "IQ Gateway"}]}]
        }
    )
    await runtime._async_refresh_devices_inventory(force=True)
    assert runtime._devices_inventory_payload == {"value": "raw"}  # noqa: SLF001

    monkeypatch.setattr(
        inventory_runtime_mod, "redact_battery_payload", lambda payload: payload
    )
    await runtime._async_refresh_devices_inventory(force=True)
    assert coord.inventory_view.has_type("envoy") is True

    runtime._devices_inventory_cache_until = None  # noqa: SLF001
    coord.client.devices_inventory = AsyncMock(
        return_value={"result": [{"type": "envoy"}]}
    )
    monkeypatch.setattr(
        runtime,
        "_parse_devices_inventory_payload",
        lambda payload: (
            True,
            {"envoy": {"type_key": "envoy", "count": object(), "devices": [{}]}},
            ["envoy"],
        ),
    )
    await runtime._async_refresh_devices_inventory(force=True)
    assert runtime._devices_inventory_cache_until is not None  # noqa: SLF001

    runtime._hems_devices_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    await runtime._async_refresh_hems_devices()

    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = None
    await runtime._async_refresh_hems_devices()

    coord.client._hems_site_supported = False  # noqa: SLF001
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    await runtime._async_refresh_hems_devices()
    coord.client.hems_devices.assert_not_awaited()
    assert runtime._hems_devices_payload is None  # noqa: SLF001

    coord.client._hems_site_supported = None  # noqa: SLF001
    runtime._hems_support_preflight_cache_until = None  # noqa: SLF001
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value={"is_hems": False})
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    await runtime._async_refresh_hems_devices()
    assert coord.client.hems_site_supported is False

    coord.client._hems_site_supported = None  # noqa: SLF001
    runtime._hems_support_preflight_cache_until = None  # noqa: SLF001
    coord.client.system_dashboard_summary = AsyncMock(return_value=None)
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await runtime._async_refresh_hems_devices()
    assert runtime._hems_devices_payload is None  # noqa: SLF001

    _clear_hems_inventory_endpoint_family(coord)
    monkeypatch.setattr(
        inventory_runtime_mod, "redact_battery_payload", lambda payload: payload
    )
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(
        return_value={"data": {"hems-devices": {"heat-pump": []}}}
    )
    await runtime._async_refresh_hems_devices(force=True)
    assert runtime._hems_devices_payload == {
        "data": {"hems-devices": {"heat-pump": []}}
    }  # noqa: SLF001

    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await runtime._async_refresh_hems_devices()
    assert runtime._hems_devices_using_stale is True  # noqa: SLF001

    _clear_hems_inventory_endpoint_family(coord)
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=RuntimeError("boom"))
    await runtime._async_refresh_hems_devices(force=True)
    assert runtime._hems_devices_using_stale is True  # noqa: SLF001

    _clear_hems_inventory_endpoint_family(coord)
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    runtime._hems_devices_last_success_mono = (
        time.monotonic() - HEMS_DEVICES_STALE_AFTER_S - 1
    )  # noqa: SLF001
    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)
    await runtime._async_refresh_hems_devices()
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_devices_using_stale is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_hems_failure_enters_endpoint_family_cooldown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value=None)

    await runtime._async_refresh_hems_devices()

    health = coord._endpoint_family_state(
        HEMS_INVENTORY_ENDPOINT_FAMILY
    )  # noqa: SLF001
    assert health.cooldown_active is True
    assert health.next_retry_mono is not None
    assert runtime.hems_devices_refresh_due(force=True) is False


def test_inventory_runtime_hems_refresh_due_uses_cache(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._hems_devices_cache_until = time.monotonic() + 60  # noqa: SLF001

    assert runtime.hems_devices_refresh_due() is False


@pytest.mark.asyncio
async def test_inventory_runtime_hems_cooldown_reuses_stale_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    now = time.monotonic()
    runtime._hems_devices_payload = {"existing": True}  # noqa: SLF001
    runtime._hems_devices_last_success_mono = now  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("unused"))
    health = coord._endpoint_family_state(
        HEMS_INVENTORY_ENDPOINT_FAMILY
    )  # noqa: SLF001
    health.cooldown_active = True
    health.next_retry_mono = now + 300
    health.last_success_mono = now

    await runtime._async_refresh_hems_devices(force=True)

    coord.client.hems_devices.assert_not_awaited()
    assert runtime._hems_devices_payload == {"existing": True}  # noqa: SLF001
    assert runtime._hems_devices_using_stale is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_devices_inventory_early_return_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    coord._endpoint_family_should_run = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda family, force=False: False if family == "inventory_topology" else True
    )
    coord.client.devices_inventory = AsyncMock(side_effect=AssertionError("unused"))
    await runtime._async_refresh_devices_inventory()
    coord.client.devices_inventory.assert_not_awaited()

    coord._endpoint_family_should_run = lambda *args, **kwargs: True  # type: ignore[method-assign]  # noqa: SLF001
    coord.client.devices_inventory = None
    await runtime._async_refresh_devices_inventory()


def test_inventory_runtime_devices_inventory_refresh_due_respects_cache(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._devices_inventory_cache_until = 150.0  # noqa: SLF001
    runtime._gateway_phase_map_cache_until = 150.0  # noqa: SLF001
    coord.client.devices_inventory = AsyncMock(side_effect=AssertionError("unused"))
    monkeypatch.setattr(inventory_runtime_mod.time, "monotonic", lambda: 100.0)

    assert runtime.devices_inventory_refresh_due() is False


def test_inventory_runtime_inverter_helper_paths(coordinator_factory) -> None:
    runtime = coordinator_factory().inventory_runtime

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert (
        runtime._format_inverter_model_summary({"": 1, "IQ7A": "x", "IQ8": 0}) is None
    )  # noqa: SLF001
    assert runtime._normalize_inverter_status("normal") == "normal"  # noqa: SLF001
    assert runtime._normalize_inverter_status("recommended") == "normal"  # noqa: SLF001
    assert runtime._normalize_inverter_status("warning") == "warning"  # noqa: SLF001
    assert (
        runtime._normalize_inverter_status("critical error") == "error"
    )  # noqa: SLF001
    assert (
        runtime._normalize_inverter_status("not reporting") == "not_reporting"
    )  # noqa: SLF001
    assert runtime._normalize_inverter_status("mystery") == "unknown"  # noqa: SLF001
    assert runtime._normalize_inverter_status(BadStr()) == "unknown"  # noqa: SLF001
    assert (
        runtime._inverter_connectivity_state({"total": 2, "not_reporting": 0})
        == "online"
    )  # noqa: SLF001
    assert (
        runtime._inverter_connectivity_state({"total": 2, "not_reporting": 1})
        == "degraded"
    )  # noqa: SLF001
    assert (
        runtime._inverter_connectivity_state({"total": 2, "not_reporting": 2})
        == "offline"
    )  # noqa: SLF001
    assert runtime._inverter_connectivity_state({"total": 0}) is None  # noqa: SLF001
    assert runtime._parse_inverter_last_report(None) is None  # noqa: SLF001
    assert runtime._parse_inverter_last_report("   ") is None  # noqa: SLF001
    assert (
        runtime._parse_inverter_last_report("2026-02-09T00:00:00Z") is not None
    )  # noqa: SLF001
    assert (
        runtime._parse_inverter_last_report("2026-02-09T00:00:00Z[UTC]") is not None
    )  # noqa: SLF001
    assert (
        runtime._parse_inverter_last_report(1_780_000_000_000) is not None
    )  # noqa: SLF001
    assert (
        runtime._parse_inverter_last_report(datetime(2026, 2, 9, 0, 0, 0)).tzinfo
        == timezone.utc
    )  # noqa: SLF001
    assert runtime._parse_inverter_last_report(float("inf")) is None  # noqa: SLF001
    assert runtime._parse_inverter_last_report("bad") is None  # noqa: SLF001
    assert runtime._parse_inverter_last_report(BadStr()) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_paths(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    def _clear_family_windows() -> None:
        for family in ("inverter_inventory", "inverter_status", "inverter_production"):
            health = coord._endpoint_family_state(family)  # noqa: SLF001
            health.next_retry_mono = None
            health.next_retry_utc = None
            health.cooldown_active = False
        coord._inverters_inventory_cache_until = None  # noqa: SLF001
        coord._inverter_status_cache_until = None  # noqa: SLF001
        coord._inverter_production_cache_until = None  # noqa: SLF001

    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 2,
            "not_reporting": 0,
            "normal_count": 2,
            "warning_count": 0,
            "error_count": 0,
            "inverters": [
                {
                    "name": "IQ7A",
                    "array_name": "North",
                    "serial_number": "INV-A",
                    "status": "normal",
                    "statusText": "Normal",
                    "last_report": 1_780_000_000,
                    "fw1": "520-00082-r01-v04.30.32",
                },
                {
                    "name": "IQ7A",
                    "array_name": "West",
                    "serial_number": "INV-B",
                    "status": "normal",
                    "statusText": "Normal",
                    "last_report": 1_770_000_000,
                    "fw1": "520-00082-r01-v04.30.32",
                },
            ],
            "panel_info": {
                "pv_module_manufacturer": "Acme",
                "model_name": "PV-123",
                "stc_rating": 420,
            },
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {
                "serialNum": "INV-A",
                "deviceId": 11,
                "statusCode": "normal",
                "type": "IQ7A",
            },
            "1002": {
                "serialNum": "INV-B",
                "deviceId": 12,
                "statusCode": "normal",
                "type": "IQ7A",
            },
        }
    )
    coord.client.inverter_production = AsyncMock(
        return_value={
            "production": {"1001": 1_000_000, "1002": "2_000_000"},
            "start_date": "2022-08-10",
            "end_date": "2026-02-09",
        }
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    assert coord.iter_inverter_serials() == ["INV-A", "INV-B"]
    assert coord.inverter_data("INV-A")["inverter_id"] == "1001"
    assert coord.inverter_data("INV-A")["device_id"] == 11
    assert coord.inverter_data("INV-A")["lifetime_production_wh"] == 1_000_000.0
    bucket = coord.inventory_view.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["count"] == 2
    assert bucket["status_counts"]["normal"] == 2
    assert bucket["connectivity_state"] == "online"

    coord._inverter_data = []  # type: ignore[assignment]  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(return_value={"inverters": {"bad": 1}})
    coord.client.inverter_status = AsyncMock(side_effect=RuntimeError("boom"))
    coord.client.inverter_production = AsyncMock(side_effect=RuntimeError("boom"))
    _clear_family_windows()
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == []

    coord.energy._site_energy_meta = {}  # noqa: SLF001
    coord._inverter_data = {}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "normal_count": 1,
            "warning_count": 0,
            "error_count": 0,
            "not_reporting": 0,
            "inverters": [{"serial_number": "INV-A", "name": "IQ7A"}],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={"1001": {"serialNum": "INV-A", "deviceId": 11}}
    )
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": 1}}
    )
    _clear_family_windows()
    await runtime._async_refresh_inverters()  # noqa: SLF001
    coord.client.inverter_production.assert_not_awaited()

    coord.include_inverters = False
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    coord._type_device_buckets = {
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"serial_number": "INV-A"}],
        }
    }  # noqa: SLF001
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == []
    assert coord.inventory_view.type_bucket("microinverter") is None


@pytest.mark.asyncio
async def test_inventory_runtime_bulk_parameter_telemetry(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord._type_device_buckets["envoy"] = {  # noqa: SLF001
        "type_key": "envoy",
        "count": 1,
        "devices": [{"serial_number": "GW-A"}],
    }
    coord.client.system_dashboard_envoy_inverters = AsyncMock(
        return_value={
            "data": [
                {
                    "serial_number": "INV-A",
                    "id": "DEVICE-A",
                    "name": "IQ8",
                    "status": "normal",
                }
            ]
        }
    )
    coord.client.system_dashboard_master_data = AsyncMock(
        return_value={
            "parameters": [
                {"id": "power"},
                {"id": "ac_frequency"},
                {"id": "temperature"},
                {"id": "unrelated"},
            ]
        }
    )
    coord.client.system_dashboard_data_columns = AsyncMock(
        return_value={"columns": [{"attribute_name": "reading_1"}]}
    )

    async def _parameter_view(_serials, parameter_id, *, per_page, page):
        assert per_page == 50
        assert page == 1
        if parameter_id == "power":
            return {
                "intervals": [{"timestamp": "2026-07-11T01:00:00Z", "reading_1": 212}],
                "columns": [{"serial_number": "INV-A", "attribute_name": "reading_1"}],
            }
        return {
            "intervals": [
                {
                    "serial_number": "INV-A",
                    "timestamp": "2026-07-11T01:00:00Z",
                    "value": 49.98 if parameter_id == "ac_frequency" else "41.5",
                }
            ]
        }

    coord.client.system_dashboard_parameter_view = AsyncMock(
        side_effect=_parameter_view
    )
    coord.client.inverters_inventory = AsyncMock(return_value={"inverters": []})
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(return_value={})

    await runtime._async_refresh_inverters()  # noqa: SLF001

    snapshot = coord.inverter_data("INV-A")
    assert snapshot is not None
    assert snapshot["device_id"] is None
    assert snapshot["telemetry"] == {
        "power": 212.0,
        "parameter_ids": {
            "power": "power",
            "ac_frequency": "ac_frequency",
            "temperature": "temperature",
        },
        "sampled_at": {
            "power": "2026-07-11T01:00:00Z",
            "ac_frequency": "2026-07-11T01:00:00Z",
            "temperature": "2026-07-11T01:00:00Z",
        },
        "ac_frequency": 49.98,
        "temperature": 41.5,
    }
    assert coord._inverter_parameter_columns == ["reading_1"]  # noqa: SLF001
    assert coord.inventory_runtime.topology_snapshot().inverter_telemetry_serials == (
        "INV-A",
    )
    assert coord.client.system_dashboard_parameter_view.await_count == 3
    assert all(
        call.kwargs["per_page"] == 50
        for call in coord.client.system_dashboard_parameter_view.await_args_list
    )

    for family in (
        "inverter_dashboard_inventory",
        "inverter_parameter_catalog",
        "inverter_parameter_telemetry",
    ):
        assert (
            coord._endpoint_family_state(family).consecutive_failures == 0
        )  # noqa: SLF001

    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.client.system_dashboard_parameter_view.await_count == 3


@pytest.mark.asyncio
async def test_inventory_runtime_parameter_telemetry_follows_pagination(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._set_shared_state_attr("_inverter_parameter_ids", ["power"])  # noqa: SLF001
    monkeypatch.setattr(
        coord,
        "_endpoint_family_should_run",
        lambda family, **_kwargs: family == "inverter_parameter_telemetry",
    )

    async def _parameter_view(_serials, _parameter_id, *, per_page, page):
        assert per_page == 50
        if page == 1:
            return {
                "intervals": [{"serial_number": "INV-A", "value": 250}],
                "pagination": {"page": 1, "total_pages": 2},
            }
        assert page == 2
        return {
            "data": [{"serial_number": "INV-B", "value": 175}],
            "pagination": {"page": 2, "total_pages": 2},
        }

    coord.client.system_dashboard_parameter_view = AsyncMock(
        side_effect=_parameter_view
    )

    result = await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
        ["INV-A", "INV-B"]
    )

    assert result == {
        "INV-A": {"power": 250.0, "parameter_ids": {"power": "power"}},
        "INV-B": {"power": 175.0, "parameter_ids": {"power": "power"}},
    }
    assert [
        call.kwargs["page"]
        for call in coord.client.system_dashboard_parameter_view.await_args_list
    ] == [1, 2]


@pytest.mark.asyncio
async def test_inventory_runtime_parameter_pagination_limit_is_not_authoritative(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    current_time = [1_000.0]
    monkeypatch.setattr(
        inventory_runtime_mod,
        "time",
        SimpleNamespace(monotonic=lambda: current_time[0]),
    )
    runtime._update_shared_state(  # noqa: SLF001
        _inverter_parameter_ids=["power"],
        _inverter_parameter_telemetry={
            "INV-A": {
                "power": 100.0,
                "parameter_ids": {"power": "power"},
            },
            "INV-B": {
                "power": 200.0,
                "parameter_ids": {"power": "power"},
            },
        },
        _inverter_parameter_success_mono={
            "INV-A": {"power": 0.0},
            "INV-B": {"power": 0.0},
        },
    )
    monkeypatch.setattr(
        coord,
        "_endpoint_family_should_run",
        lambda family, **_kwargs: family == "inverter_parameter_telemetry",
    )
    coord.client.system_dashboard_parameter_view = AsyncMock(
        return_value={
            "intervals": [
                {"serial_number": "INV-A", "value": 275 - index} for index in range(50)
            ]
        }
    )
    monkeypatch.setattr(inventory_runtime_mod, "INVERTER_PARAMETER_MAX_PAGES", 1)

    result = await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
        ["INV-A", "INV-B"]
    )

    assert result == {
        "INV-A": {"power": 275.0, "parameter_ids": {"power": "power"}},
        "INV-B": {"power": 200.0, "parameter_ids": {"power": "power"}},
    }
    assert coord._inverter_parameter_success_mono == {  # noqa: SLF001
        "INV-A": {"power": 1_000.0},
        "INV-B": {"power": 0.0},
    }
    health = coord._endpoint_family_state(  # noqa: SLF001
        "inverter_parameter_telemetry"
    )
    assert health.partial_success is True
    assert health.successful_items == 0
    assert health.total_items == 1
    assert health.using_cached_data is True
    assert health.last_error == "Dashboard parameter pagination limit reached for power"

    current_time[0] = 1_901.0
    monkeypatch.setattr(coord, "_endpoint_family_should_run", lambda *_args: False)
    assert await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
        ["INV-A", "INV-B"]
    ) == {"INV-A": {"power": 275.0, "parameter_ids": {"power": "power"}}}
    assert coord._inverter_parameter_success_mono == {  # noqa: SLF001
        "INV-A": {"power": 1_000.0}
    }

    current_time[0] = 2_000.0
    monkeypatch.setattr(
        coord,
        "_endpoint_family_should_run",
        lambda family, **_kwargs: family == "inverter_parameter_telemetry",
    )
    await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
        ["INV-A", "INV-B"]
    )
    assert (
        coord._endpoint_family_state(  # noqa: SLF001
            "inverter_parameter_telemetry"
        ).using_cached_data
        is False
    )


@pytest.mark.asyncio
async def test_inventory_runtime_optional_batch_has_bounded_concurrency(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().inventory_runtime
    active = 0
    max_active = 0
    order: list[str] = []

    async def _request(label: str) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        order.append(label)
        await asyncio.sleep(0)
        active -= 1
        return label

    labels = ["first", "second", "third", "fourth", "fifth"]
    results = await runtime._async_run_bounded_optional_batch(  # noqa: SLF001
        [lambda label=label: _request(label) for label in labels],
        concurrency=2,
    )

    assert results == labels
    assert order == labels
    assert max_active == 2

    bounded = await runtime._async_run_bounded_optional_batch(  # noqa: SLF001
        [lambda: _request("too-late")],
        timeout_s=0.0,
    )

    assert isinstance(bounded[0], TimeoutError)

    async def _failed() -> object:
        raise RuntimeError("failed")

    failed = await runtime._async_run_bounded_optional_batch([_failed])  # noqa: SLF001
    assert isinstance(failed[0], RuntimeError)


@pytest.mark.asyncio
async def test_inventory_runtime_empty_parameter_rows_clear_cached_value(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._set_shared_state_attr(  # noqa: SLF001
        "_inverter_parameter_ids", ["power", "temperature"]
    )
    runtime._set_shared_state_attr(  # noqa: SLF001
        "_inverter_parameter_telemetry",
        {
            "INV-A": {
                "power": 250.0,
                "temperature": 42.0,
                "parameter_ids": {
                    "power": "power",
                    "temperature": "temperature",
                },
                "sampled_at": {
                    "power": "2026-07-11T01:00:00Z",
                    "temperature": "2026-07-11T01:00:00Z",
                },
            },
            "INV-B": {"power": 20.0},
            "INV-C": {
                "power": 30.0,
                "parameter_ids": {"power": "power"},
                "sampled_at": {"power": "2026-07-11T01:00:00Z"},
            },
            "INV-RETIRED": {"power": 10.0},
        },
    )
    monkeypatch.setattr(
        coord,
        "_endpoint_family_should_run",
        lambda family, **_kwargs: family == "inverter_parameter_telemetry",
    )
    coord.client.system_dashboard_parameter_view = AsyncMock(
        side_effect=[
            {"intervals": []},
            RuntimeError("temperature temporarily unavailable"),
        ]
    )

    result = await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
        ["INV-A", "INV-B", "INV-C"]
    )

    assert result == {
        "INV-A": {
            "temperature": 42.0,
            "parameter_ids": {"temperature": "temperature"},
            "sampled_at": {"temperature": "2026-07-11T01:00:00Z"},
        }
    }
    health = coord._endpoint_family_state(  # noqa: SLF001
        "inverter_parameter_telemetry"
    )
    assert health.consecutive_failures == 0
    assert health.degraded is False
    assert health.partial_success is True
    assert health.successful_items == 1
    assert health.total_items == 2
    assert health.using_cached_data is True
    assert health.cache_stale is False
    assert health.last_error == "temperature temporarily unavailable"
    assert health.next_retry_utc is not None
    metrics = coord.collect_site_metrics()
    assert "inverter_parameter_telemetry" not in metrics["degraded_endpoint_families"]
    assert (
        metrics["endpoint_failure_details"]["inverter_parameter_telemetry"]["retry_utc"]
        == health.next_retry_utc.isoformat()
    )


@pytest.mark.asyncio
async def test_inventory_runtime_full_parameter_failures_use_fresh_cache_until_stale(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    now_mono = time.monotonic()
    runtime._update_shared_state(  # noqa: SLF001
        _inverter_parameter_ids=["power", "temperature"],
        _inverter_parameter_telemetry={"INV-A": {"power": 250.0}},
        _inverter_parameter_success_mono={"power": now_mono},
    )
    monkeypatch.setattr(
        coord,
        "_endpoint_family_should_run",
        lambda family, **_kwargs: family == "inverter_parameter_telemetry",
    )
    coord.client.system_dashboard_parameter_view = AsyncMock(
        side_effect=RuntimeError("site 9633674 inverter INV-A telemetry unavailable")
    )

    for failure_count in (1, 2, 3):
        result = (
            await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
                ["INV-A"]
            )
        )
        health = coord._endpoint_family_state(  # noqa: SLF001
            "inverter_parameter_telemetry"
        )
        assert result == {"INV-A": {"power": 250.0}}
        assert health.consecutive_failures == failure_count
        assert health.degraded is False
        assert health.successful_items == 0
        assert health.total_items == 2
        assert health.using_cached_data is True
        assert health.cache_stale is False
        assert "9633674" not in (health.last_error or "")
        assert "INV-A" not in (health.last_error or "")
        assert health.next_retry_utc is not None
        assert (
            "inverter_parameter_telemetry"
            not in coord.collect_site_metrics()["degraded_endpoint_families"]
        )

    metrics = coord.collect_site_metrics()
    assert metrics["degraded_endpoint_families"] == []
    assert (
        metrics["endpoint_failure_details"]["inverter_parameter_telemetry"]["reason"]
        == health.last_error
    )
    endpoint_health = metrics["endpoint_family_health"]["inverter_parameter_telemetry"]
    assert endpoint_health["degraded"] is False
    assert endpoint_health["successful_items"] == 0
    assert endpoint_health["using_cached_data"] is True

    runtime._set_shared_state_attr(  # noqa: SLF001
        "_inverter_parameter_success_mono",
        {"INV-A": {"power": time.monotonic() - 1_801.0}},
    )
    result = await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
        ["INV-A"]
    )

    assert result == {}
    assert health.degraded is True
    assert health.cache_stale is True
    assert health.using_cached_data is False
    assert coord.collect_site_metrics()["degraded_endpoint_families"] == [
        "inverter_parameter_telemetry"
    ]


@pytest.mark.asyncio
async def test_inventory_runtime_rate_limit_does_not_degrade_cloud_service(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._set_shared_state_attr("_inverter_parameter_ids", ["power"])  # noqa: SLF001
    monkeypatch.setattr(
        coord,
        "_endpoint_family_should_run",
        lambda family, **_kwargs: family == "inverter_parameter_telemetry",
    )
    coord.client.system_dashboard_parameter_view = AsyncMock(
        side_effect=api.InvalidPayloadError(
            "HTTP error from Enphase endpoint at /systems/9633674/inverters",
            status=429,
            endpoint="/systems/9633674/inverters",
        )
    )

    assert (
        await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
            ["INV-A"]
        )
        == {}
    )

    health = coord._endpoint_family_state(  # noqa: SLF001
        "inverter_parameter_telemetry"
    )
    assert health.degraded is True
    assert health.last_status == 429
    assert health.last_error == "Enphase rate limit (HTTP 429)"
    metrics = coord.collect_site_metrics()
    endpoint_health = metrics["endpoint_family_health"]["inverter_parameter_telemetry"]
    assert endpoint_health["rate_limited"] is True
    assert metrics["degraded_endpoint_families"] == []
    assert metrics["degraded_services"] == []
    assert metrics["endpoint_failure_details"]["inverter_parameter_telemetry"] == {
        "reason": "Enphase rate limit (HTTP 429)",
        "retry_utc": health.next_retry_utc.isoformat(),
    }


@pytest.mark.asyncio
async def test_inventory_runtime_partial_rate_limit_preserves_cooldown(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._set_shared_state_attr(  # noqa: SLF001
        "_inverter_parameter_ids",
        ["power", "temperature"],
    )
    should_run = coord._endpoint_family_should_run  # noqa: SLF001
    monkeypatch.setattr(
        coord,
        "_endpoint_family_should_run",
        lambda family, **kwargs: (
            should_run(family, **kwargs)
            if family == "inverter_parameter_telemetry"
            else False
        ),
    )
    coord.client.system_dashboard_parameter_view = AsyncMock(
        side_effect=[
            {
                "intervals": [
                    {"serial_number": "INV-A", "value": 250.0},
                    {"serial_number": "INV-B", "value": 240.0},
                ]
            },
            {
                "intervals": [{"serial_number": "INV-A", "value": 42.0}],
                "has_next": True,
            },
            api.InvalidPayloadError(
                "HTTP error from Enphase endpoint",
                status=429,
                endpoint="/systems/9633674/inverters",
            ),
        ]
    )

    result = await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
        ["INV-A", "INV-B"]
    )

    assert result == {
        "INV-A": {
            "power": 250.0,
            "temperature": 42.0,
            "parameter_ids": {
                "power": "power",
                "temperature": "temperature",
            },
        },
        "INV-B": {
            "power": 240.0,
            "parameter_ids": {"power": "power"},
        },
    }
    health = coord._endpoint_family_state(  # noqa: SLF001
        "inverter_parameter_telemetry"
    )
    assert health.consecutive_failures == 1
    assert health.last_status == 429
    assert health.cooldown_active is True
    assert health.partial_success is True
    assert health.degraded is False
    assert health.last_error == "Enphase rate limit (HTTP 429)"
    assert coord.collect_site_metrics()["degraded_endpoint_families"] == []
    assert coord.client.system_dashboard_parameter_view.await_count == 3

    assert (
        await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
            ["INV-A", "INV-B"]
        )
        == result
    )
    assert coord.client.system_dashboard_parameter_view.await_count == 3


@pytest.mark.asyncio
async def test_inventory_runtime_full_failure_without_cache_degrades_immediately(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._set_shared_state_attr("_inverter_parameter_ids", ["power"])  # noqa: SLF001
    monkeypatch.setattr(
        coord,
        "_endpoint_family_should_run",
        lambda family, **_kwargs: family == "inverter_parameter_telemetry",
    )
    coord.client.system_dashboard_parameter_view = AsyncMock(
        side_effect=TimeoutError("parameter request timed out")
    )

    assert (
        await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
            ["INV-A"]
        )
        == {}
    )

    health = coord._endpoint_family_state(  # noqa: SLF001
        "inverter_parameter_telemetry"
    )
    assert health.consecutive_failures == 1
    assert health.degraded is True
    assert health.using_cached_data is False


@pytest.mark.asyncio
async def test_inventory_runtime_stale_parameter_cache_degrades_partial_result(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._update_shared_state(  # noqa: SLF001
        _inverter_parameter_ids=["power", "temperature"],
        _inverter_parameter_telemetry={"INV-A": {"power": 250.0, "temperature": 42.0}},
        _inverter_parameter_success_mono={
            "power": time.monotonic(),
            "temperature": time.monotonic() - 1_801.0,
        },
    )
    monkeypatch.setattr(
        coord,
        "_endpoint_family_should_run",
        lambda family, **_kwargs: family == "inverter_parameter_telemetry",
    )
    coord.client.system_dashboard_parameter_view = AsyncMock(
        side_effect=[
            {"intervals": [{"serial_number": "INV-A", "value": 260.0}]},
            RuntimeError("temperature unavailable"),
        ]
    )

    result = await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
        ["INV-A"]
    )

    assert result == {
        "INV-A": {
            "power": 260.0,
            "parameter_ids": {"power": "power"},
        }
    }
    health = coord._endpoint_family_state(  # noqa: SLF001
        "inverter_parameter_telemetry"
    )
    assert health.degraded is True
    assert health.partial_success is True
    assert health.cache_stale is True
    assert health.using_cached_data is False


@pytest.mark.asyncio
async def test_inventory_runtime_partial_gateway_inventory_preserves_cache(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord._type_device_buckets["envoy"] = {  # noqa: SLF001
        "type_key": "envoy",
        "count": 2,
        "devices": [
            {"serial_number": "GW-A"},
            {"serial_number": "GW-B"},
        ],
    }
    runtime._set_shared_state_attr(  # noqa: SLF001
        "_inverter_dashboard_inventory",
        {"INV-CACHED": {"serial_number": "INV-CACHED", "status": "normal"}},
    )
    coord.client.system_dashboard_envoy_inverters = AsyncMock(
        side_effect=[
            {"data": [{"serial_number": "INV-FRESH", "status": "warning"}]},
            RuntimeError("second gateway unavailable"),
        ]
    )

    result = await runtime._async_inverter_dashboard_inventory([])  # noqa: SLF001

    assert result == [
        {"serial_number": "INV-CACHED", "status": "normal"},
        {"serial_number": "INV-FRESH", "status": "warning"},
    ]
    assert (
        coord._endpoint_family_state(  # noqa: SLF001
            "inverter_dashboard_inventory"
        ).consecutive_failures
        == 1
    )


def test_inventory_runtime_parameter_row_shapes_and_invalid_values() -> None:
    parser = InventoryRuntime._inverter_parameter_rows
    assert parser({"data": [{"serial_num": "INV-A", "power": "10.5"}]}, "power") == {
        "INV-A": (10.5, None)
    }
    assert parser(
        {
            "intervals": [
                {"device_serial": "INV-A", "reading": "N/A"},
                {"device_serial": "INV-B", "reading": float("inf")},
                {"device_serial": "INV-C", "reading": "firmware-v1"},
            ]
        },
        "firmware",
    ) == {"INV-C": ("firmware-v1", None)}
    assert parser({"intervals": "bad"}, "power") == {}


def test_inventory_runtime_parameter_paging_metadata() -> None:
    has_more = InventoryRuntime._inverter_parameter_has_more_pages

    assert has_more({"has_next": True}, 1) is True
    assert has_more({"next_page": None}, 1) is False
    assert has_more({"nextPage": "2"}, 1) is True
    assert has_more({"total": "3", "per_page": "2", "page": "1"}, 1) is True
    assert (
        has_more(
            {
                "total": "16",
                "per_page": "50",
                "pagination": {"page": 1, "total_pages": 2},
            },
            1,
        )
        is True
    )
    assert has_more({}, 1) is None


@pytest.mark.asyncio
async def test_inventory_runtime_parameter_later_page_failure_retains_readings(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().inventory_runtime
    fetcher = AsyncMock(
        side_effect=[
            {
                "intervals": [{"serial_number": "INV-A", "value": 250}],
                "has_next": True,
            },
            TimeoutError("later page timed out"),
        ]
    )

    result = await runtime._async_fetch_complete_inverter_parameter(  # noqa: SLF001
        fetcher,
        ["INV-A", "INV-B"],
        "power",
        per_page=50,
    )

    assert result.readings == {"INV-A": (250.0, None)}
    assert result.authoritative_serials == frozenset({"INV-A"})
    assert isinstance(result.error, TimeoutError)


@pytest.mark.asyncio
async def test_inventory_runtime_parameter_telemetry_edge_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord._inverter_dashboard_inventory = {  # noqa: SLF001
        "INV-CACHED": {"serial_number": "INV-CACHED"}
    }
    coord._type_device_buckets["envoy"] = {  # noqa: SLF001
        "type_key": "envoy",
        "count": 0,
        "devices": "bad",
    }
    original_type_bucket = runtime.type_bucket
    monkeypatch.setattr(runtime, "type_bucket", lambda _type: {"devices": "bad"})
    assert runtime._gateway_serials_for_inverter_telemetry() == []  # noqa: SLF001
    monkeypatch.setattr(runtime, "type_bucket", lambda _type: {"devices": ["bad"]})
    assert runtime._gateway_serials_for_inverter_telemetry() == []  # noqa: SLF001
    monkeypatch.setattr(runtime, "type_bucket", original_type_bucket)
    assert await runtime._async_inverter_dashboard_inventory([]) == [  # noqa: SLF001
        {"serial_number": "INV-CACHED"}
    ]
    coord._endpoint_family_state(  # noqa: SLF001
        "inverter_dashboard_inventory"
    ).last_success_mono = (time.monotonic() - 86_401.0)
    assert await runtime._async_inverter_dashboard_inventory([]) == []  # noqa: SLF001

    coord._type_device_buckets["envoy"] = {  # noqa: SLF001
        "type_key": "envoy",
        "count": 1,
        "devices": ["bad", {}, {"serial_num": "GW-A"}, {"serial_num": "GW-A"}],
    }
    assert runtime._gateway_serials_for_inverter_telemetry() == ["GW-A"]  # noqa: SLF001
    monkeypatch.setattr(coord, "_endpoint_family_should_run", lambda *_args: False)
    assert await runtime._async_inverter_dashboard_inventory(  # noqa: SLF001
        [{"serial_number": "INV-CACHED", "name": "Legacy"}]
    ) == [{"serial_number": "INV-CACHED", "name": "Legacy"}]

    monkeypatch.setattr(coord, "_endpoint_family_should_run", lambda *_args: True)
    coord.client.system_dashboard_envoy_inverters = AsyncMock(
        return_value={"data": ["bad", {"serial_number": ""}]}
    )
    assert (
        await runtime._async_inverter_dashboard_inventory(  # noqa: SLF001
            [{"serial_number": ""}]
        )
        == []
    )
    coord.client.system_dashboard_envoy_inverters = AsyncMock(
        return_value={"data": "bad"}
    )
    assert await runtime._async_inverter_dashboard_inventory([]) == []  # noqa: SLF001
    coord.client.system_dashboard_envoy_inverters = AsyncMock(
        side_effect=RuntimeError("optional")
    )
    assert await runtime._async_inverter_dashboard_inventory([]) == []  # noqa: SLF001

    assert runtime._inverter_parameter_value(None) is None  # noqa: SLF001
    assert runtime._inverter_parameter_value(True) is None  # noqa: SLF001
    assert runtime._inverter_parameter_value({}) is None  # noqa: SLF001
    assert runtime._inverter_parameter_rows(  # noqa: SLF001
        {
            "intervals": [
                "bad",
                {"serial_number": "INV-A", "value": 1},
                {"INV-B": 2},
            ],
            "columns": [
                "bad",
                {"serial_number": ""},
                {"serial_number": "INV-A", "attribute_name": "unused"},
                {"serial_number": "INV-B"},
            ],
        },
        "power",
    ) == {"INV-A": (1.0, None), "INV-B": (2.0, None)}

    coord.client.system_dashboard_master_data = AsyncMock(
        return_value={
            "parameters": [
                "bad",
                {"id": "power"},
                {"id": "ac_power"},
            ]
        }
    )
    coord.client.system_dashboard_data_columns = AsyncMock(
        side_effect=["bad", {"columns": "bad"}]
    )
    coord._type_device_buckets["envoy"]["devices"] = [  # noqa: SLF001
        {"serial_num": "GW-A"},
        {"serial_num": "GW-B"},
    ]
    coord.client.system_dashboard_parameter_view = AsyncMock(
        side_effect=[RuntimeError("one parameter failed")]
    )
    assert (
        await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
            ["INV-A"]
        )
        == {}
    )

    coord.client.system_dashboard_master_data = AsyncMock(return_value=None)
    coord.client.system_dashboard_data_columns = AsyncMock(
        return_value={"columns": ["bad", {"name": "column-name"}]}
    )
    coord._inverter_parameter_ids = ["power", "temperature"]  # noqa: SLF001
    coord.client.system_dashboard_parameter_view = AsyncMock(
        side_effect=["bad", {"intervals": [{"serial_number": "OTHER", "value": 2}]}]
    )
    assert (
        await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
            ["INV-A"]
        )
        == {}
    )
    assert coord._inverter_parameter_columns == ["column-name"]  # noqa: SLF001

    coord.client.system_dashboard_master_data = AsyncMock(
        side_effect=RuntimeError("catalog failed")
    )
    coord._inverter_parameter_ids = []  # noqa: SLF001
    assert (
        await runtime._async_refresh_inverter_parameter_telemetry(  # noqa: SLF001
            ["INV-A"]
        )
        == {}
    )
    coord._inverter_parameter_telemetry = {"INV-A": {"power": 1}}  # noqa: SLF001
    coord._endpoint_family_state(  # noqa: SLF001
        "inverter_parameter_telemetry"
    ).last_success_mono = (time.monotonic() - 1_801.0)
    assert (
        await runtime._async_refresh_inverter_parameter_telemetry([]) == {}
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_inverters_early_return_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    coord.client.inverters_inventory = None
    coord.client.inverter_status = AsyncMock(side_effect=AssertionError("unused"))
    await runtime._async_refresh_inverters()  # noqa: SLF001

    coord.client.inverters_inventory = AsyncMock(return_value={})
    coord.client.inverter_status = None
    await runtime._async_refresh_inverters()  # noqa: SLF001


def test_inventory_runtime_inverters_refresh_due_covers_include_flag_and_production_due(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    monkeypatch.setattr(inventory_runtime_mod.time, "monotonic", lambda: 100.0)

    coord.include_inverters = False
    assert runtime.inverters_refresh_due() is False

    coord.include_inverters = True
    coord.client.inverters_inventory = AsyncMock(return_value={})
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(return_value={})
    runtime._inverters_inventory_cache_until = 150.0  # noqa: SLF001
    runtime._inverter_status_cache_until = 150.0  # noqa: SLF001
    runtime._inverter_production_cache_key = None  # noqa: SLF001
    runtime._inverter_production_payload = None  # noqa: SLF001
    runtime._inverter_production_cache_until = None  # noqa: SLF001
    coord.energy._site_energy_meta = {"start_date": "2026-01-01"}  # noqa: SLF001

    assert runtime.inverters_refresh_due() is True


@pytest.mark.asyncio
async def test_inventory_runtime_ensure_dashboard_refreshes_when_empty(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._system_dashboard_type_summaries = {}  # noqa: SLF001
    runtime._system_dashboard_hierarchy_summary = {}  # noqa: SLF001
    refresh = AsyncMock()
    object.__setattr__(runtime, "_async_refresh_system_dashboard", refresh)

    await runtime.async_ensure_system_dashboard_diagnostics()

    refresh.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_system_dashboard_populates_summaries_and_accessors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client.devices_tree = AsyncMock(
        return_value={
            "devices": [
                {
                    "device_uid": "GW-1",
                    "type": "envoy",
                    "name": "Gateway",
                    "children": [
                        {
                            "device_uid": "BAT-1",
                            "type": "encharge",
                            "name": "Battery 1",
                        },
                        {"device_uid": "MTR-1", "type": "meter", "name": "Meter 1"},
                    ],
                }
            ]
        }
    )
    coord.client.devices_details = AsyncMock(
        side_effect=lambda type_key: {
            "envoys": {
                "envoys": [
                    {
                        "device_uid": "GW-1",
                        "type": "envoy",
                        "status": "normal",
                        "last_report": "2026-03-09T05:45:00+00:00",
                        "network": {"mode": "dhcp"},
                    }
                ]
            },
            "meters": {
                "meters": [
                    {
                        "device_uid": "MTR-1",
                        "type": "meter",
                        "name": "Consumption Meter",
                        "meter_type": "consumption",
                        "meter_state": "Enabled",
                    }
                ]
            },
            "encharges": {
                "encharges": [
                    {
                        "device_uid": "BAT-1",
                        "type": "encharge",
                        "serial_number": "BAT-1",
                        "rssi_dbm": -61,
                    }
                ]
            },
            "inverters": {
                "inverters": {
                    "total": 16,
                    "not_reporting": 1,
                    "plc_comm": 5,
                    "items": [{"name": "IQ7A Microinverters", "count": 16}],
                }
            },
        }.get(type_key)
    )

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    diagnostics = runtime.system_dashboard_diagnostics()
    assert diagnostics["hierarchy_summary"]["counts_by_type"]["envoy"] == 2
    assert diagnostics["type_summaries"]["microinverter"]["model_summary"] == (
        "IQ7A Microinverters x16"
    )
    assert runtime.system_dashboard_envoy_detail()["status"] == "normal"
    assert runtime.system_dashboard_meter_detail("consumption")["meter_state"] == (
        "Enabled"
    )
    assert runtime.system_dashboard_battery_detail("BAT-1")["rssi_dbm"] == -61

    coord.client.devices_tree.reset_mock()
    coord.client.devices_details.reset_mock()
    await runtime._async_refresh_system_dashboard()
    coord.client.devices_tree.assert_not_called()
    coord.client.devices_details.assert_not_called()


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_system_dashboard_handles_missing_fetchers_and_invalid_payloads(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client.devices_tree = None
    coord.client.devices_details = None

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    assert runtime.system_dashboard_diagnostics()["devices_tree_payload"] is None

    monkeypatch.setattr(
        inventory_runtime_mod,
        "SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES",
        ("", "envoy", "envoys"),
    )
    coord.client.devices_tree = AsyncMock(return_value=["bad"])
    coord.client.devices_details = AsyncMock(side_effect=[{}, ["bad"], {}])

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    diagnostics = runtime.system_dashboard_diagnostics()
    assert diagnostics["devices_tree_payload"] is None
    assert diagnostics["devices_details_payloads"]["envoy"] == {"envoys": {}}


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_system_dashboard_fetches_details_concurrently(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    monkeypatch.setattr(
        inventory_runtime_mod,
        "SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES",
        ("envoys", "meters", "encharges", "inverters"),
    )
    monkeypatch.setattr(
        inventory_runtime_mod,
        "SYSTEM_DASHBOARD_DETAIL_CONCURRENCY",
        2,
    )
    active = 0
    max_active = 0

    async def _details(source_type: str):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return {source_type: [{"device_uid": source_type}]}

    coord.client.devices_tree = AsyncMock(return_value={"devices": []})
    coord.client.devices_details = AsyncMock(side_effect=_details)

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    assert max_active == 2
    assert coord.client.devices_details.await_count == 4


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_system_dashboard_handles_fetch_exceptions(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord._system_dashboard_devices_tree_raw = {
        "devices": [{"device_uid": "cached"}]
    }  # noqa: SLF001
    coord._system_dashboard_devices_details_raw = {  # noqa: SLF001
        "envoy": {"envoys": {"envoys": [{"device_uid": "GW-1"}]}}
    }
    monkeypatch.setattr(
        inventory_runtime_mod,
        "SYSTEM_DASHBOARD_DIAGNOSTIC_TYPES",
        ("envoys", "meters"),
    )
    coord.client.devices_tree = AsyncMock(side_effect=RuntimeError("tree"))

    async def _details(source_type: str):
        if source_type == "envoys":
            raise RuntimeError("detail")
        return {"meters": [{"name": "Meter"}]}

    coord.client.devices_details = AsyncMock(side_effect=_details)

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    diagnostics = runtime.system_dashboard_diagnostics()
    assert diagnostics["devices_tree_payload"] == {
        "devices": [{"device_uid": "cached"}]
    }
    assert diagnostics["devices_details_payloads"]["envoy"]["envoys"] == {
        "envoys": [{"device_uid": "GW-1"}]
    }
    assert diagnostics["devices_details_payloads"]["envoy"]["meters"] == {
        "meters": [{"name": "Meter"}]
    }
    assert diagnostics["detail_failures"] == {"envoys": "detail"}


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_devices_inventory_without_refresh_kw(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    calls: list[str] = []

    async def devices_inventory():
        calls.append("devices_inventory")
        return {"ok": True}

    coord.client.devices_inventory = devices_inventory  # type: ignore[method-assign]
    coord._parse_devices_inventory_payload = MagicMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value=(True, {"envoy": {"count": 1}}, ["envoy"])
    )
    runtime._set_type_device_buckets = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._merge_heatpump_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001

    await runtime._async_refresh_devices_inventory(force=True)  # noqa: SLF001

    assert calls == ["devices_inventory"]
    runtime._set_type_device_buckets.assert_called_once_with(  # noqa: SLF001
        {"envoy": {"count": 1}},
        ["envoy"],
    )
    assert coord._devices_inventory_payload == {"ok": True}  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_hems_devices_uses_heatpump_runtime_preflight(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = {"heatpump"}  # noqa: SLF001
    runtime = coord.inventory_runtime

    async def _mark_unsupported(*, force: bool = False) -> None:
        assert force is True
        coord.client._hems_site_supported = False  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=_mark_unsupported
    )
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("no fetch"))
    runtime._merge_heatpump_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._debug_log_summary_if_changed = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001

    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight.assert_awaited_once_with(  # noqa: SLF001
        force=True
    )
    coord.client.hems_devices.assert_not_awaited()
    runtime._merge_heatpump_type_bucket.assert_called_once_with()  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_disables_hems_inventory_without_heatpump(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = {"envoy", "iqevse"}  # noqa: SLF001
    runtime = coord.inventory_runtime
    runtime._hems_devices_payload = {"existing": True}  # noqa: SLF001
    runtime._hems_devices_last_success_mono = time.monotonic()  # noqa: SLF001
    runtime._hems_devices_last_success_utc = datetime.now(timezone.utc)  # noqa: SLF001
    runtime._hems_devices_using_stale = True  # noqa: SLF001
    runtime._devices_inventory_payload = {  # noqa: SLF001
        "result": [
            {
                "type": "hemsDevices",
                "devices": [
                    {
                        "heat-pump": [
                            {
                                "device-uid": "legacy-heat-pump",
                                "device-type": "HEAT_PUMP",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    coord._note_hems_auth_failure(  # noqa: SLF001
        api.Unauthorized(), endpoint="hems_devices"
    )
    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("unused")
    )
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("unused"))

    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight.assert_not_awaited()
    coord.client.hems_devices.assert_not_awaited()
    assert runtime.hems_devices_refresh_due(force=True) is False
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_devices_last_success_mono is None  # noqa: SLF001
    assert runtime._hems_devices_last_success_utc is None  # noqa: SLF001
    assert runtime._hems_devices_using_stale is False  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001
    assert coord.inventory_view.has_type("heatpump") is False
    assert "heatpump" not in coord.inventory_view.iter_type_keys()
    assert coord._hems_auth_circuit_active() is False  # noqa: SLF001
    assert coord.collect_site_metrics()["hems_inventory_polling_enabled"] is False


@pytest.mark.asyncio
async def test_inventory_runtime_preserves_non_heatpump_hems_auth_circuit(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = set()  # noqa: SLF001
    runtime = coord.inventory_runtime
    coord._note_hems_auth_failure(  # noqa: SLF001
        api.Unauthorized(), endpoint="hems_consumption_lifetime"
    )
    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("unused")
    )
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("unused"))

    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight.assert_not_awaited()
    coord.client.hems_devices.assert_not_awaited()
    assert coord._hems_auth_circuit_active() is True  # noqa: SLF001
    assert coord._hems_auth_last_endpoint == "hems_consumption_lifetime"  # noqa: SLF001


def test_inventory_runtime_hems_inventory_enabled_for_heatpump_and_legacy_entries(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    coord._selected_type_keys = None  # noqa: SLF001
    assert runtime.hems_devices_refresh_due(force=True) is True

    coord._selected_type_keys = {"heatpump"}  # noqa: SLF001
    assert runtime.hems_devices_refresh_due(force=True) is True


@pytest.mark.asyncio
async def test_inventory_runtime_hems_devices_stops_after_preflight_auth_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._hems_devices_payload = {"existing": True}  # noqa: SLF001

    async def _open_auth_circuit(*, force: bool = False) -> None:
        assert force is True
        coord._note_hems_auth_failure(  # noqa: SLF001
            api.Unauthorized(),
            endpoint="hems_support_preflight",
        )

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=_open_auth_circuit
    )
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("unused"))

    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001

    coord.client.hems_devices.assert_not_awaited()
    assert coord._hems_auth_circuit_active() is True  # noqa: SLF001
    assert runtime._hems_devices_payload == {"existing": True}  # noqa: SLF001
    assert runtime._hems_devices_using_stale is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_skips_hems_devices_during_auth_circuit(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._hems_devices_payload = {"existing": True}  # noqa: SLF001
    coord._hems_auth_backoff_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=AssertionError("unused"))

    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001

    coord.client.hems_devices.assert_not_awaited()
    assert runtime._hems_devices_payload == {"existing": True}  # noqa: SLF001
    assert runtime._hems_devices_using_stale is True  # noqa: SLF001
    assert runtime.hems_devices_refresh_due(force=True) is False


@pytest.mark.asyncio
async def test_inventory_runtime_hems_devices_auth_failure_uses_circuit(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._hems_devices_payload = {"existing": True}  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=api.Unauthorized())

    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001

    assert coord._hems_auth_circuit_active() is True  # noqa: SLF001
    assert runtime._hems_devices_payload == {"existing": True}  # noqa: SLF001
    assert runtime._hems_devices_using_stale is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_refreshable_fetcher_falls_back_when_uninspectable(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().inventory_runtime

    class BadSignatureFetcher:
        @property
        def __signature__(self):
            raise ValueError("boom")

        async def __call__(self):
            return {"ok": True}

    assert await runtime._async_call_refreshable_fetcher(  # noqa: SLF001
        BadSignatureFetcher(),
        force=True,
    ) == {"ok": True}


@pytest.mark.asyncio
async def test_coordinator_inventory_runtime_wrapper_delegation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = MagicMock()
    snapshot = object()
    runtime._current_topology_snapshot.return_value = snapshot
    runtime._extract_hems_group_members.return_value = (True, [{"device_uid": "x"}])
    runtime._async_refresh_devices_inventory = AsyncMock()
    runtime._rebuild_inventory_summary_caches = MagicMock()
    coord.inventory_runtime = runtime

    assert (
        InventoryRuntime._router_record_key({"key": "router"}) == "router"
    )  # noqa: SLF001
    assert coord._current_topology_snapshot() is snapshot  # noqa: SLF001
    assert InventoryRuntime._legacy_hems_devices_groups(  # noqa: SLF001
        {"result": [{"type": "hemsDevices", "devices": [{"gateway": [{}]}]}]}
    ) == [{"gateway": [{}]}]
    assert InventoryRuntime._normalize_hems_member(
        {"device-uid": "abc", "serial": "123"}
    ) == {  # noqa: SLF001
        "device-uid": "abc",
        "serial": "123",
        "device_uid": "abc",
        "serial_number": "123",
        "uid": "abc",
    }
    assert coord.inventory_runtime._extract_hems_group_members(
        [], {"gateway"}
    ) == (  # noqa: SLF001
        True,
        [{"device_uid": "x"}],
    )

    coord.inventory_runtime._rebuild_inventory_summary_caches()  # noqa: SLF001
    await coord.inventory_runtime._async_refresh_devices_inventory(
        force=True
    )  # noqa: SLF001

    runtime._current_topology_snapshot.assert_called_once_with()
    runtime._extract_hems_group_members.assert_called_once_with([], {"gateway"})
    runtime._rebuild_inventory_summary_caches.assert_called_once_with()
    runtime._async_refresh_devices_inventory.assert_awaited_once_with(force=True)


def test_inventory_runtime_rebuild_summary_caches_updates_coordinator_caches(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime

    coord._gateway_inventory_summary_cache = {"stale": True}  # noqa: SLF001
    coord._microinverter_inventory_summary_cache = {"stale": True}  # noqa: SLF001
    coord._heatpump_inventory_summary_cache = {"stale": True}  # noqa: SLF001
    coord._heatpump_type_summaries_cache = {
        "HEAT_PUMP": {"stale": True}
    }  # noqa: SLF001
    coord._gateway_iq_energy_router_records_cache = [{"key": "stale"}]  # noqa: SLF001

    monkeypatch.setattr(runtime, "_gateway_inventory_summary_marker", lambda: "gw")
    monkeypatch.setattr(
        runtime, "_microinverter_inventory_summary_marker", lambda: "micro"
    )
    monkeypatch.setattr(runtime, "_heatpump_inventory_summary_marker", lambda: "heat")
    monkeypatch.setattr(
        runtime, "_gateway_iq_energy_router_records_marker", lambda: "router"
    )
    monkeypatch.setattr(
        runtime, "_build_gateway_inventory_summary", lambda: {"gateway": 1}
    )
    monkeypatch.setattr(
        runtime, "_build_microinverter_inventory_summary", lambda: {"micro": 2}
    )
    monkeypatch.setattr(
        runtime, "_build_heatpump_inventory_summary", lambda: {"heatpump": 3}
    )
    monkeypatch.setattr(
        runtime,
        "_build_heatpump_type_summaries",
        lambda: {"HEAT_PUMP": {"count": 4}},
    )
    monkeypatch.setattr(
        runtime,
        "_gateway_iq_energy_router_summary_records",
        lambda _records: [{"key": "router-1", "name": "Router"}],
    )
    monkeypatch.setattr(runtime, "gateway_iq_energy_router_records", lambda: [])

    coord.inventory_runtime._rebuild_inventory_summary_caches()  # noqa: SLF001

    assert coord.gateway_inventory_summary() == {"gateway": 1}
    assert coord.microinverter_inventory_summary() == {"micro": 2}
    assert coord.heatpump_inventory_summary() == {"heatpump": 3}
    assert coord.heatpump_type_summary("heat_pump") == {"count": 4}
    assert coord.inventory_view.gateway_iq_energy_router_summary_records() == [
        {"key": "router-1", "name": "Router"}
    ]


def test_inventory_runtime_parser_and_hems_edge_cases(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.inventory_runtime import InventoryRuntime
    from custom_components.enphase_ev import inventory_runtime as inv_mod

    runtime = coordinator_factory().inventory_runtime

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    class EmptyNormalized(dict):
        def __bool__(self) -> bool:
            return False

    buckets = runtime._devices_inventory_buckets(  # noqa: SLF001
        {"value": {"result": [{"type": "envoy"}, "bad"]}}
    )
    assert buckets == [{"type": "envoy"}]

    assert runtime._hems_devices_groups(
        {"result": {"devices": [{"a": 1}, "bad"]}}
    ) == [  # noqa: SLF001
        {"a": 1}
    ]
    assert runtime._hems_devices_groups(
        {"result": {"devices": {"gateway": []}}}
    ) == [  # noqa: SLF001
        {"gateway": []}
    ]
    assert runtime._hems_devices_groups({"data": []}) == []  # noqa: SLF001

    assert (
        runtime._legacy_hems_devices_groups(  # noqa: SLF001
            {"result": [{"type": "hemsDevices", "devices": {"bad": True}}]}
        )
        == []
    )
    assert runtime._normalize_heatpump_member({"deviceUid": "abc"}) == {  # noqa: SLF001
        "deviceUid": "abc",
        "device_uid": "abc",
        "uid": "abc",
    }

    groups = [
        {
            "gateway": [
                "bad",
                {"name": "skip-retired"},
                {"device-uid": "UID-1", "name": "first"},
                {"device-uid": "UID-1", "name": "duplicate"},
                {"name": "empty-normalized"},
                {"serial": "SER-2", "name": "second"},
            ]
        }
    ]

    monkeypatch.setattr(
        runtime,
        "member_is_retired",
        lambda member: isinstance(member, dict)
        and member.get("name") == "skip-retired",
    )
    monkeypatch.setattr(
        runtime,
        "_normalize_hems_member",
        lambda member: (
            EmptyNormalized()
            if member.get("name") == "empty-normalized"
            else InventoryRuntime._normalize_hems_member(member)
        ),
    )

    found, members = runtime._extract_hems_group_members(
        groups, {"gateway"}
    )  # noqa: SLF001
    assert found is True
    assert members == [
        {"device-uid": "UID-1", "name": "first", "device_uid": "UID-1", "uid": "UID-1"},
        {"serial": "SER-2", "name": "second", "serial_number": "SER-2"},
    ]

    assert runtime._hems_bucket_type("") is None  # noqa: SLF001
    assert runtime._hems_bucket_type("heat-pump") == "heatpump"  # noqa: SLF001
    assert runtime._hems_bucket_type(BadStr()) is None  # noqa: SLF001
    original_hems_normalize_type_key = inv_mod.normalize_type_key
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.normalize_type_key",
        lambda raw: (
            None if raw == "raw hems type" else original_hems_normalize_type_key(raw)
        ),
    )
    assert runtime._hems_bucket_type("raw hems type") == "rawhemstype"  # noqa: SLF001
    assert (
        runtime._heatpump_worst_status_text({"warning": 1}) == "Warning"
    )  # noqa: SLF001
    assert (
        runtime._heatpump_worst_status_text({"not_reporting": 1}) == "Not Reporting"
    )  # noqa: SLF001
    assert runtime._heatpump_worst_status_text({"error": 1}) == "Error"  # noqa: SLF001

    values = iter(
        [
            {"name": "Dry Contact 1", "rating": 10},
            {"name": "Dry Contact 2", "enabled": True},
            {"meta": {"ignored": True}},
        ]
    )
    original_normalize_type_key = inv_mod.normalize_type_key
    original_sanitize_member = inv_mod.sanitize_member
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.normalize_type_key",
        lambda raw: (
            "dry_contact"
            if isinstance(raw, BadStr)
            else original_normalize_type_key(raw)
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.type_display_label",
        lambda _raw: "Dry Contact",
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.inventory_runtime.sanitize_member",
        lambda _member: next(values, {"name": None}),
    )

    valid, grouped, ordered = runtime._parse_devices_inventory_payload(  # noqa: SLF001
        [
            "bad",
            {"type": BadStr(), "devices": [{}, {}, {}]},
            {"type": "envoy", "devices": "bad"},
            {"type": "envoy", "devices": [None, {}, {"name": None}]},
        ]
    )
    assert valid is True
    assert ordered == ["dry_contact", "envoy"]
    assert grouped["dry_contact"]["devices"] == [
        {"name": "Dry Contact 1", "rating": 10},
        {"name": "Dry Contact 2", "enabled": True},
        {"meta": {"ignored": True}},
    ]
    assert grouped["envoy"]["devices"] == [{"name": None}, {"name": None}]

    with monkeypatch.context() as nested:
        nested.setattr(
            "custom_components.enphase_ev.inventory_runtime.normalize_type_key",
            original_hems_normalize_type_key,
        )
        nested.setattr(
            "custom_components.enphase_ev.inventory_runtime.sanitize_member",
            original_sanitize_member,
        )
        assert (
            runtime._parse_devices_inventory_payload(  # noqa: SLF001
                {
                    "result": [
                        {
                            "type": "drycontactloads",
                            "devices": [{"name": "x"}, {"name": "y"}],
                        },
                        {
                            "type": "envoy",
                            "devices": [{"name": "ignored"}],
                        },
                        {
                            "type": "encharge",
                            "devices": [
                                {"serial_number": "BAT-1"},
                                {"serial_number": "BAT-2", "name": "IQ Battery 5P"},
                            ],
                        },
                    ]
                }
            )[1]["encharge"]["model_summary"]
            == "IQ Battery 5P x1"
        )

    with monkeypatch.context() as nested:
        dry_values = iter(
            [
                {"rating": 10},
                {"meta": {"ignored": True}},
                {},
                {"serial_number": "ENV-1"},
            ]
        )
        nested.setattr(
            "custom_components.enphase_ev.inventory_runtime.sanitize_member",
            lambda _member: next(dry_values),
        )
        valid, grouped, _ordered = (
            runtime._parse_devices_inventory_payload(  # noqa: SLF001
                {
                    "result": [
                        {
                            "type": "drycontactloads",
                            "devices": [{}, {}, {}],
                        },
                        {"type": "envoy", "devices": [{}]},
                    ]
                }
            )
        )
        assert valid is True
        assert grouped["dry_contact"]["devices"] == [
            {"rating": 10},
            {"meta": {"ignored": True}},
        ]
        assert grouped["envoy"]["devices"] == [{"serial_number": "ENV-1"}]


def test_inventory_runtime_merge_heatpump_bucket_uses_worst_status_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord._type_device_buckets = {  # noqa: SLF001
        "iqevse": {
            "type_key": "iqevse",
            "type_label": "EV Chargers",
            "count": 1,
            "devices": [{"serial_number": "EV-1"}],
        }
    }
    coord._type_device_order = ["iqevse"]  # noqa: SLF001
    runtime._devices_inventory_ready = False  # noqa: SLF001
    runtime._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "heat-pump": [
                    {
                        "deviceType": "furnace",
                        "device-uid": "HP-1",
                        "name": "Aux Heat",
                        "statusText": "warning",
                        "firmware-version": "1.2.3",
                        "part-number": "PN-1",
                        "lastReportedAt": "2026-02-09T00:00:00Z",
                    }
                ]
            }
        }
    }
    refresh_calls: list[str] = []
    runtime._refresh_cached_topology = lambda: refresh_calls.append("refresh") or True  # type: ignore[method-assign]  # noqa: SLF001

    runtime._merge_heatpump_type_bucket()  # noqa: SLF001

    bucket = coord.inventory_view.type_bucket("heatpump")
    assert bucket is not None
    assert coord.inventory_view.iter_type_keys() == ["iqevse", "heatpump"]
    assert bucket["overall_status_text"] == "Warning"
    assert bucket["latest_reported_device"] == {
        "device_type": "FURNACE",
        "device_uid": "HP-1",
        "name": "Aux Heat",
        "status": "warning",
    }
    assert bucket["model_summary"] == "PN-1 x1"
    assert bucket["firmware_summary"] == "1.2.3 x1"
    info = coord.inventory_view.type_device_info("heatpump")
    assert info is not None
    assert info["sw_version"] == "1.2.3"


def test_inventory_runtime_system_dashboard_and_microinverter_edge_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    runtime = coord.inventory_runtime

    class BadText:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    coord._inverter_data = {  # noqa: SLF001
        "INV-BAD": "bad-payload",
        "INV-OK": {
            "name": "Inverter",
            "serial_number": "INV-OK",
            "status": "normal",
            "last_report": "2026-02-15T10:00:00Z",
            "array_name": BadText(),
            "fw1": BadText(),
        },
    }
    coord._inverter_order = ["INV-BAD", "INV-OK"]  # noqa: SLF001
    runtime._inverter_summary_counts = {  # noqa: SLF001
        "total": 1,
        "normal": 1,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
        "unknown": 0,
    }
    runtime._inverter_model_counts = {"IQ8": 1}  # noqa: SLF001

    runtime._merge_microinverter_type_bucket()  # noqa: SLF001

    bucket = coord.inventory_view.type_bucket("microinverter")
    assert bucket is not None
    assert bucket["count"] == 1
    assert bucket["array_summary"] is None
    assert bucket["firmware_summary"] is None

    runtime._devices_inventory_ready = False  # noqa: SLF001
    runtime._merge_microinverter_type_bucket()  # noqa: SLF001
    assert runtime._devices_inventory_ready is False  # noqa: SLF001

    runtime._system_dashboard_devices_details_raw = []  # type: ignore[assignment]  # noqa: SLF001
    assert runtime._system_dashboard_raw_payloads("envoy") == {}  # noqa: SLF001

    runtime._system_dashboard_devices_details_raw = {  # noqa: SLF001
        "encharge": {
            "encharges": {
                "encharges": [
                    {"serial_number": "OTHER", "id": "OTHER", "rssi_dbm": -70},
                    {"serial_number": "BAT-1", "id": "BAT-1", "rssi_dbm": -61},
                ]
            }
        }
    }
    runtime._battery_storage_data = {  # noqa: SLF001
        "BAT-1": {"serial_number": "BAT-1", "identity": "BAT-1"}
    }
    assert runtime.system_dashboard_battery_detail("BAT-1") == {"rssi_dbm": -61}


def test_merge_heatpump_type_bucket_preserves_fault_over_lifecycle_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "heat-pump": [
                    {
                        "device-type": "HEAT_PUMP",
                        "device-uid": "HP-1",
                        "name": "Primary Heat Pump",
                        "statusText": "Fault",
                        "pairing-status": "UNPAIRED",
                        "device-state": "INACTIVE",
                    }
                ]
            }
        }
    }

    runtime._merge_heatpump_type_bucket()  # noqa: SLF001

    bucket = coord.inventory_view.type_bucket("heatpump")
    assert bucket is not None
    assert bucket["overall_status_text"] == "Fault"
    assert bucket["status_counts"] == {
        "total": 1,
        "normal": 0,
        "warning": 0,
        "error": 1,
        "not_reporting": 0,
        "unknown": 0,
    }
    assert bucket["latest_reported_device"] is None


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_devices_inventory_logs_empty_grouped_summary(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client.devices_inventory = AsyncMock(return_value={"result": []})
    runtime._debug_devices_inventory_summary = MagicMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value={"devices": 0}
    )
    runtime._debug_log_summary_if_changed = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001

    await runtime._async_refresh_devices_inventory(force=True)  # noqa: SLF001

    runtime._debug_log_summary_if_changed.assert_called_once_with(  # noqa: SLF001
        "devices_inventory",
        "Device inventory discovery summary",
        {"devices": 0},
    )


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_hems_devices_unsupported_and_redaction_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._merge_heatpump_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._debug_log_summary_if_changed = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._debug_hems_inventory_summary = MagicMock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value={"hems": 1}
    )

    coord.client._hems_site_supported = True  # noqa: SLF001
    runtime._hems_devices_payload = {"existing": True}  # noqa: SLF001
    runtime._hems_devices_last_success_mono = time.monotonic()  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=RuntimeError("boom"))
    original_preflight = coord.heatpump_runtime.async_refresh_hems_support_preflight

    async def unsupported_on_error(*, force: bool = False) -> None:
        await original_preflight(force=force)
        coord.client._hems_site_supported = True  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=unsupported_on_error
    )
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload == {"existing": True}  # noqa: SLF001
    assert runtime._hems_devices_using_stale is True  # noqa: SLF001

    _clear_hems_inventory_endpoint_family(coord)

    async def unsupported_preflight(*, force: bool = False) -> None:
        coord.client._hems_site_supported = False  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=unsupported_preflight
    )
    coord.client.hems_devices = AsyncMock(side_effect=RuntimeError("unused"))
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001

    _clear_hems_inventory_endpoint_family(coord)

    async def supported_preflight(*, force: bool = False) -> None:
        coord.client._hems_site_supported = True  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=supported_preflight
    )
    runtime._hems_devices_payload = {"keep": True}  # noqa: SLF001
    runtime._hems_devices_last_success_mono = (
        time.monotonic() - HEMS_DEVICES_STALE_AFTER_S - 1
    )  # noqa: SLF001
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value="bad")
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_inventory_ready is False  # noqa: SLF001

    _clear_hems_inventory_endpoint_family(coord)

    async def unsupported_invalid_payload(*, force: bool = False) -> None:
        coord.client._hems_site_supported = False  # noqa: SLF001

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=unsupported_invalid_payload
    )
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(return_value="still bad")
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001

    _clear_hems_inventory_endpoint_family(coord)

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=supported_preflight
    )
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    monkeypatch.setattr(
        inventory_runtime_mod,
        "redact_battery_payload",
        lambda payload: "wrapped",
    )
    coord.client.hems_devices = AsyncMock(return_value={"result": {"devices": []}})
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload == {"value": "wrapped"}  # noqa: SLF001

    _clear_hems_inventory_endpoint_family(coord)

    async def unsupported_during_fetch(*, force: bool = False) -> None:
        coord.client._hems_site_supported = True  # noqa: SLF001

    async def fetch_marks_unsupported(*, refresh_data: bool = False):
        coord.client._hems_site_supported = False  # noqa: SLF001
        raise RuntimeError("boom")

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=unsupported_during_fetch
    )
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=fetch_marks_unsupported)
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001

    _clear_hems_inventory_endpoint_family(coord)

    async def fetch_invalid_marks_unsupported(*, refresh_data: bool = False):
        coord.client._hems_site_supported = False  # noqa: SLF001
        return "bad"

    coord.heatpump_runtime.async_refresh_hems_support_preflight = AsyncMock(  # type: ignore[assignment]  # noqa: SLF001
        side_effect=unsupported_during_fetch
    )
    runtime._hems_devices_cache_until = None  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(side_effect=fetch_invalid_marks_unsupported)
    await runtime._async_refresh_hems_devices(force=True)  # noqa: SLF001
    assert runtime._hems_devices_payload is None  # noqa: SLF001
    assert runtime._hems_inventory_ready is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_hems_devices_uses_fast_poll_cache_floor(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client._hems_site_supported = True  # noqa: SLF001
    coord.client.hems_devices = AsyncMock(
        return_value={"data": {"hems-devices": {"heat-pump": []}}}
    )
    monkeypatch.setattr(
        inventory_runtime_mod, "redact_battery_payload", lambda payload: payload
    )

    await runtime._async_refresh_hems_devices()

    coord.client.hems_devices.assert_awaited_once_with(refresh_data=False)
    assert runtime._hems_devices_cache_until is not None  # noqa: SLF001
    assert runtime._hems_devices_last_success_mono is not None  # noqa: SLF001
    assert runtime._hems_devices_cache_ttl_s() == pytest.approx(60.0)  # noqa: SLF001
    assert (
        runtime._hems_devices_cache_until
        - runtime._hems_devices_last_success_mono  # noqa: SLF001
    ) == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_pagination_and_error_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    def _clear_family_windows() -> None:
        for family in ("inverter_inventory", "inverter_status", "inverter_production"):
            health = coord._endpoint_family_state(family)  # noqa: SLF001
            health.next_retry_mono = None
            health.next_retry_utc = None
            health.cooldown_active = False
        coord._inverters_inventory_cache_until = None  # noqa: SLF001
        coord._inverter_status_cache_until = None  # noqa: SLF001
        coord._inverter_production_cache_until = None  # noqa: SLF001

    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    runtime._merge_microinverter_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    runtime._merge_heatpump_type_bucket = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001

    class BadStatusType:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    pages: list[tuple[int, int]] = []

    async def inventory_fetcher(*args, **kwargs):
        if kwargs:
            offset = kwargs["offset"]
            pages.append((kwargs["limit"], offset))
            if offset == 0:
                return {
                    "total": 3,
                    "normal_count": 5,
                    "warning_count": 5,
                    "error_count": 5,
                    "not_reporting": 5,
                    "inverters": [
                        {"serial_number": "INV-A", "name": "IQ8", "status": "normal"},
                        {"serial_number": "INV-B", "name": "IQ8", "status": "offline"},
                    ],
                    "panel_info": {
                        "manufacturer": "  ",
                        "model": None,
                        "rating": [],
                    },
                }
            if offset == 2:
                return {
                    "total": 3,
                    "inverters": [
                        {"serial_number": "INV-C", "name": "IQ7", "status": "warning"},
                        {"serial_number": "   ", "name": "blank"},
                        {"serial_number": "INV-RET", "name": "retired"},
                    ],
                }
            raise AssertionError(offset)
        return {"total": 1, "inverters": [{"serial_number": "INV-FB", "name": "IQ7"}]}

    coord.client.inverters_inventory = AsyncMock(side_effect=inventory_fetcher)
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {
                "serialNum": "INV-A",
                "deviceId": 1,
                "statusCode": "normal",
                "type": BadStatusType(),
            },
            "1002": "bad",
            "1003": {"serialNum": "", "deviceId": 3},
        }
    )
    coord.client.inverter_production = AsyncMock(return_value={"production": []})
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "serial_number": "INV-A",
            "lifetime_production_wh": "bad",
            "lifetime_query_start_date": "2022-08-01",
            "lifetime_query_end_date": "2026-02-01",
        },
        "INV-B": {
            "serial_number": "INV-B",
            "inverter_id": "1002",
            "device_id": 2,
            "status_code": "warning",
            "lifetime_production_wh": "still-bad",
        },
        "INV-C": {
            "serial_number": "INV-C",
            "lifetime_production_wh": 20.0,
            "lifetime_query_start_date": "2022-08-05",
            "lifetime_query_end_date": "2026-02-05",
        },
    }

    monkeypatch.setattr(
        runtime,
        "member_is_retired",
        lambda item: item.get("serial_number") == "INV-RET",
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    assert pages == [(1000, 0), (1000, 2)]
    assert coord.iter_inverter_serials() == ["INV-A", "INV-B", "INV-C"]
    assert coord.inverter_data("INV-A")["lifetime_production_wh"] is None
    assert coord.inverter_data("INV-A")["lifetime_query_start_date"] == "2022-08-01"
    assert coord.inverter_data("INV-B")["lifetime_query_start_date"] == "2022-08-10"
    assert (
        coord.inverter_data("INV-B")["lifetime_query_end_date"]
        == coord._site_local_current_date()
    )  # noqa: SLF001
    assert coord.inverter_data("INV-B")["lifetime_production_wh"] is None
    assert coord.inverter_data("INV-B")["inverter_type"] is None
    assert coord.inverter_data("INV-C")["lifetime_production_wh"] == 20.0
    assert runtime._inverter_summary_counts == {  # noqa: SLF001
        "total": 3,
        "normal": 1,
        "warning": 1,
        "error": 1,
        "not_reporting": 0,
        "unknown": 0,
    }
    assert runtime._inverter_panel_info is None  # noqa: SLF001

    coord.client.inverters_inventory = AsyncMock(return_value="bad")
    _clear_family_windows()
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == ["INV-A", "INV-B", "INV-C"]

    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 2,
            "normal_count": 5,
            "warning_count": 5,
            "error_count": 5,
            "not_reporting": 5,
            "inverters": [
                {"serial_number": "INV-X", "name": "IQX", "status": "mystery"},
                {"serial_number": "INV-Y", "name": "IQY", "status": "mystery"},
            ],
        }
    )
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(return_value={})
    coord._inverter_data = {}  # noqa: SLF001
    _clear_family_windows()
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert runtime._inverter_summary_counts == {  # noqa: SLF001
        "total": 2,
        "normal": 2,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
        "unknown": 0,
    }

    async def legacy_inventory_fetcher(*args, **kwargs):
        if kwargs:
            raise TypeError("legacy")
        return {"total": 1, "inverters": [{"serial_number": "INV-FB", "name": "IQ7"}]}

    coord.client.inverters_inventory = AsyncMock(side_effect=legacy_inventory_fetcher)
    coord.client.inverter_status = AsyncMock(return_value=[])
    coord.client.inverter_production = AsyncMock(return_value="bad")
    _clear_family_windows()
    await runtime._async_refresh_inverters()  # noqa: SLF001
    coord.client.inverter_production.assert_awaited_once()

    async def pagination_typeerror_fetcher(*args, **kwargs):
        if kwargs.get("offset") == 0:
            return {
                "total": 2,
                "inverters": [{"serial_number": "INV-1", "name": "IQ7"}],
            }
        raise TypeError("legacy page")

    coord.client.inverters_inventory = AsyncMock(
        side_effect=pagination_typeerror_fetcher
    )
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(
        return_value={"production": {"1001": "bad"}}
    )
    coord._inverter_data = {
        "INV-1": {"serial_number": "INV-1", "inverter_id": "1001"}
    }  # noqa: SLF001
    _clear_family_windows()
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == ["INV-1"]
    assert coord.inverter_data("INV-1")["lifetime_production_wh"] is None

    async def pagination_non_list_fetcher(*args, **kwargs):
        if kwargs.get("offset") == 0:
            return {
                "total": 2,
                "inverters": [{"serial_number": "INV-2", "name": "IQ7"}],
            }
        return {"total": 2, "inverters": "bad"}

    coord.client.inverters_inventory = AsyncMock(
        side_effect=pagination_non_list_fetcher
    )
    _clear_family_windows()
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == ["INV-2"]

    async def pagination_empty_page_fetcher(*args, **kwargs):
        if kwargs.get("offset") == 0:
            return {
                "total": 2,
                "inverters": [{"serial_number": "INV-3", "name": "IQ7"}],
            }
        return {"total": 2, "inverters": []}

    coord.client.inverters_inventory = AsyncMock(
        side_effect=pagination_empty_page_fetcher
    )
    _clear_family_windows()
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == ["INV-3"]

    async def pagination_total_growth_fetcher(*args, **kwargs):
        if kwargs.get("offset") == 0:
            return {
                "total": 2,
                "inverters": [{"serial_number": "INV-4", "name": "IQ7"}],
            }
        return {
            "total": 3,
            "inverters": [{"serial_number": "INV-5", "name": "IQ8"}],
        }

    coord.client.inverters_inventory = AsyncMock(
        side_effect=pagination_total_growth_fetcher
    )
    _clear_family_windows()
    await runtime._async_refresh_inverters()  # noqa: SLF001
    assert coord.iter_inverter_serials() == ["INV-4", "INV-5"]


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_uses_cached_payloads_during_cooldown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    now = time.monotonic()

    coord._devices_inventory_payload = {"curr_date_site": "2026-02-09"}  # noqa: SLF001
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord._inverters_inventory_payload = {  # noqa: SLF001
        "total": 1,
        "inverters": [{"serial_number": "INV-A", "name": "IQ7", "status": "normal"}],
    }
    coord._inverter_status_payload = {  # noqa: SLF001
        "1001": {
            "serialNum": "INV-A",
            "deviceId": 11,
            "statusCode": "normal",
            "type": "IQ7",
        }
    }
    coord._inverter_production_payload = {  # noqa: SLF001
        "production": {"1001": 321.0},
        "start_date": "2022-08-10",
        "end_date": "2026-02-09",
    }
    coord._inverter_production_cache_key = (  # noqa: SLF001
        "2022-08-10",
        "2026-02-09",
    )
    for family in ("inverter_inventory", "inverter_status", "inverter_production"):
        health = coord._endpoint_family_state(family)  # noqa: SLF001
        health.next_retry_mono = now + 600
        health.cooldown_active = True
        health.last_success_mono = now

    coord.client.inverters_inventory = AsyncMock(side_effect=AssertionError("unused"))
    coord.client.inverter_status = AsyncMock(side_effect=AssertionError("unused"))
    coord.client.inverter_production = AsyncMock(side_effect=AssertionError("unused"))

    await runtime._async_refresh_inverters()  # noqa: SLF001

    coord.client.inverters_inventory.assert_not_awaited()
    coord.client.inverter_status.assert_not_awaited()
    coord.client.inverter_production.assert_not_awaited()
    assert coord.iter_inverter_serials() == ["INV-A"]
    assert coord.inverter_data("INV-A")["lifetime_production_wh"] == 321.0


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_uses_success_cache_ttls(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    coord._devices_inventory_payload = {"curr_date_site": "2026-02-09"}  # noqa: SLF001
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "inverters": [
                {"serial_number": "INV-A", "name": "IQ7", "status": "normal"}
            ],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {
                "serialNum": "INV-A",
                "deviceId": 11,
                "statusCode": "normal",
                "type": "IQ7",
            }
        }
    )
    coord.client.inverter_production = AsyncMock(
        return_value={
            "production": {"1001": 456.0},
            "start_date": "2022-08-10",
            "end_date": "2026-02-09",
        }
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    assert coord.client.inverters_inventory.await_count == 1
    assert coord.client.inverter_status.await_count == 1
    assert coord.client.inverter_production.await_count == 1
    assert coord._inverters_inventory_cache_until is not None  # noqa: SLF001
    assert coord._inverter_status_cache_until is not None  # noqa: SLF001
    assert coord._inverter_production_cache_until is not None  # noqa: SLF001

    await runtime._async_refresh_inverters()  # noqa: SLF001

    assert coord.client.inverters_inventory.await_count == 1
    assert coord.client.inverter_status.await_count == 1
    assert coord.client.inverter_production.await_count == 1
    assert coord.inverter_data("INV-A")["lifetime_production_wh"] == 456.0


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_refetches_production_after_ttl(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    coord._devices_inventory_payload = {"curr_date_site": "2026-02-09"}  # noqa: SLF001
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "inverters": [
                {"serial_number": "INV-A", "name": "IQ7", "status": "normal"}
            ],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {
                "serialNum": "INV-A",
                "deviceId": 11,
                "statusCode": "normal",
                "type": "IQ7",
            }
        }
    )
    coord.client.inverter_production = AsyncMock(
        side_effect=[
            {
                "production": {"1001": 456.0},
                "start_date": "2022-08-10",
                "end_date": "2026-02-09",
            },
            {
                "production": {"1001": 789.0},
                "start_date": "2022-08-10",
                "end_date": "2026-02-09",
            },
        ]
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    production_health = coord._endpoint_family_state(
        "inverter_production"
    )  # noqa: SLF001
    production_health.next_retry_mono = time.monotonic() - 1
    production_health.next_retry_utc = None
    production_health.cooldown_active = False
    monkeypatch.setattr(
        time,
        "monotonic",
        lambda: (production_health.last_success_mono or 0.0) + 601.0,
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    assert coord.client.inverter_production.await_count == 2
    assert coord.inverter_data("INV-A")["lifetime_production_wh"] == 789.0


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_not_blocked_by_topology_family_ttl(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    now = time.monotonic()

    coord._devices_inventory_payload = {"curr_date_site": "2026-02-09"}  # noqa: SLF001
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    topology_health = coord._endpoint_family_state("inventory_topology")  # noqa: SLF001
    topology_health.next_retry_mono = now + 21_600
    topology_health.cooldown_active = True
    topology_health.last_success_mono = now
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "inverters": [
                {"serial_number": "INV-A", "name": "IQ7", "status": "normal"}
            ],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {
                "serialNum": "INV-A",
                "deviceId": 11,
                "statusCode": "normal",
                "type": "IQ7",
            }
        }
    )
    coord.client.inverter_production = AsyncMock(
        return_value={
            "production": {"1001": 456.0},
            "start_date": "2022-08-10",
            "end_date": "2026-02-09",
        }
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    coord.client.inverters_inventory.assert_awaited_once()
    assert coord.iter_inverter_serials() == ["INV-A"]


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_manual_bypass_refetches_same_day_production(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    now = time.monotonic()

    coord._devices_inventory_payload = {"curr_date_site": "2026-02-09"}  # noqa: SLF001
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord._endpoint_manual_bypass_active = True  # noqa: SLF001
    for family in ("inverter_inventory", "inverter_status", "inverter_production"):
        health = coord._endpoint_family_state(family)  # noqa: SLF001
        health.next_retry_mono = now + 600
        health.cooldown_active = True
        health.last_success_mono = now
    coord._inverter_production_payload = {  # noqa: SLF001
        "production": {"1001": 123.0},
        "start_date": "2022-08-10",
        "end_date": "2026-02-09",
    }
    coord._inverter_production_cache_key = (  # noqa: SLF001
        "2022-08-10",
        "2026-02-09",
    )
    coord.client.inverters_inventory = AsyncMock(
        return_value={
            "total": 1,
            "inverters": [
                {"serial_number": "INV-A", "name": "IQ7", "status": "normal"}
            ],
        }
    )
    coord.client.inverter_status = AsyncMock(
        return_value={
            "1001": {
                "serialNum": "INV-A",
                "deviceId": 11,
                "statusCode": "normal",
                "type": "IQ7",
            }
        }
    )
    coord.client.inverter_production = AsyncMock(
        return_value={
            "production": {"1001": 456.0},
            "start_date": "2022-08-10",
            "end_date": "2026-02-09",
        }
    )

    await runtime._async_refresh_inverters()  # noqa: SLF001

    coord.client.inverter_production.assert_awaited_once()
    assert coord.inverter_data("INV-A")["lifetime_production_wh"] == 456.0
    assert (
        coord._endpoint_family_state("inverter_production").next_retry_utc is not None
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_system_dashboard_records_endpoint_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.client.devices_tree = AsyncMock(side_effect=RuntimeError("tree"))
    coord.client.devices_details = AsyncMock(side_effect=RuntimeError("detail"))

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    health = coord._endpoint_family_state("inventory_topology")  # noqa: SLF001
    assert health.consecutive_failures == 1
    assert health.cooldown_active is True


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_system_dashboard_respects_cache_and_details_error_branch(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._system_dashboard_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.devices_tree = AsyncMock(side_effect=AssertionError("unused"))
    coord.client.devices_details = AsyncMock(side_effect=AssertionError("unused"))

    await runtime._async_refresh_system_dashboard()  # noqa: SLF001

    coord.client.devices_tree.assert_not_awaited()
    coord.client.devices_details.assert_not_awaited()

    runtime._system_dashboard_cache_until = None  # noqa: SLF001
    coord.client.devices_tree = None
    coord.client.devices_details = AsyncMock(side_effect=RuntimeError("detail"))

    await runtime._async_refresh_system_dashboard(force=True)  # noqa: SLF001

    health = coord._endpoint_family_state("inventory_topology")  # noqa: SLF001
    assert health.consecutive_failures >= 1


@pytest.mark.asyncio
async def test_inventory_runtime_refresh_inverters_handles_inventory_exception_and_cached_bad_production(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    now = time.monotonic()

    coord._devices_inventory_payload = {"curr_date_site": "2026-02-09"}  # noqa: SLF001
    coord.energy._site_energy_meta = {"start_date": "2022-08-10"}  # noqa: SLF001
    coord._inverters_inventory_payload = {  # noqa: SLF001
        "total": 1,
        "inverters": [{"serial_number": "INV-A", "name": "IQ7", "status": "normal"}],
    }
    coord._inverter_status_payload = {  # noqa: SLF001
        "1001": {
            "serialNum": "INV-A",
            "deviceId": 11,
            "statusCode": "normal",
            "type": "IQ7",
        }
    }
    coord._inverter_production_payload = {  # noqa: SLF001
        "production": {"1001": 222.0},
        "start_date": "2022-08-10",
        "end_date": "2026-02-09",
    }
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"serial_number": "INV-A", "inverter_id": "1001"}
    }
    coord._inverter_production_cache_key = (  # noqa: SLF001
        "2022-08-10",
        "2026-02-09",
    )
    status_health = coord._endpoint_family_state("inverter_status")  # noqa: SLF001
    status_health.last_success_mono = now
    production_health = coord._endpoint_family_state(
        "inverter_production"
    )  # noqa: SLF001
    production_health.last_success_mono = now

    coord.client.inverters_inventory = AsyncMock(side_effect=RuntimeError("boom"))
    coord.client.inverter_status = AsyncMock(return_value={})
    coord.client.inverter_production = AsyncMock(return_value="bad")

    await runtime._async_refresh_inverters()  # noqa: SLF001

    assert coord.iter_inverter_serials() == ["INV-A"]
    assert coord.inverter_data("INV-A")["lifetime_production_wh"] == 222.0


def test_inventory_runtime_misc_helper_edges(coordinator_factory) -> None:
    runtime = coordinator_factory().inventory_runtime

    assert runtime._normalize_inverter_status("") == "unknown"  # noqa: SLF001
    assert (
        runtime._inverter_connectivity_state({"total": 2, "unknown": 2}) == "unknown"
    )  # noqa: SLF001


def test_inventory_runtime_helper_timing_diagnostics(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.inventory_runtime

    assert (
        1.0 <= runtime._seconds_until_next_site_local_day() <= 86_400.0
    )  # noqa: SLF001

    coord._inverter_production_cache_until = 110.0  # noqa: SLF001
    coord._inverter_production_cache_key = ("2022-08-10", "2026-02-09")  # noqa: SLF001
    health = coord._endpoint_family_state("inverter_production")  # noqa: SLF001
    health.last_success_mono = 70.0
    monkeypatch.setattr(inventory_runtime_mod.time, "monotonic", lambda: 100.0)

    payload = runtime.inverter_diagnostics_payloads()

    assert payload["production_cache_key"] == ("2022-08-10", "2026-02-09")
    assert payload["production_cache_remaining_seconds"] == 10.0
    assert payload["production_cache_age_seconds"] == 30.0


def test_inventory_runtime_refresh_cached_topology_handles_snapshot_errors(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory(serials=[]).inventory_runtime
    runtime._rebuild_inventory_summary_caches = lambda: None  # type: ignore[method-assign]  # noqa: SLF001

    def _boom():
        raise RuntimeError("snapshot failed")

    runtime._current_topology_snapshot = _boom  # type: ignore[method-assign]  # noqa: SLF001

    assert runtime._refresh_cached_topology() is False  # noqa: SLF001
