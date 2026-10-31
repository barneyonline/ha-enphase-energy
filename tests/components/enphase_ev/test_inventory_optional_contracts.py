"""Inventory and heat-pump refresh boundaries with sparse and expired metadata."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.inventory_runtime import InventoryRuntime
from tests.components.enphase_ev.test_heatpump_runtime import (
    _seed_heatpump_auth_test_topology,
)


@pytest.mark.parametrize("details", [{}, {"phases": {}}, {"phases": {"L1": None}}])
def test_gateway_phase_map_does_not_invent_phases(details):
    assert InventoryRuntime._normalize_gateway_phase_map({"GW": details}) == {"GW": {}}


def test_inventory_refresh_floor_without_heatpump_extension(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    monkeypatch.setattr(coord.heatpump_runtime, "hems_refresh_floor_s", None)
    assert coord.inventory_runtime._hems_refresh_floor_s() > 0


def test_hems_debug_keys_ignore_blank_and_non_group_fields(coordinator_factory):
    runtime = coordinator_factory().inventory_runtime
    runtime._hems_devices_payload = {"result": {"devices": [{" ": [], "metadata": {}}]}}
    assert runtime._debug_hems_inventory_summary()["group_keys"] == []


def test_ending_topology_batch_without_begin_does_not_underflow(coordinator_factory):
    runtime = coordinator_factory().inventory_runtime
    assert runtime._end_topology_refresh_batch() is False
    assert runtime._topology_refresh_suppressed == 0


def test_inventory_wrapped_payload_without_result_is_empty():
    assert (
        InventoryRuntime._devices_inventory_buckets({"value": {"result": None}}) == []
    )


def test_battery_inventory_without_names_has_no_model_summary(coordinator_factory):
    runtime = coordinator_factory().inventory_runtime
    valid, buckets, order = runtime._parse_devices_inventory_payload(
        {"result": [{"type": "encharge", "devices": [{"serial_number": "BAT"}]}]}
    )
    assert valid
    assert buckets["encharge"]["count"] == 1
    assert "model_summary" not in buckets["encharge"]


def test_gateway_summary_accepts_reported_boolean_connectivity(coordinator_factory):
    coord = coordinator_factory()
    coord._type_device_buckets["envoy"]["devices"] = [
        {"serial_number": "GW", "connected": True}
    ]
    summary = coord.inventory_runtime._build_gateway_inventory_summary()
    assert summary["connected_devices"] == 1


def test_heatpump_inventory_uses_newest_report_and_omits_future_cache_age(
    coordinator_factory,
):
    coord = coordinator_factory()
    _seed_heatpump_auth_test_topology(coord)
    coord._type_device_buckets["heatpump"]["devices"] = [
        {"device_uid": "HP-new", "last_report": 1700000000},
        {"device_uid": "HP-old", "last_report": 1600000000},
    ]
    runtime = coord.inventory_runtime
    runtime._hems_devices_last_success_mono = time.monotonic() + 100
    summary = runtime._build_heatpump_inventory_summary()
    assert summary["latest_reported_device"]["device_uid"] == "HP-new"
    assert summary["hems_last_success_age_s"] is None


def test_metadata_invalidation_without_inventory_view_extension(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    monkeypatch.setattr(coord, "inventory_view", SimpleNamespace())
    coord.inventory_runtime._invalidate_device_metadata()


def test_invalid_next_page_does_not_override_other_paging_metadata():
    assert (
        InventoryRuntime._inverter_parameter_has_more_pages(
            {"next_page": "unknown", "total_pages": 2}, 1
        )
        is True
    )


@pytest.mark.parametrize("kind", ["inventory", "hems"])
def test_expired_inventory_cache_is_refreshable(coordinator_factory, monkeypatch, kind):
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    if kind == "inventory":
        runtime._devices_inventory_cache_until = time.monotonic() - 1
        monkeypatch.setattr(
            runtime, "gateway_phase_map_refresh_due", lambda **kwargs: False
        )
        assert runtime.devices_inventory_refresh_due()
    else:
        _seed_heatpump_auth_test_topology(coord)
        runtime._hems_devices_cache_until = time.monotonic() - 1
        assert runtime.hems_devices_refresh_due()


async def test_expired_dashboard_without_detail_or_today_endpoints(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._system_dashboard_cache_until = time.monotonic() - 1
    coord.client.devices_tree = AsyncMock(return_value=None)
    monkeypatch.setattr(coord.client, "devices_details", None)
    monkeypatch.setattr(coord.client, "pv_system_today", None)
    await runtime._async_refresh_system_dashboard()
    coord.client.devices_tree.assert_awaited_once()


@pytest.mark.parametrize(
    "cache_attr", ["_heatpump_power_cache_until", "_heatpump_power_backoff_until"]
)
async def test_heatpump_power_resumes_after_expired_cache_and_backoff(
    coordinator_factory, monkeypatch, cache_attr
):
    coord = coordinator_factory()
    _seed_heatpump_auth_test_topology(coord)
    runtime = coord.heatpump_runtime
    setattr(runtime, cache_attr, time.monotonic() - 1)
    fetch = AsyncMock()
    monkeypatch.setattr(runtime, "_async_refresh_heatpump_runtime_state", AsyncMock())
    monkeypatch.setattr(runtime, "_async_refresh_heatpump_daily_consumption", fetch)
    assert runtime.heatpump_power_refresh_due()
    await runtime._async_refresh_heatpump_power()
    fetch.assert_awaited_once()


async def test_expired_hems_preflight_with_no_support_flag_keeps_unknown(
    coordinator_factory,
):
    coord = coordinator_factory()
    runtime = coord.heatpump_runtime
    runtime._hems_support_preflight_cache_until = time.monotonic() - 1
    coord.client.system_dashboard_summary = AsyncMock(return_value={})
    await runtime.async_refresh_hems_support_preflight()
    assert coord.client.hems_site_supported is None
    coord.client.system_dashboard_summary.assert_awaited_once()


async def test_expired_heatpump_diagnostics_without_optional_endpoints(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    _seed_heatpump_auth_test_topology(coord)
    runtime = coord.heatpump_runtime
    runtime._heatpump_runtime_diagnostics_cache_until = time.monotonic() - 1
    monkeypatch.setattr(runtime, "_async_refresh_heatpump_runtime_state", AsyncMock())
    for endpoint in ("show_livestream", "heat_pump_events_json", "iq_er_events_json"):
        monkeypatch.setattr(coord.client, endpoint, None)
    await runtime.async_ensure_heatpump_runtime_diagnostics()
    assert runtime._heatpump_events_payloads == []


def test_primary_heatpump_uid_skips_members_without_identity(coordinator_factory):
    coord = coordinator_factory()
    _seed_heatpump_auth_test_topology(coord)
    coord._type_device_buckets["heatpump"]["devices"].insert(
        0, {"device_type": "HEAT_PUMP"}
    )
    assert coord.heatpump_runtime._heatpump_primary_device_uid() == "HP-1"


async def test_runtime_readback_survives_transient_missing_inventory_members(
    coordinator_factory,
):
    coord = coordinator_factory()
    _seed_heatpump_auth_test_topology(coord)
    runtime = coord.heatpump_runtime
    runtime._heatpump_runtime_state = {"device_uid": "HP-1"}
    coord._type_device_buckets["heatpump"]["devices"] = []
    coord.client.hems_heatpump_state = AsyncMock(
        return_value={"device_uid": "HP-1", "heatpump_status": "IDLE"}
    )
    await runtime._async_refresh_heatpump_runtime_state(force=True)
    assert runtime._heatpump_runtime_state["heatpump_status"] == "IDLE"
    assert "member_name" not in runtime._heatpump_runtime_state


def test_heatpump_consumption_requires_bucket_list(coordinator_factory):
    coord = coordinator_factory()
    _seed_heatpump_auth_test_topology(coord)
    assert (
        coord.heatpump_runtime._select_heatpump_energy_consumption_entry(
            {"data": {"heat-pump": [{"device_uid": "HP-1", "consumption": None}]}}
        )
        is None
    )


def test_heatpump_nested_total_skips_unusable_first_alias(coordinator_factory):
    runtime = coordinator_factory().heatpump_runtime
    assert runtime._site_today_heatpump_numeric_total({"value": None}) is None


@pytest.mark.parametrize(
    "freshness",
    [
        None,
        {"GONE": {"power": 1}, "INV": {}},
        {None: None, "power": 1},
        {"temperature": 1},
    ],
)
async def test_telemetry_normalizes_legacy_freshness_without_fetch_extensions(
    coordinator_factory, monkeypatch, freshness
):
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._inverter_parameter_telemetry = {"INV": {"power": 1}}
    runtime._inverter_parameter_success_mono = freshness
    monkeypatch.setattr(
        coord, "_endpoint_family_should_run", lambda *args, **kwargs: False
    )
    result = await runtime._async_refresh_inverter_parameter_telemetry(["INV"])
    assert set(result) <= {"INV"}


@pytest.mark.parametrize("columns", [None, {"columns": [{"name": " "}]}])
async def test_telemetry_discovery_without_column_names(
    coordinator_factory, monkeypatch, columns
):
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    runtime._inverter_parameter_ids = []
    coord.client.system_dashboard_master_data = AsyncMock(return_value={})
    coord.client.system_dashboard_data_columns = (
        None if columns is None else AsyncMock(return_value=columns)
    )
    coord.client.system_dashboard_parameter_view = None
    assert await runtime._async_refresh_inverter_parameter_telemetry(["INV"]) == {}


async def test_expired_hems_cache_fetches_without_stale_payload(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    _seed_heatpump_auth_test_topology(coord)
    coord.inventory_runtime._hems_devices_cache_until = 0.001
    coord.client.hems_devices = AsyncMock(return_value={})
    monkeypatch.setattr(
        coord.heatpump_runtime, "async_refresh_hems_support_preflight", AsyncMock()
    )
    await coord.inventory_runtime._async_refresh_hems_devices()
    coord.client.hems_devices.assert_awaited_once()


async def test_empty_inventory_bucket_does_not_hide_later_active_bucket(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord.client.devices_inventory = AsyncMock(
        return_value={
            "result": [
                {"type": "envoy", "devices": []},
                {"type": "encharge", "devices": [{"serial_number": "BAT"}]},
            ]
        }
    )
    await coord.inventory_runtime._async_refresh_devices_inventory(force=True)
    assert coord.inventory_runtime.type_bucket("encharge")["count"] == 1


async def test_hems_cooldown_without_previous_payload_does_not_publish_stale(
    coordinator_factory,
):
    coord = coordinator_factory()
    _seed_heatpump_auth_test_topology(coord)
    runtime = coord.inventory_runtime
    runtime._hems_devices_payload = None
    health = coord._endpoint_family_state("hems_inventory")
    health.cooldown_active = True
    health.next_retry_mono = time.monotonic() + 100
    await runtime._async_refresh_hems_devices(force=True)
    assert runtime._hems_devices_payload is None


async def test_telemetry_policy_without_stale_deadline(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    monkeypatch.setattr(coord, "_endpoint_family_policy", lambda family: None)
    monkeypatch.setattr(
        coord, "_endpoint_family_should_run", lambda *args, **kwargs: False
    )
    coord.inventory_runtime._inverter_parameter_telemetry = {"INV": {"power": 1}}
    result = await coord.inventory_runtime._async_refresh_inverter_parameter_telemetry(
        ["INV"]
    )
    assert result["INV"]["power"] == 1


def test_recommended_heatpump_without_primary_identifier(coordinator_factory):
    coord = coordinator_factory()
    coord._type_device_buckets["heatpump"] = {
        "devices": [{"status": "recommended", "device_type": "HEAT_PUMP"}]
    }
    assert coord.heatpump_runtime._heatpump_power_candidate_is_recommended("unknown")


async def test_heatpump_diagnostics_without_matching_events_fetcher(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    _seed_heatpump_auth_test_topology(coord)
    runtime = coord.heatpump_runtime
    monkeypatch.setattr(runtime, "_async_refresh_heatpump_runtime_state", AsyncMock())
    monkeypatch.setattr(
        runtime, "_async_refresh_heatpump_daily_consumption", AsyncMock()
    )
    monkeypatch.setattr(runtime, "_async_refresh_heatpump_power", AsyncMock())
    coord.client.show_livestream = None
    coord.client.heat_pump_events_json = None
    coord.client.iq_er_events_json = AsyncMock(return_value={})
    await runtime.async_ensure_heatpump_runtime_diagnostics(force=True)
    assert any(
        row["events_namespace"] == "heat_pump"
        for row in runtime._heatpump_events_payloads
    )


@pytest.mark.parametrize(
    "mode", ["invalid_status", "cooldown", "no_production_fetcher", "fresh_production"]
)
async def test_inverter_refresh_without_reusable_family_cache(
    coordinator_factory, monkeypatch, mode
):
    coord = coordinator_factory()
    runtime = coord.inventory_runtime
    coord.energy._site_energy_meta = {"start_date": "2022-01-01"}
    runtime._devices_inventory_payload = {"curr_date_site": "2026-10-31"}
    coord.client.inverters_inventory = AsyncMock(
        return_value={"total": 1, "inverters": [{"serial_number": "INV"}]}
    )
    coord.client.inverter_status = AsyncMock(return_value=None)
    coord.client.inverter_production = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(coord, "_endpoint_family_can_use_stale", lambda family: False)
    monkeypatch.setattr(
        runtime,
        "_async_refresh_inverter_parameter_telemetry",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        runtime, "_async_inverter_dashboard_inventory", AsyncMock(return_value=[])
    )
    if mode == "cooldown":
        for family in ("inverter_status", "inverter_production"):
            health = coord._endpoint_family_state(family)
            health.next_retry_mono = time.monotonic() + 100
            health.cooldown_active = True
    elif mode == "no_production_fetcher":
        coord.client.inverter_production = None
    elif mode == "fresh_production":
        runtime._inverter_production_cache_key = ("2022-01-01", "2026-10-31")
        runtime._inverter_production_payload = {"production": {}}
        runtime._inverter_production_cache_until = time.monotonic() + 100
        runtime.inverters_refresh_due()
    await runtime._async_refresh_inverters()
    assert not runtime._inverter_status_payload
    if mode == "invalid_status":
        assert coord._endpoint_family_state("inverter_status").last_error
        coord.client.inverter_production.assert_awaited_once()
