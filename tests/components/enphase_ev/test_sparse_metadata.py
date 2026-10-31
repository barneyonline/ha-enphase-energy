"""Sparse discovery metadata must remain safe for entities and diagnostics."""

from collections import UserDict
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_components.enphase_ev import firmware_catalog
from custom_components.enphase_ev import system_dashboard_helpers as dashboard
from custom_components.enphase_ev.entity_cleanup import (
    entries_for_device,
    find_entity_id_by_unique_id,
)
from custom_components.enphase_ev.evse_power import _power_topology
from custom_components.enphase_ev.gateway_software_update import _normalize_components
from custom_components.enphase_ev.inventory_payload_helpers import hems_devices_groups
from custom_components.enphase_ev.inventory_view import InventoryView
from custom_components.enphase_ev.sensor_battery_helpers import (
    battery_last_reported_snapshot,
    battery_optional_bool,
)


def test_hems_legacy_inventory_is_used_when_result_devices_are_missing():
    members = {"heat-pump": [{"device_uid": "HP"}]}
    assert hems_devices_groups({"result": {}, "data": {"hems_devices": members}}) == [
        members
    ]


def test_dashboard_simplification_drops_empty_nested_fields():
    assert dashboard.dashboard_simple_value({"empty": None, "name": "gateway"}) == {
        "name": "gateway"
    }


def test_empty_dashboard_has_no_type_summaries():
    summaries, hierarchy, index = dashboard.build_system_dashboard_summaries(
        None, {"envoy": UserDict()}
    )
    assert summaries == {
        key: {"hierarchy": {"count": 0, "relationships": []}}
        for key in ("envoy", "encharge", "microinverter")
    }
    assert index == {}
    assert isinstance(hierarchy, dict)


def test_meter_without_known_configuration_fields_keeps_identity():
    assert dashboard.system_dashboard_meter_summaries(
        {
            "meters": {
                "meters": [{"name": "Production", "configuration": {"unknown": 1}}]
            }
        }
    ) == [{"name": "Production", "meter_type": "production"}]


def test_inventory_without_site_or_serial_metadata_has_no_devices():
    view = InventoryView(SimpleNamespace(_devices_inventory_ready=True))
    assert view.iter_type_keys() == []
    assert not view.has_type_for_entities("heatpump")


def test_primary_gateway_falls_back_when_phase_map_references_removed_serial():
    first = {"serial_number": "GW1", "name": "Gateway"}
    coord = SimpleNamespace(
        site_id="SITE",
        _type_device_buckets={"envoy": {"count": 1, "devices": [first]}},
        inventory_runtime=SimpleNamespace(
            gateway_phase_map_preferred_serial=lambda: "removed"
        ),
    )
    assert InventoryView(coord).primary_gateway_serial() == "GW1"


def test_battery_boolean_does_not_interpret_containers_as_flags():
    assert battery_optional_bool({"enabled": True}) is None


def test_battery_last_report_uses_newest_member_regardless_of_inventory_order():
    coord = SimpleNamespace(
        battery_status_payload={
            "storages": [
                {"serial_number": "new", "last_report": 1700000000},
                {"serial_number": "old", "last_report": 1600000000},
            ]
        }
    )
    snapshot = battery_last_reported_snapshot(coord)
    assert snapshot["latest_reported"] == datetime.fromtimestamp(
        1700000000, timezone.utc
    )
    assert snapshot["latest_reported_device"]["serial_number"] == "new"
    assert snapshot["total_batteries"] == 2


def test_unknown_phase_mode_uses_reported_phase_count():
    assert (
        _power_topology({"phase_mode": "future-mode", "phase_count": 3})
        == "three_phase"
    )


def test_gateway_update_ignores_components_without_public_progress_fields():
    assert _normalize_components([{"fw_image": "/private/file.bin"}]) == []


@pytest.mark.parametrize("urls", [None, {}])
def test_global_firmware_catalog_without_country_or_localized_urls(urls):
    entry = {
        "version": "8.3.6000",
        "urls_by_locale": urls,
        "countries_text": "Worldwide",
    }
    selected = firmware_catalog.select_catalog_entry(
        {"devices": {"envoy": {"latest_global": entry}}},
        device_type="envoy",
        country="AU",
        locale="en-AU",
    )
    assert selected.entry["version"] == "8.3.6000"
    assert selected.source_scope == "global"


def test_entity_lookup_can_use_legacy_registry_without_entry_getter():
    registry = SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique: "sensor.example"
    )
    assert (
        find_entity_id_by_unique_id(registry, "sensor", "unique", entry_id="entry")
        == "sensor.example"
    )


def test_device_entity_lookup_can_scan_without_registry_helper(monkeypatch):
    from custom_components.enphase_ev import entity_cleanup

    record = SimpleNamespace(entity_id="sensor.example", device_id="device")
    monkeypatch.setattr(entity_cleanup.er, "async_entries_for_device", None)
    registry = SimpleNamespace(entities={record.entity_id: record})
    assert entries_for_device(registry, "device") == [record]
