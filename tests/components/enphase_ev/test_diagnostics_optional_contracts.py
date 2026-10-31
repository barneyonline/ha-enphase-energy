"""Diagnostics tolerate unavailable optional components and malformed metadata."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr

from custom_components.enphase_ev import diagnostics
from custom_components.enphase_ev.const import (
    DOMAIN,
    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
)
from custom_components.enphase_ev.coordinator_diagnostics import CoordinatorDiagnostics
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from tests.components.enphase_ev.test_heatpump_runtime import (
    _seed_heatpump_auth_test_topology,
)


def test_repair_context_without_service_list_does_not_add_one():
    assert CoordinatorDiagnostics._repair_issue_metrics(
        "other", {"hems_auth_reason": "private", "degraded_services": None}
    ) == {"degraded_services": None}


def test_site_metrics_omit_negative_cache_ages(coordinator_factory):
    coord = coordinator_factory()
    fields = {
        "_grid_control_check_last_success_mono": "grid_control_last_success_age_s",
        "_grid_mode_status_last_success_mono": "grid_mode_status_last_success_age_s",
        "_grid_outage_context_last_success_mono": "grid_outage_context_last_success_age_s",
        "_dry_contact_settings_last_success_mono": "dry_contact_settings_last_success_age_s",
    }
    for attr in fields:
        setattr(coord, attr, time.monotonic() + 100)
    metrics = coord.collect_site_metrics()
    assert all(key not in metrics for key in fields.values())


def test_site_metrics_without_optional_managers(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    with monkeypatch.context() as patch:
        patch.setattr(coord, "energy", None)
        patch.setattr(coord, "evse_timeseries", None)
        metrics = coord.collect_site_metrics()
        health = coord.diagnostics.payload_health_diagnostics()
    assert "site_energy" not in metrics
    assert "evse_timeseries" not in health


def test_firmware_metrics_ignore_non_mapping_status(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    monkeypatch.setattr(
        coord,
        "firmware_catalog_manager",
        SimpleNamespace(status_snapshot=lambda: []),
        raising=False,
    )
    metrics = coord.collect_site_metrics()
    assert "firmware_catalog_generated_at" not in metrics


def test_grid_health_without_stale_check_is_degraded(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    health = coord._endpoint_family_state("grid_mode_status")
    health.consecutive_failures = 1
    monkeypatch.setattr(coord, "_endpoint_family_can_use_stale", None)
    snapshot = coord.diagnostics.endpoint_family_health_diagnostics()
    assert snapshot["grid_mode_status"]["degraded"] is True


def test_repairs_without_retry_timestamp_still_report(
    coordinator_factory, mock_issue_registry, config_entry
):
    coord = coordinator_factory()
    coord.config_entry = config_entry
    object.__setattr__(
        config_entry, "options", {OPT_DEGRADED_SERVICE_REPAIR_ISSUES: True}
    )
    _seed_heatpump_auth_test_topology(coord)
    coord.diagnostics.create_hems_auth_degraded_issue()
    coord.diagnostics.create_rate_limited_issue()
    assert len(mock_issue_registry.created) == 2
    for _domain, _id, payload in mock_issue_registry.created:
        placeholders = payload.get("translation_placeholders", {})
        assert "backoff_until" not in placeholders
        assert "backoff_ends" not in placeholders


@pytest.mark.parametrize("client", [None, SimpleNamespace()])
async def test_entry_diagnostics_without_client_header_extensions(
    hass, config_entry, coordinator_factory, monkeypatch, client
):
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    with monkeypatch.context() as patch:
        patch.setattr(coord, "client", client)
        patch.setattr(coord.tariff_runtime, "diagnostics", None)
        patch.setattr(
            coord,
            "firmware_catalog_manager",
            SimpleNamespace(status_snapshot=lambda: []),
            raising=False,
        )
        payload = await diagnostics.async_get_config_entry_diagnostics(
            hass, config_entry
        )
    assert payload["entry_data"]["cookie"] == "**REDACTED**"
    assert payload["entry_data"]["email"] == "**REDACTED**"
    assert config_entry.data["email"] not in json.dumps(payload)


@pytest.mark.parametrize("kind", ["envoy", "encharge"])
@pytest.mark.parametrize(
    "dashboard",
    [
        None,
        [],
        {"type_summaries": {}},
        {"type_summaries": {"envoy": None, "encharge": None}},
        {"type_summaries": {"envoy": {}, "encharge": {}}},
    ],
)
async def test_type_diagnostics_without_dashboard_details(
    hass, config_entry, coordinator_factory, monkeypatch, kind, dashboard
):
    coord = coordinator_factory()
    coord._battery_status_payload = {"storages": []}
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{coord.site_id}:{kind}")},
    )
    monkeypatch.setattr(
        coord,
        "system_dashboard_diagnostics",
        None if dashboard is None else lambda: dashboard,
    )
    payload = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert "system_dashboard_details" not in payload
    assert str(coord.site_id) not in json.dumps(payload)


async def test_heatpump_type_diagnostics_without_runtime_extension(
    hass, config_entry, coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{coord.site_id}:heatpump")},
    )
    monkeypatch.setattr(coord, "heatpump_runtime_diagnostics", None)
    payload = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert "heatpump_runtime" not in payload


async def test_device_diagnostics_ignore_malformed_type_identifier(hass, config_entry):
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, "type:malformed")}
    )
    payload = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert isinstance(payload, dict)


@pytest.mark.parametrize("flows, meta", [([], []), ({" ": 1}, {})])
def test_discovery_channels_ignore_invalid_or_blank_flow_keys(
    coordinator_factory, monkeypatch, flows, meta
):
    coord = coordinator_factory()
    with monkeypatch.context() as patch:
        patch.setattr(
            coord, "energy", SimpleNamespace(site_energy=flows, site_energy_meta=meta)
        )
        assert coord.discovery_snapshot.live_site_energy_channels() == set()


def test_discovery_key_handles_unavailable_type_mapping(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    monkeypatch.setattr(coord, "_type_device_buckets", ["invalid"])
    assert isinstance(coord.discovery_snapshot._discovery_key(), tuple)


@pytest.mark.parametrize("serial_order", [None, [" ", "EV"]])
def test_discovery_restore_ignores_empty_serial_and_type_keys(
    coordinator_factory, serial_order
):
    coord = coordinator_factory()
    coord.discovery_snapshot.apply(
        {
            "serial_order": serial_order,
            "type_device_order": ["", "envoy"],
            "type_device_buckets": {
                "envoy": {"count": 1, "devices": [{"serial_number": "GW"}]}
            },
        }
    )
    assert coord.inventory_view.has_type("envoy")
    if serial_order:
        assert coord._restored_evse_serial_order == ["EV"]


@pytest.mark.parametrize("cancelled", [False, True])
async def test_discovery_save_failure_preserves_newer_pending_snapshot(
    coordinator_factory, monkeypatch, cancelled
):
    coord = coordinator_factory()
    manager = coord.discovery_snapshot

    async def save(snapshot):
        coord._serial_order = ["NEW"]
        coord.serials = {"NEW"}
        coord.data = {}
        coord._type_device_buckets["iqevse"]["devices"] = [{"serial_number": "NEW"}]
        manager.schedule_save()
        raise asyncio.CancelledError if cancelled else RuntimeError("temporary")

    monkeypatch.setattr(manager._store, "async_save", save)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await manager.async_save()
    else:
        await manager.async_save()
    assert manager._pending_snapshot["serial_order"] == ["NEW"]


async def test_completed_discovery_save_does_not_clear_newer_task(coordinator_factory):
    manager = coordinator_factory().discovery_snapshot
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    manager._clear_save_task(task)
    assert manager._save_task is None


async def test_session_history_issue_fallback_keeps_aware_day(
    coordinator_factory, monkeypatch
):
    from datetime import datetime, timezone
    from unittest.mock import Mock
    from custom_components.enphase_ev import coordinator_diagnostics as module

    coord = coordinator_factory()
    with monkeypatch.context() as context:
        context.setattr(module.dt_util, "now", lambda: datetime.now(timezone.utc))
        context.setattr(module.dt_util, "as_local", Mock(side_effect=ValueError()))
        coord.diagnostics.sync_session_history_issue()


@pytest.mark.parametrize("missing", ["begin", "end"])
def test_schedule_metrics_accept_legacy_raw_boundaries(coordinator_factory, missing):
    from custom_components.enphase_ev.state_models import BatteryControlCapability

    coord = coordinator_factory()
    coord.last_update_success = True
    coord._battery_user_is_owner = True
    coord._battery_user_is_installer = False
    for family in ("dtg", "rbd"):
        setattr(
            coord,
            f"_battery_{family}_control",
            BatteryControlCapability(show=True, locked=True),
        )
        setattr(coord, f"_battery_{family}_schedule_status", "active")
        setattr(
            coord, f"_battery_{family}_begin_time", None if missing == "begin" else -1
        )
        setattr(coord, f"_battery_{family}_end_time", None if missing == "end" else -1)
    assert coord.collect_site_metrics()
