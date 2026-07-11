"""Tests for integration diagnostics."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeassistant.helpers import device_registry as dr

from custom_components.enphase_ev import diagnostics
from custom_components.enphase_ev.energy import SiteEnergyFlow
from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


def test_gateway_diagnostics_helper_branches() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert diagnostics._text(BadStr()) is None
    assert diagnostics._optional_bool(1) is True
    assert diagnostics._optional_bool("on") is True
    assert diagnostics._optional_bool("off") is False
    assert diagnostics._optional_bool("unknown") is None
    assert diagnostics._normalize_gateway_status(None) == "unknown"
    assert diagnostics._normalize_gateway_status("critical error") == "error"
    assert diagnostics._normalize_gateway_status("other") == "unknown"

    empty = diagnostics._gateway_summary([], 0)
    assert empty["connectivity"] is None

    summary = diagnostics._gateway_summary(
        [
            {"connected": True, "status": "normal", "model": "IQ Gateway"},
            {"connected": False, "statusText": "not reporting", "sw_version": "8.2.0"},
            {"connected": "maybe", "status": "warning"},
        ],
        3,
    )
    assert summary["connectivity"] == "degraded"
    assert summary["connected_devices"] == 1
    assert summary["disconnected_devices"] == 1
    assert summary["unknown_connection_devices"] == 1
    assert summary["model_counts"]["IQ Gateway"] == 1
    assert summary["firmware_counts"]["8.2.0"] == 1

    fallback_summary = diagnostics._gateway_summary(
        [
            {"connected": None, "status": "normal"},
            {"connected": None, "statusText": "not reporting"},
        ],
        2,
    )
    assert fallback_summary["connected_devices"] == 1
    assert fallback_summary["disconnected_devices"] == 1

    unknown = diagnostics._gateway_summary([{"status": "unknown"}], 2)
    assert unknown["connectivity"] == "unknown"
    assert (
        diagnostics._gateway_summary([{"connected": True}], 1)["connectivity"]
        == "online"
    )
    assert (
        diagnostics._gateway_summary(
            [{"connected": False}, {"connected": False}],
            2,
        )["connectivity"]
        == "offline"
    )
    micro_summary = diagnostics._microinverter_summary(
        {
            "count": 3,
            "status_counts": {
                "total": 3,
                "normal": 2,
                "warning": 0,
                "error": 0,
                "not_reporting": 1,
            },
            "panel_info": {"pv_module_manufacturer": "Acme"},
            "status_summary": "Normal 2 | Warning 0 | Error 0 | Not Reporting 1",
        }
    )
    assert micro_summary["connectivity"] == "degraded"
    assert micro_summary["reporting_inverters"] == 2
    assert micro_summary["panel_info"]["pv_module_manufacturer"] == "Acme"
    assert diagnostics._microinverter_summary([]) == {}
    assert (
        diagnostics._microinverter_summary({"count": object()})["total_inverters"] == 0
    )
    assert (
        diagnostics._microinverter_summary(
            {"count": 1, "status_counts": {"total": 1, "not_reporting": 0}}
        )["connectivity"]
        == "online"
    )
    assert (
        diagnostics._microinverter_summary(
            {"count": 1, "status_counts": {"total": 1, "not_reporting": 1}}
        )["connectivity"]
        == "offline"
    )
    malformed_counts = diagnostics._microinverter_summary(
        {"count": 2, "status_counts": {"total": "x", "not_reporting": object()}}
    )
    assert malformed_counts["total_inverters"] == 2
    assert malformed_counts["connectivity"] == "unknown"
    assert malformed_counts["unknown_inverters"] == 2
    overflow_counts = diagnostics._microinverter_summary(
        {"count": 1, "status_counts": {"total": 1, "not_reporting": 1, "unknown": 2}}
    )
    assert overflow_counts["unknown_inverters"] == 0
    assert overflow_counts["connectivity"] == "offline"
    assert diagnostics._microinverter_summary({"count": 2})["connectivity"] == "unknown"
    assert diagnostics._microinverter_summary({})["connectivity"] is None
    assert diagnostics._battery_status_summary_for_diagnostics(None) == {}
    assert diagnostics._ac_battery_status_summary_for_diagnostics(None) == {}


def test_diagnostics_redacts_numeric_site_ids_in_nested_payloads() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    payload = {
        "payload": {
            "id": 9990001,
            9990001: {"status": "raw"},
            42: "other",
            "ids": [9990001, 9990001.0, 42, True, BadStr()],
            "nested": {"value": "/systems/9990001/devices"},
        }
    }

    redacted = diagnostics._redact_diagnostics_payload(  # noqa: SLF001
        payload,
        site_ids=("9990001",),
    )

    assert redacted["payload"]["id"] == "[site]"
    assert redacted["payload"]["[site]"] == {"status": "raw"}
    assert 9990001 not in redacted["payload"]
    assert redacted["payload"][42] == "other"
    assert redacted["payload"]["ids"][:2] == ["[site]", "[site]"]
    assert redacted["payload"]["ids"][2] == 42
    assert redacted["payload"]["ids"][3] is True
    assert isinstance(redacted["payload"]["ids"][4], BadStr)
    assert redacted["payload"]["nested"]["value"] == "/systems/[site]/devices"
    assert diagnostics._normalize_site_ids(
        [None, "9990001", 9990001, BadStr()]
    ) == (  # noqa: SLF001
        "9990001",
    )


class DummyClient(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__()
        self._h = {"Authorization": "REDACTED", "X-Test": "value"}

    def base_header_names(self) -> list[str]:
        return sorted(self._h.keys())

    def scheduler_bearer(self):
        return "token"

    def has_scheduler_bearer(self) -> bool:
        return bool(self.scheduler_bearer())


class DummyCoordinator(SimpleNamespace):
    """Coordinator stub exposing diagnostics attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.client = DummyClient()
        self.update_interval = timedelta(seconds=45)
        self._charge_mode_cache = {RANDOM_SERIAL: ("FAST", 0)}
        self.site_id = RANDOM_SITE_ID
        self.serials = {RANDOM_SERIAL}
        self.data = {RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "status": "idle"}}
        self._network_errors = 2
        self._http_errors = 1
        self._backoff_until = 120.0
        self._last_error = "timeout"
        self.phase_timings = {"fast": 0.6}
        self._session_history_cache_ttl = 300
        self._session_history_cache = {"key": []}
        self._session_history_interval_min = 15
        self._session_refresh_in_progress = {"key"}
        self.session_history = SimpleNamespace(
            cache_ttl=300,
            cache_key_count=1,
            in_progress=1,
            service_available=False,
            service_using_stale=True,
            service_failures=2,
            service_last_error="payload invalid",
            service_last_failure_utc=datetime(2026, 3, 1, tzinfo=timezone.utc),
            _service_last_payload_signature={
                "endpoint": "/session_history",
                "failure_kind": "json_decode",
            },
        )
        self._battery_site_settings_payload = {
            "data": {"showSavingsMode": True},
            "userId": "[redacted]",
        }
        self._battery_profile_payload = {
            "data": {"profile": "cost_savings", "batteryBackupPercentage": 20},
            "token": "[redacted]",
        }
        self._battery_settings_payload = {
            "data": {"batteryGridMode": "ImportExport", "chargeFromGrid": True}
        }
        self._battery_status_payload = {
            "current_charge": "48%",
            "storages": [{"serial_number": "BT0001", "status": "normal"}],
        }
        self._ac_battery_devices_payload = {
            "battery_count": 1,
            "records": [
                {
                    "serial_number": "ACB0001",
                    "battery_id": "67890",
                    "status_text": "Warning",
                }
            ],
        }
        self._ac_battery_telemetry_payloads = {
            "records": [
                {
                    "serial_number": "ACB0001",
                    "battery_id": "67890",
                    "power_w": 260.0,
                }
            ]
        }
        self._ac_battery_events_payloads = {
            "records": [
                {
                    "serial_number": "ACB0001",
                    "battery_id": "67890",
                    "location": "/systems/9990001/ac_batteries/67890/events",
                }
            ]
        }
        self.battery_status_summary = {
            "aggregate_charge_pct": 48.0,
            "aggregate_status": "normal",
            "aggregate_charge_source": "site_current_charge",
            "included_count": 1,
            "contributing_count": 0,
            "excluded_count": 0,
            "site_current_charge_pct": 48.0,
            "site_available_power_kw": 3.2,
            "worst_status": "normal",
            "battery_order": ["BT0001"],
            "per_battery_status": {"BT0001": "normal"},
            "worst_storage_key": "BT0001",
        }
        self.ac_battery_status_summary = {
            "aggregate_status": "warning",
            "battery_count": 1,
            "sleep_state": "pending",
            "selected_sleep_min_soc": 25,
            "worst_status": "warning",
            "power_w": 260.0,
            "latest_reported_utc": "2026-03-01T00:00:00+00:00",
        }
        self.ac_battery_aggregate_status = "warning"
        self.ac_battery_sleep_state = "pending"
        self.ac_battery_selected_sleep_min_soc = 25
        self.ac_battery_control_pending = True
        self.battery_dtg_control = {
            "show": True,
            "enabled": False,
            "locked": True,
            "show_day_schedule": None,
            "schedule_supported": None,
            "force_schedule_supported": None,
            "force_schedule_opted": None,
        }
        self.battery_cfg_control = {
            "show": True,
            "enabled": True,
            "locked": False,
            "show_day_schedule": True,
            "schedule_supported": True,
            "force_schedule_supported": True,
            "force_schedule_opted": True,
        }
        self.battery_rbd_control = {
            "show": True,
            "enabled": True,
            "locked": False,
            "show_day_schedule": None,
            "schedule_supported": None,
            "force_schedule_supported": None,
            "force_schedule_opted": None,
        }
        self.battery_system_task = False
        self._grid_control_check_payload = {
            "disableGridControl": False,
            "activeDownload": False,
        }
        self._grid_mode_status_payload = {
            "meters": {
                "gridRelay": "OPER_RELAY_CLOSED",
                "serial_number": "[redacted]",
            }
        }
        self._grid_outage_context_payload = {
            "is_grid_outage": False,
            "show_grid_connect": True,
            "has_battery": True,
        }
        self._dry_contact_settings_payload = {
            "data": {
                "contacts": [
                    {"serial": "DC0001", "displayName": "Solar Diverter"},
                ]
            },
            "token": "[redacted]",
        }
        self._battery_backup_history_payload = {
            "total_records": 1,
            "histories": [{"start_time": "2025-10-17T14:38:30+11:00", "duration": 121}],
        }
        self._hems_devices_payload = {
            "data": {
                "hems-devices": {
                    "gateway": [
                        {
                            "device-uid": "5956621_IQ_ENERGY_ROUTER_1",
                            "uid": "LGX-025",
                            "hems-device-id": "router-id",
                            "hems-device-facet-id": "router-facet-id",
                            "iqer-uid": "5956621_IQ_ENERGY_ROUTER_1",
                            "ip-address": "192.0.2.99",
                            "statusText": "Normal",
                        }
                    ]
                }
            }
        }
        self._devices_inventory_payload = {"result": [{"type": "encharge"}]}
        self._system_dashboard_devices_tree_payload = {
            "devices": [
                {
                    "device_uid": "GW-1",
                    "type": "envoy",
                    "name": "Gateway",
                    "children": [{"device_uid": "BAT-1", "type": "encharge"}],
                }
            ]
        }
        self._system_dashboard_devices_details_payloads = {
            "envoy": {
                "envoy": {
                    "device_link": "https://enlighten.example/systems/9990001/envoys/200001",
                    "modem": {
                        "rssi": -72,
                        "signal_strength": "strong",
                        "plan_expiry_date": "2026-08-01",
                        "imei": "359111111111111",
                    },
                    "connection_details": {
                        "interface_ip": {
                            "ethernet": "192.0.2.10",
                        }
                    },
                    "network_configuration": [
                        {
                            "details": {
                                "mac_addr": "00:11:22:33:44:55",
                                "ip_addr": "192.0.2.10",
                                "gateway_ip_addr": "192.0.2.1",
                            }
                        }
                    ],
                    "default_route": "192.0.2.1 (Ethernet)",
                    "network": {"status": "online", "mode": "dhcp"},
                    "tunnel": {"status": "connected"},
                },
                "enpower": {"earth_type": "TN-C-S"},
                "meter": {
                    "devices": [
                        {
                            "device_uid": "MTR-1",
                            "type": "meter",
                            "name": "Consumption Meter",
                            "meter_type": "consumption",
                            "configuration": {"phase": "three_phase"},
                        }
                    ]
                },
            },
            "encharge": {
                "encharge": {
                    "connectivity": {"rssi": -61, "status": "online"},
                    "software": {"app_version": "1.2.3"},
                    "operation_mode": {"mode": "backup"},
                    "imsi": "310150123456789",
                }
            },
        }
        self._system_dashboard_hierarchy_summary = {
            "total_nodes": 2,
            "counts_by_type": {"envoy": 1, "encharge": 1},
            "relationships": [
                {
                    "device_uid": "GW-1",
                    "parent_uid": None,
                    "type_key": "envoy",
                    "name": "Gateway",
                },
                {
                    "device_uid": "BAT-1",
                    "parent_uid": "GW-1",
                    "type_key": "encharge",
                    "name": "Battery 1",
                },
            ],
        }
        self._system_dashboard_type_summaries = {
            "envoy": {
                "modem": {
                    "rssi": -72,
                    "signal": "strong",
                    "sim_plan_expiry": "2026-08-01",
                    "imei": "359111111111111",
                },
                "network": {"status": "online", "mode": "dhcp"},
                "tunnel": {"status": "connected"},
                "controller": {"earth_type": "TN-C-S"},
                "meters": [
                    {
                        "name": "Consumption Meter",
                        "meter_type": "consumption",
                        "config": {"phase": "three_phase"},
                    }
                ],
                "hierarchy": {
                    "count": 1,
                    "relationships": [{"device_uid": "GW-1", "parent_uid": None}],
                },
            },
            "encharge": {
                "connectivity": {"rssi": -61, "status": "online"},
                "software": {"app_version": "1.2.3"},
                "operation_mode": {"mode": "backup"},
                "hierarchy": {
                    "count": 1,
                    "relationships": [{"device_uid": "BAT-1", "parent_uid": "GW-1"}],
                },
            },
            "microinverter": {
                "total_inverters": 16,
                "not_reporting_inverters": 1,
                "plc_comm_inverters": 5,
                "model_summary": "IQ7A Microinverters x16",
            },
        }
        self._evse_site_feature_flags = {
            "evse_charging_mode": True,
            "evse_storm_guard": False,
        }
        self._evse_feature_flags_by_serial = {
            RANDOM_SERIAL: {
                "evse_authentication": True,
                "iqevse_rfid": True,
                "max_current_config_support": True,
            }
        }
        self._evse_feature_flags_payload = {
            "meta": {"serverTimeStamp": "2026-03-08T09:40:02.917+00:00"},
            "data": {
                "evse_charging_mode": True,
                RANDOM_SERIAL: {
                    "evse_authentication": True,
                    "iqevse_rfid": True,
                    "max_current_config_support": True,
                },
            },
            "error": {},
        }
        self.include_inverters = True
        self._inverter_summary_counts = {
            "total": 2,
            "normal": 2,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
        }
        self._inverter_model_counts = {"IQ7A": 2}
        self._inverters_inventory_payload = {"total": 2}
        self._inverter_status_payload = {"key": {"serialNum": "INV-A"}}
        self._inverter_production_payload = {"production": {"key": 100}}
        self._inverter_production_cache_key = ("2022-08-10", "2026-03-08")
        self._inverter_production_cache_until = time.monotonic() + 300
        self.firmware_catalog_manager = SimpleNamespace(
            status_snapshot=lambda: {
                "last_fetch_utc": "2026-03-01T00:00:00+00:00",
                "last_success_utc": "2026-03-01T00:00:00+00:00",
                "last_error": None,
                "using_stale": False,
                "catalog_generated_at": "2026-03-01T00:00:00Z",
                "catalog_source_age_seconds": 42.0,
            }
        )
        self._heatpump_runtime_state = {
            "device_uid": "HP-1",
            "heatpump_status": "RUNNING",
            "sg_ready_mode_raw": "MODE_3",
            "sg_ready_mode_label": "Recommended",
            "sg_ready_active": True,
            "last_report_at": "2026-03-01T00:00:00+00:00",
            "source": "hems_heatpump_state:HP-1",
        }
        self._heatpump_runtime_state_using_stale = False
        self._heatpump_runtime_state_last_success_utc = datetime(
            2026, 3, 1, 0, 0, tzinfo=timezone.utc
        )
        self._heatpump_runtime_state_last_error = None
        self._heatpump_daily_consumption = {
            "device_uid": None,
            "split_device_uid": "HP-1",
            "daily_energy_wh": 230.0,
            "split_daily_energy_wh": 230.0,
            "source": "site_today_heatpump",
            "split_source": "hems_energy_consumption:HP-1",
        }
        self._heatpump_daily_consumption_using_stale = False
        self._heatpump_daily_consumption_last_success_utc = datetime(
            2026, 3, 1, 0, 0, tzinfo=timezone.utc
        )
        self._heatpump_daily_consumption_last_error = None
        self._show_livestream_payload = {"live_status": True, "live_vitals": True}
        self._heatpump_events_payloads = [
            {
                "device_uid": "HP-1",
                "device_type": "HEAT_PUMP",
                "name": "Waermepumpe",
                "payload": [{"statusText": "Recommended"}],
            }
        ]
        self._heatpump_power_snapshot = {
            "site_date": "2026-03-01",
            "force": True,
            "previous_device_ref": "H...1",
            "candidates": [
                {
                    "requested_device_ref": "H...1",
                    "member_device_ref": "H...1",
                    "member_device_type": "HEAT_PUMP",
                    "status": "Normal",
                    "recommended": False,
                }
            ],
            "attempts": [
                {
                    "requested_device_ref": "H...1",
                    "resolved_device_ref": "H...1",
                    "detail_count": 1,
                    "reported_detail_value": 550.0,
                    "accepted_value_w": 550.0,
                    "runtime_mode": "RUNNING",
                    "validation": "accepted",
                    "rejected": False,
                }
            ],
            "selected_payload": {
                "resolved_device_ref": "H...1",
                "reported_detail_value": 550.0,
                "accepted_value_w": 550.0,
                "runtime_mode": "RUNNING",
                "validation": "accepted",
                "rejected": False,
            },
            "selected_source": "hems_energy_consumption:H...1",
            "selected_sample_at_utc": "2026-03-01T00:05:00+00:00",
            "last_error": None,
            "outcome": "selected_sample",
            "using_stale": False,
            "last_success_utc": "2026-03-01T00:00:00+00:00",
        }
        self._heatpump_power_using_stale = False
        self._heatpump_power_last_success_utc = datetime(
            2026, 3, 1, 0, 0, tzinfo=timezone.utc
        )
        self._heatpump_runtime_diagnostics_error = None
        self.tariff_runtime = SimpleNamespace(
            diagnostics=lambda: {
                "billing_available": True,
                "import_rate_available": True,
                "export_rate_available": False,
                "endpoint_family": {"support_state": "supported"},
            }
        )

    def collect_site_metrics(self):
        return {
            "site_id": self.site_id,
            "site_name": "Garage Site",
            "network_errors": self._network_errors,
            "http_errors": self._http_errors,
            "last_error": self._last_error,
            "last_failure_endpoint": f"/service/evse_controller/{self.site_id}/ev_chargers/status",
            "payload_using_stale": True,
            "payload_failure_kind": "json_decode",
            "payload_health": self.payload_health_diagnostics(),
            "phase_timings": self.phase_timings,
            "session_cache_ttl_s": self._session_history_cache_ttl,
            "dry_contact_settings_supported": True,
            "dry_contact_settings_contact_count": 1,
            "dry_contact_settings_unmatched_count": 0,
            "dry_contact_settings_fetch_failures": 0,
            "dry_contact_settings_data_stale": False,
            "battery_dtg_control": self.battery_dtg_control,
            "battery_cfg_control": self.battery_cfg_control,
            "battery_rbd_control": self.battery_rbd_control,
            "battery_system_task": self.battery_system_task,
        }

    def charge_mode_cache_snapshot(self):
        cache = getattr(self, "_charge_mode_cache", {}) or {}
        return {str(serial): str(value[0]) for serial, value in cache.items() if value}

    def session_history_diagnostics(self):
        return {
            "available": False,
            "using_stale": True,
            "failures": 2,
            "last_error": "payload invalid",
            "last_failure_utc": "2026-03-01T00:00:00+00:00",
            "last_payload_signature": {
                "endpoint": "/session_history",
                "failure_kind": "json_decode",
            },
            "cache_ttl_seconds": self._session_history_cache_ttl,
            "cache_keys": len(self._session_history_cache),
            "interval_minutes": self._session_history_interval_min,
            "in_progress": len(self._session_refresh_in_progress),
        }

    def payload_health_diagnostics(self):
        return {
            "status": {
                "available": False,
                "using_stale": True,
                "failures": 1,
                "last_error": "Invalid JSON response",
                "last_success_age_s": 12.5,
                "last_payload_signature": {
                    "endpoint": f"/service/evse_controller/{self.site_id}/ev_chargers/status",
                    "failure_kind": "json_decode",
                    "body_preview_redacted": '{"site":"[site]","serial":"SERI...5678"}',
                },
            }
        }

    def battery_diagnostics_payloads(self):
        return {
            "site_settings_payload": self._battery_site_settings_payload,
            "profile_payload": self._battery_profile_payload,
            "settings_payload": self._battery_settings_payload,
            "status_payload": self._battery_status_payload,
            "grid_control_check_payload": self._grid_control_check_payload,
            "grid_mode_status_payload": self._grid_mode_status_payload,
            "grid_outage_context_payload": self._grid_outage_context_payload,
            "dry_contacts_payload": self._dry_contact_settings_payload,
            "backup_history_payload": self._battery_backup_history_payload,
            "hems_devices_payload": self._hems_devices_payload,
            "devices_inventory_payload": self._devices_inventory_payload,
            "ac_battery_devices_payload": self._ac_battery_devices_payload,
            "ac_battery_telemetry_payloads": self._ac_battery_telemetry_payloads,
            "ac_battery_events_payloads": self._ac_battery_events_payloads,
        }

    async def async_ensure_heatpump_runtime_diagnostics(self):
        return None

    async def async_ensure_battery_status_diagnostics(self):
        return None

    async def async_ensure_ac_battery_diagnostics(self):
        return None

    def heatpump_runtime_diagnostics(self):
        return {
            "runtime_state": self._heatpump_runtime_state,
            "runtime_state_last_error": self._heatpump_runtime_state_last_error,
            "daily_consumption": self._heatpump_daily_consumption,
            "daily_consumption_last_error": self._heatpump_daily_consumption_last_error,
            "show_livestream_payload": self._show_livestream_payload,
            "events_payloads": self._heatpump_events_payloads,
            "power_snapshot": self._heatpump_power_snapshot,
            "last_error": self._heatpump_runtime_diagnostics_error,
        }

    def inverter_diagnostics_payloads(self):
        return {
            "enabled": self.include_inverters,
            "summary_counts": self._inverter_summary_counts,
            "model_counts": self._inverter_model_counts,
            "inventory_payload": self._inverters_inventory_payload,
            "status_payload": self._inverter_status_payload,
            "production_payload": self._inverter_production_payload,
            "production_cache_key": self._inverter_production_cache_key,
            "production_cache_remaining_seconds": 300.0,
            "production_cache_age_seconds": 120.0,
        }

    def evse_diagnostics_payloads(self):
        return {
            "feature_flags_meta": self._evse_feature_flags_payload["meta"],
            "feature_flags_error": self._evse_feature_flags_payload["error"],
            "site_feature_flags": self._evse_site_feature_flags,
            "charger_feature_flags": [
                {
                    "serial": RANDOM_SERIAL,
                    "flags": self._evse_feature_flags_by_serial[RANDOM_SERIAL],
                }
            ],
            "charger_support_sources": [
                {
                    "serial": RANDOM_SERIAL,
                    "sources": {"auth_feature_supported": "runtime"},
                }
            ],
        }

    def scheduler_diagnostics(self):
        backoff = self._scheduler_backoff_ends_utc
        if isinstance(backoff, datetime):
            try:
                backoff = backoff.isoformat()
            except Exception:
                backoff = None
        return {"backoff_ends_utc": backoff}

    def system_dashboard_diagnostics(self):
        return {
            "devices_tree_payload": self._system_dashboard_devices_tree_payload,
            "devices_details_payloads": self._system_dashboard_devices_details_payloads,
            "hierarchy_summary": self._system_dashboard_hierarchy_summary,
            "type_summaries": self._system_dashboard_type_summaries,
        }


@pytest.mark.asyncio
async def test_config_entry_diagnostics_includes_coordinator(
    hass, config_entry
) -> None:
    """Validate coordinator diagnostics payload and redaction logic."""
    coord = DummyCoordinator()
    coord.schedule_sync = SimpleNamespace(diagnostics=lambda: {"enabled": True})
    coord.system_events_runtime = SimpleNamespace(
        diagnostics=lambda: {
            "active_count": 1,
            "high_impact_count": 1,
            "device_type_counts": {"IQ Gateway": 1},
        }
    )
    coord._scheduler_backoff_ends_utc = datetime(2025, 1, 1, tzinfo=timezone.utc)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["entry_data"]["cookie"] == "**REDACTED**"
    assert diag["entry_data"]["email"] == "**REDACTED**"
    assert diag["coordinator"]["site_id"] == "**REDACTED**"
    assert diag["coordinator"]["site_metrics"]["site_name"] == "**REDACTED**"
    assert diag["coordinator"]["headers_info"]["base_header_names"] == [
        "Authorization",
        "X-Test",
    ]
    assert diag["coordinator"]["headers_info"]["has_scheduler_bearer"] is True
    assert diag["coordinator"]["last_scheduler_modes"] == {RANDOM_SERIAL: "FAST"}
    assert diag["coordinator"]["session_history"]["cache_keys"] == 1
    assert diag["coordinator"]["site_metrics"]["payload_using_stale"] is True
    assert diag["coordinator"]["site_metrics"]["payload_failure_kind"] == "json_decode"
    assert diag["coordinator"]["session_history"]["using_stale"] is True
    assert (
        diag["coordinator"]["session_history"]["last_payload_signature"]["endpoint"]
        == "/session_history"
    )
    assert diag["coordinator"]["payload_health"]["status"]["using_stale"] is True
    assert (
        diag["coordinator"]["payload_health"]["status"]["last_payload_signature"][
            "endpoint"
        ]
        == "/service/evse_controller/[site]/ev_chargers/status"
    )
    assert RANDOM_SITE_ID not in json.dumps(diag, default=str)
    assert (
        diag["coordinator"]["payload_health"]["status"]["last_payload_signature"][
            "body_preview_redacted"
        ]
        == '{"site":"[site]","serial":"SERI...5678"}'
    )
    assert diag["coordinator"]["inverters"]["production_cache_key"] == (
        "2022-08-10",
        "2026-03-08",
    )
    assert (
        diag["coordinator"]["inverters"]["production_cache_remaining_seconds"]
        is not None
    )
    assert diag["coordinator"]["inverters"]["production_cache_age_seconds"] is not None
    assert (
        diag["coordinator"]["battery_config"]["site_settings_payload"]["userId"]
        == "[redacted]"
    )
    assert (
        diag["coordinator"]["battery_config"]["profile_payload"]["token"]
        == "[redacted]"
    )
    assert (
        diag["coordinator"]["battery_config"]["settings_payload"]["data"][
            "batteryGridMode"
        ]
        == "ImportExport"
    )
    assert (
        diag["coordinator"]["battery_config"]["status_payload"]["current_charge"]
        == "48%"
    )
    assert (
        diag["coordinator"]["battery_config"]["grid_control_check_payload"][
            "disableGridControl"
        ]
        is False
    )
    assert (
        diag["coordinator"]["battery_config"]["grid_outage_context_payload"][
            "is_grid_outage"
        ]
        is False
    )
    assert (
        diag["coordinator"]["battery_config"]["dry_contacts_payload"]["token"]
        == "[redacted]"
    )
    assert (
        diag["coordinator"]["battery_config"]["backup_history_payload"]["total_records"]
        == 1
    )
    assert diag["coordinator"]["site_metrics"]["battery_cfg_control"]["locked"] is False
    assert diag["coordinator"]["site_metrics"]["battery_dtg_control"]["locked"] is True
    assert diag["coordinator"]["site_metrics"]["battery_system_task"] is False
    assert (
        diag["coordinator"]["battery_config"]["hems_devices_payload"]["data"][
            "hems-devices"
        ]["gateway"][0]["device-uid"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["battery_config"]["hems_devices_payload"]["data"][
            "hems-devices"
        ]["gateway"][0]["uid"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["battery_config"]["hems_devices_payload"]["data"][
            "hems-devices"
        ]["gateway"][0]["hems-device-id"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["battery_config"]["hems_devices_payload"]["data"][
            "hems-devices"
        ]["gateway"][0]["ip-address"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["heatpump_runtime"]["show_livestream_payload"][
            "live_vitals"
        ]
        is True
    )
    assert (
        diag["coordinator"]["heatpump_runtime"]["events_payloads"][0]["device_uid"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["heatpump_runtime"]["events_payloads"][0]["payload"][0][
            "statusText"
        ]
        == "Recommended"
    )
    assert (
        diag["coordinator"]["heatpump_runtime"]["power_snapshot"]["candidates"][0][
            "requested_device_ref"
        ]
        == "H...1"
    )
    assert (
        diag["coordinator"]["heatpump_runtime"]["power_snapshot"]["selected_payload"][
            "accepted_value_w"
        ]
        == 550.0
    )
    assert (
        diag["coordinator"]["heatpump_runtime"]["power_snapshot"]["selected_source"]
        == "hems_energy_consumption:H...1"
    )
    assert diag["coordinator"]["battery_config"]["devices_inventory_payload"] == {
        "result": [{"type": "encharge"}]
    }
    assert diag["coordinator"]["site_metrics"]["dry_contact_settings_supported"] is True
    assert (
        diag["coordinator"]["site_metrics"]["dry_contact_settings_contact_count"] == 1
    )
    assert (
        diag["coordinator"]["evse"]["site_feature_flags"]["evse_charging_mode"] is True
    )
    assert (
        diag["coordinator"]["evse"]["charger_feature_flags"][0]["serial"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["evse"]["charger_feature_flags"][0]["flags"][
            "max_current_config_support"
        ]
        is True
    )
    assert (
        diag["coordinator"]["evse"]["charger_support_sources"][0]["serial"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["evse"]["charger_support_sources"][0]["sources"][
            "auth_feature_supported"
        ]
        == "runtime"
    )
    assert diag["coordinator"]["inverters"]["enabled"] is True
    assert diag["coordinator"]["inverters"]["summary_counts"]["total"] == 2
    assert diag["coordinator"]["inverters"]["model_counts"]["IQ7A"] == 2
    assert diag["coordinator"]["firmware_catalog"]["catalog_generated_at"] == (
        "2026-03-01T00:00:00Z"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["devices_tree_payload"]["devices"][0][
            "device_uid"
        ]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
            "envoy"
        ]["modem"]["imei"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
            "envoy"
        ]["device_link"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
            "envoy"
        ]["connection_details"]["interface_ip"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
            "envoy"
        ]["network_configuration"][0]["details"]["mac_addr"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
            "envoy"
        ]["network_configuration"][0]["details"]["ip_addr"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
            "envoy"
        ]["network_configuration"][0]["details"]["gateway_ip_addr"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["devices_details_payloads"]["envoy"][
            "envoy"
        ]["default_route"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["hierarchy_summary"]["relationships"][
            1
        ]["parent_uid"]
        == "**REDACTED**"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["type_summaries"]["envoy"]["modem"][
            "sim_plan_expiry"
        ]
        == "2026-08-01"
    )
    assert (
        diag["coordinator"]["system_dashboard"]["type_summaries"]["microinverter"][
            "plc_comm_inverters"
        ]
        == 5
    )
    assert diag["coordinator"]["schedule_sync"] == {"enabled": True}
    assert (
        diag["coordinator"]["scheduler"]["backoff_ends_utc"]
        == "2025-01-01T00:00:00+00:00"
    )
    assert diag["coordinator"]["tariff"] == {
        "billing_available": True,
        "import_rate_available": True,
        "export_rate_available": False,
        "endpoint_family": {"support_state": "supported"},
    }
    assert diag["coordinator"]["system_events"] == {
        "active_count": 1,
        "high_impact_count": 1,
        "device_type_counts": {"IQ Gateway": 1},
    }


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_system_events_capture_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.system_events_runtime = SimpleNamespace(
        diagnostics=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["system_events"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_schedule_sync_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()

    class BadScheduleSync:
        def diagnostics(self):
            raise RuntimeError("boom")

    coord.schedule_sync = BadScheduleSync()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["schedule_sync"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_evse_capture_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.evse_diagnostics_payloads = lambda: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["evse"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_firmware_catalog_snapshot_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.firmware_catalog_manager = SimpleNamespace(
        status_snapshot=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["firmware_catalog"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_tariff_diagnostics_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.tariff_runtime = SimpleNamespace(
        diagnostics=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["tariff"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_scheduler_backoff_format_error(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()

    class BadDatetime(datetime):
        def isoformat(self):  # type: ignore[override]
            raise ValueError("boom")

    coord._scheduler_backoff_ends_utc = BadDatetime(2025, 1, 1, tzinfo=timezone.utc)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["scheduler"]["backoff_ends_utc"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_without_coordinator(hass, config_entry) -> None:
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert "coordinator" not in diag


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_faulty_coordinator(
    hass, config_entry
) -> None:
    class FaultyClient:
        def base_header_names(self):
            raise RuntimeError("no headers")

        def has_scheduler_bearer(self):
            raise RuntimeError("no bearer")

    class FaultyCoordinator(DummyCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.update_interval = object()
            self._charge_mode_cache = None
            self.client = FaultyClient()

        def collect_site_metrics(self):
            raise RuntimeError("boom")

    coord = FaultyCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    coordinator = diag["coordinator"]
    assert coordinator["site_metrics"] is None
    assert coordinator["headers_info"]["base_header_names"] == []
    assert coordinator["headers_info"]["has_scheduler_bearer"] is False
    assert coordinator["last_scheduler_modes"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_snapshot_helper_errors(
    hass, config_entry
) -> None:
    class PartialFailureCoordinator(DummyCoordinator):
        def charge_mode_cache_snapshot(self):
            raise RuntimeError("modes")

        def session_history_diagnostics(self):
            raise RuntimeError("session")

        def battery_diagnostics_payloads(self):
            raise RuntimeError("battery")

        def inverter_diagnostics_payloads(self):
            raise RuntimeError("inverters")

    coord = PartialFailureCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    coordinator = diag["coordinator"]
    assert coordinator["last_scheduler_modes"] == {}
    assert coordinator["session_history"] == {}
    assert coordinator["battery_config"] == {}
    assert coordinator["inverters"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_payload_health_capture_error(
    hass, config_entry
) -> None:
    class PartialFailureCoordinator(DummyCoordinator):
        def payload_health_diagnostics(self):
            raise RuntimeError("payload")

    coord = PartialFailureCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["payload_health"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_system_dashboard_capture_error(
    hass, config_entry
) -> None:
    class PartialFailureCoordinator(DummyCoordinator):
        def system_dashboard_diagnostics(self):
            raise RuntimeError("dashboard")

    coord = PartialFailureCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["coordinator"]["system_dashboard"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_captures_grid_profile_runtime(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.grid_profile_runtime = SimpleNamespace(
        diagnostics=lambda: {"support_state": "installer_access_confirmed"}
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["grid_profile"] == {
        "support_state": "installer_access_confirmed"
    }

    coord.grid_profile_runtime.diagnostics = lambda: (_ for _ in ()).throw(
        RuntimeError("grid profile")
    )
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["grid_profile"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_uses_cached_system_dashboard_only(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_system_dashboard_diagnostics = AsyncMock()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    coord.async_ensure_system_dashboard_diagnostics.assert_not_awaited()
    assert "system_dashboard" in diag["coordinator"]


@pytest.mark.asyncio
async def test_config_entry_diagnostics_does_not_prefetch_dashboard(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_system_dashboard_diagnostics = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    coord.async_ensure_system_dashboard_diagnostics.assert_not_awaited()
    assert "system_dashboard" in diag["coordinator"]


@pytest.mark.asyncio
async def test_config_entry_diagnostics_includes_site_energy(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.energy = SimpleNamespace(
        site_energy={
            "grid_import": SiteEnergyFlow(
                value_kwh=1.0,
                bucket_count=2,
                fields_used=["import"],
                start_date="2024-01-01",
                last_report_date=datetime(2024, 1, 2, tzinfo=timezone.utc),
                update_pending=True,
                source_unit="Wh",
                last_reset_at=None,
                interval_minutes=60,
            ),
            "legacy_flow": SimpleNamespace(
                value_kwh=2.0,
                bucket_count=1,
                fields_used=["legacy"],
                start_date="2024-01-02",
                last_report_date=datetime(2024, 1, 4, tzinfo=timezone.utc),
                update_pending=False,
                source_unit="Wh",
                last_reset_at="2024-01-05T00:00:00+00:00",
                interval_minutes=30,
            ),
        },
        site_energy_meta={
            "start_date": "2024-01-01",
            "last_report_date": datetime(2024, 1, 3, tzinfo=timezone.utc),
        },
        site_energy_cache_age=1.23,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    site_energy = diag["site_energy"]
    assert "grid_import" in site_energy["flows"]
    assert site_energy["flows"]["grid_import"]["interval_minutes"] == 60
    assert site_energy["flows"]["legacy_flow"]["interval_minutes"] == 30
    assert site_energy["meta"]["last_report_date"].startswith("2024-01-03")


@pytest.mark.asyncio
async def test_config_entry_diagnostics_handles_unexpected_site_energy(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.energy = SimpleNamespace(
        site_energy={"bad": None, "other": "string"},
        site_energy_meta={
            "last_report_date": datetime(2024, 1, 1, tzinfo=timezone.utc)
        },
        site_energy_cache_age=1.23,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["site_energy"]["flows"] in (None, {})

    class BoomSiteEnergy:
        def items(self):
            raise RuntimeError("boom")

    coord.energy.site_energy = BoomSiteEnergy()
    coord.energy.site_energy_meta = {
        "last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc)
    }
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["site_energy"]["flows"] is None


@pytest.mark.asyncio
async def test_config_entry_diagnostics_cache_age_failure(hass, config_entry) -> None:
    coord = DummyCoordinator()
    coord.energy = SimpleNamespace(
        site_energy={"grid_import": {"value_kwh": 1.0}},
        site_energy_meta={
            "last_report_date": datetime(2024, 1, 2, tzinfo=timezone.utc)
        },
        site_energy_cache_age=None,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["site_energy"]["cache_age_s"] is None


@pytest.mark.asyncio
async def test_device_diagnostics_returns_snapshot(hass, config_entry) -> None:
    """Device diagnostics should resolve a serial and return cached data."""
    coord = DummyCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Garage Charger",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert result["serial"] == "**REDACTED**"
    assert result["snapshot"] == {"sn": "**REDACTED**", "status": "idle"}


@pytest.mark.asyncio
async def test_device_diagnostics_returns_site_snapshot(hass, config_entry) -> None:
    """Site devices should return redacted site diagnostics instead of a serial error."""
    coord = DummyCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert result["device_kind"] == "site"
    assert result["site_id"] == "**REDACTED**"
    assert result["metrics_available"] is True
    assert result["site_metrics"]["site_id"] == "**REDACTED**"
    assert result["site_metrics"]["last_failure_endpoint"] == (
        "/service/evse_controller/[site]/ev_chargers/status"
    )
    assert result["serials_count"] == 1


@pytest.mark.asyncio
async def test_device_diagnostics_returns_site_snapshot_without_runtime_data(
    hass, config_entry, monkeypatch
) -> None:
    """Site device diagnostics should still be useful before runtime data is attached."""
    monkeypatch.setattr(
        diagnostics,
        "get_runtime_data",
        lambda _entry: (_ for _ in ()).throw(RuntimeError("missing runtime")),
    )
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert result["device_kind"] == "site"
    assert result["site_id"] == "**REDACTED**"
    assert result["metrics_available"] is False
    assert result["site_metrics"] is None
    assert result["serials_count"] is None


@pytest.mark.asyncio
async def test_device_diagnostics_returns_site_snapshot_when_metrics_fail(
    hass, config_entry, monkeypatch
) -> None:
    """Site device diagnostics should not fail when metrics capture fails."""

    class RaisingCoordinator(DummyCoordinator):
        def collect_site_metrics(self):
            raise RuntimeError("boom")

    coord = RaisingCoordinator()
    monkeypatch.setattr(
        diagnostics,
        "get_runtime_data",
        lambda _entry: SimpleNamespace(coordinator=coord),
    )
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert result["device_kind"] == "site"
    assert result["metrics_available"] is False
    assert result["site_metrics"] is None
    assert result["serials_count"] == 1


@pytest.mark.asyncio
async def test_device_diagnostics_handles_missing_serial(hass, config_entry) -> None:
    """If a non-site device has no serial identifier, report the error."""
    coord = DummyCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("other_domain", "site-without-enphase-identifier")},
        manufacturer="Enphase",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert result == {"error": "serial_not_resolved"}


@pytest.mark.asyncio
async def test_device_diagnostics_device_not_found(hass, config_entry) -> None:
    device = SimpleNamespace(id="missing-device")
    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert result == {"error": "device_not_found"}


@pytest.mark.asyncio
async def test_device_diagnostics_missing_coordinator(hass, config_entry) -> None:
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Garage Charger",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert result == {"serial": "**REDACTED**", "snapshot": {}}


@pytest.mark.asyncio
async def test_device_diagnostics_redacts_session_auth_fields(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.data[RANDOM_SERIAL]["energy_today_sessions"] = [
        {
            "session_id": "session-123",
            "auth_identifier": "rfid-456",
            "auth_token": "secret-token",
        }
    ]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Garage Charger",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    sessions = result["snapshot"]["energy_today_sessions"]

    assert sessions == [
        {
            "session_id": "**REDACTED**",
            "auth_identifier": "**REDACTED**",
            "auth_token": "**REDACTED**",
        }
    ]


@pytest.mark.asyncio
async def test_device_diagnostics_type_device_payload(hass, config_entry) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: (
        {  # type: ignore[attr-defined]
            "type_label": "Battery",
            "count": 2,
            "devices": [{"serial_number": "BAT-1"}, {"serial_number": "BAT-2"}],
            "status_counts": {"normal": 2},
            "property_keys": ["name", "serial_number"],
            "summary_label": "ok",
        }
        if type_key == "encharge"
        else None
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:encharge")},
        manufacturer="Enphase",
        name="Battery (2)",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert result["site_id"] == "**REDACTED**"
    assert result["type_key"] == "encharge"
    assert result["type_label"] == "Battery"
    assert result["count"] == 2
    assert len(result["devices"]) == 2
    assert result["status_counts"]["normal"] == 2
    assert result["property_keys"] == ["name", "serial_number"]
    assert result["summary_label"] == "ok"


@pytest.mark.asyncio
async def test_device_diagnostics_heatpump_includes_runtime_payloads(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: (
        {  # type: ignore[attr-defined]
            "type_label": "Heat Pump",
            "count": 1,
            "devices": [{"device_uid": "HP-1", "statusText": "Normal"}],
        }
        if type_key == "heatpump"
        else None
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:heatpump")},
        manufacturer="Enphase",
        name="Heat Pump",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)

    assert result["heatpump_runtime"]["show_livestream_payload"]["live_status"] is True
    assert (
        result["heatpump_runtime"]["events_payloads"][0]["device_uid"] == "**REDACTED**"
    )
    assert (
        result["heatpump_runtime"]["events_payloads"][0]["payload"][0]["statusText"]
        == "Recommended"
    )
    assert (
        result["heatpump_runtime"]["power_snapshot"]["selected_payload"][
            "resolved_device_ref"
        ]
        == "H...1"
    )
    assert (
        result["heatpump_runtime"]["power_snapshot"]["selected_source"]
        == "hems_energy_consumption:H...1"
    )


@pytest.mark.asyncio
async def test_config_diagnostics_include_redacted_ac_battery_payloads(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    battery_config = diag["coordinator"]["battery_config"]
    assert (
        battery_config["ac_battery_devices_payload"]["records"][0]["serial_number"]
        == "**REDACTED**"
    )
    assert (
        battery_config["ac_battery_devices_payload"]["records"][0]["battery_id"]
        == "**REDACTED**"
    )
    assert (
        battery_config["ac_battery_events_payloads"]["records"][0]["location"]
        == "**REDACTED**"
    )


@pytest.mark.asyncio
async def test_device_diagnostics_ac_battery_type_includes_payloads(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: (  # type: ignore[attr-defined]
        {
            "type_label": "AC Battery",
            "count": 1,
            "devices": [{"serial_number": "ACB0001"}],
        }
        if type_key == "ac_battery"
        else None
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:ac_battery")},
        manufacturer="Enphase",
        name="AC Battery",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)

    assert result["type_key"] == "ac_battery"
    assert result["ac_battery_status_summary"]["aggregate_status"] == "warning"
    assert (
        result["ac_battery_devices_payload"]["records"][0]["serial_number"]
        == "**REDACTED**"
    )


@pytest.mark.asyncio
async def test_config_entry_diagnostics_heatpump_runtime_errors_fallback_to_empty(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_heatpump_runtime_diagnostics = AsyncMock(
        side_effect=RuntimeError("capture failed")
    )
    coord.heatpump_runtime_diagnostics = lambda: (_ for _ in ()).throw(
        RuntimeError("runtime failed")
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["heatpump_runtime"] == {}


@pytest.mark.asyncio
async def test_config_entry_diagnostics_ac_battery_capture_errors_are_swallowed(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_ac_battery_diagnostics = AsyncMock(
        side_effect=RuntimeError("capture failed")
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    diag = await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["coordinator"]["battery_config"]["ac_battery_devices_payload"]


@pytest.mark.asyncio
async def test_config_entry_diagnostics_skips_optional_type_prefetch(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord._selected_type_keys = {"envoy", "encharge"}  # noqa: SLF001
    coord.inventory_view = SimpleNamespace(
        has_type_for_entities=lambda type_key: False,
        iter_type_keys=lambda: ["envoy", "encharge"],
        type_bucket=lambda type_key: None,
    )
    coord._ac_battery_devices_payload = None  # noqa: SLF001
    coord._ac_battery_telemetry_payloads = None  # noqa: SLF001
    coord._ac_battery_events_payloads = None  # noqa: SLF001
    coord._heatpump_runtime_state = None  # noqa: SLF001
    coord._heatpump_daily_consumption = None  # noqa: SLF001
    coord._show_livestream_payload = None  # noqa: SLF001
    coord._heatpump_events_payloads = []  # noqa: SLF001
    coord._heatpump_power_snapshot = None  # noqa: SLF001
    coord.async_ensure_ac_battery_diagnostics = AsyncMock()
    coord.async_ensure_heatpump_runtime_diagnostics = AsyncMock()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await diagnostics.async_get_config_entry_diagnostics(hass, config_entry)

    coord.async_ensure_ac_battery_diagnostics.assert_not_awaited()
    coord.async_ensure_heatpump_runtime_diagnostics.assert_not_awaited()


@pytest.mark.asyncio
async def test_device_diagnostics_heatpump_runtime_errors_are_swallowed(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: (
        {  # type: ignore[attr-defined]
            "type_label": "Heat Pump",
            "count": 1,
            "devices": [{"device_uid": "HP-1", "statusText": "Normal"}],
        }
        if type_key == "heatpump"
        else None
    )
    coord.async_ensure_heatpump_runtime_diagnostics = AsyncMock(
        side_effect=RuntimeError("capture failed")
    )
    coord.heatpump_runtime_diagnostics = lambda: (_ for _ in ()).throw(
        RuntimeError("runtime failed")
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:heatpump")},
        manufacturer="Enphase",
        name="Heat Pump",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)

    coord.async_ensure_heatpump_runtime_diagnostics.assert_not_awaited()
    assert "heatpump_runtime" not in result


@pytest.mark.asyncio
async def test_device_diagnostics_ac_battery_capture_errors_are_swallowed(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: (  # type: ignore[attr-defined]
        {
            "type_label": "AC Battery",
            "count": 1,
            "devices": [{"serial_number": "ACB0001"}],
        }
        if type_key == "ac_battery"
        else None
    )
    coord.async_ensure_ac_battery_diagnostics = AsyncMock(
        side_effect=RuntimeError("capture failed")
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:ac_battery")},
        manufacturer="Enphase",
        name="AC Battery",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)

    coord.async_ensure_ac_battery_diagnostics.assert_not_awaited()
    assert result["type_key"] == "ac_battery"
    assert result["ac_battery_status_summary"]["aggregate_status"] == "warning"


@pytest.mark.asyncio
async def test_device_diagnostics_envoy_includes_gateway_summary(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: (
        {  # type: ignore[attr-defined]
            "type_label": "Gateway",
            "count": 3,
            "devices": [
                {
                    "serial_number": "GW-1",
                    "name": "IQ Gateway",
                    "connected": True,
                    "status": "normal",
                    "model": "IQ Gateway",
                    "envoy_sw_version": "8.2.0",
                },
                {
                    "serial_number": "GW-2",
                    "name": "System Controller",
                    "connected": False,
                    "statusText": "Not Reporting",
                    "channel_type": "enpower",
                    "sw_version": "8.2.0",
                },
                {
                    "serial_number": "GW-3",
                    "name": "Meter",
                    "connected": None,
                    "status": "warning",
                },
            ],
        }
        if type_key == "envoy"
        else None
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    summary = result["gateway_summary"]
    assert result["type_key"] == "envoy"
    assert summary["connectivity"] == "degraded"
    assert summary["connected_devices"] == 1
    assert summary["disconnected_devices"] == 1
    assert summary["unknown_connection_devices"] == 1
    assert summary["status_counts"]["warning"] == 1
    assert summary["model_counts"]["IQ Gateway"] == 1
    assert summary["firmware_counts"]["8.2.0"] == 2
    assert "serial_number" in summary["property_keys"]
    assert result["system_dashboard_details"]["modem"]["rssi"] == -72
    assert result["system_dashboard_details"]["controller"]["earth_type"] == "TN-C-S"
    assert (
        result["system_dashboard_details"]["hierarchy"]["relationships"][0][
            "device_uid"
        ]
        == "**REDACTED**"
    )


@pytest.mark.asyncio
async def test_device_diagnostics_envoy_gateway_summary_handles_bad_bucket_shapes(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: (
        {  # type: ignore[attr-defined]
            "type_label": "Gateway",
            "count": "bad",
            "devices": "bad",
        }
        if type_key == "envoy"
        else None
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    summary = result["gateway_summary"]
    assert summary["connectivity"] is None
    assert summary["connected_devices"] == 0


@pytest.mark.asyncio
async def test_device_diagnostics_microinverter_includes_summary(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.type_bucket = lambda type_key: (
        {  # type: ignore[attr-defined]
            "type_label": "Microinverters",
            "count": 3,
            "devices": [
                {"serial_number": "INV-A"},
                {"serial_number": "INV-B"},
                {"serial_number": "INV-C"},
            ],
            "status_counts": {
                "total": 3,
                "normal": 2,
                "warning": 0,
                "error": 0,
                "not_reporting": 1,
            },
            "status_summary": "Normal 2 | Warning 0 | Error 0 | Not Reporting 1",
            "model_summary": "IQ7A x3",
            "panel_info": {"pv_module_manufacturer": "Acme"},
            "latest_reported_utc": "2026-02-15T18:00:00+00:00",
        }
        if type_key == "microinverter"
        else None
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:microinverter")},
        manufacturer="Enphase",
        name="Microinverters",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    summary = result["microinverter_summary"]
    assert result["type_key"] == "microinverter"
    assert summary["connectivity"] == "degraded"
    assert summary["total_inverters"] == 3
    assert summary["reporting_inverters"] == 2
    assert summary["not_reporting_inverters"] == 1
    assert summary["panel_info"]["pv_module_manufacturer"] == "Acme"


@pytest.mark.asyncio
async def test_device_diagnostics_type_device_without_coordinator_payload(
    hass, config_entry
) -> None:
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={
            ("other", "value"),
            (DOMAIN, f"type:{RANDOM_SITE_ID}:encharge"),
        },
        manufacturer="Enphase",
        name="Battery (2)",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert result["site_id"] == "**REDACTED**"
    assert result["type_key"] == "encharge"
    assert result["count"] == 0
    assert result["devices"] == []


@pytest.mark.asyncio
async def test_device_diagnostics_encharge_includes_system_dashboard_details(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_system_dashboard_diagnostics = AsyncMock()
    coord.async_ensure_battery_status_diagnostics = AsyncMock()
    coord.type_bucket = lambda type_key: (
        {  # type: ignore[attr-defined]
            "type_label": "Battery",
            "count": 1,
            "devices": [{"serial_number": "BAT-1", "name": "Battery 1"}],
        }
        if type_key == "encharge"
        else None
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:encharge")},
        manufacturer="Enphase",
        name="Battery",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    coord.async_ensure_system_dashboard_diagnostics.assert_not_awaited()
    coord.async_ensure_battery_status_diagnostics.assert_not_awaited()
    assert result["system_dashboard_details"]["connectivity"]["rssi"] == -61
    assert result["system_dashboard_details"]["software"]["app_version"] == "1.2.3"
    assert result["system_dashboard_details"]["operation_mode"]["mode"] == "backup"
    assert (
        result["system_dashboard_details"]["hierarchy"]["relationships"][0][
            "parent_uid"
        ]
        == "**REDACTED**"
    )
    assert (
        result["battery_status_payload"]["storages"][0]["serial_number"]
        == "**REDACTED**"
    )
    assert result["battery_status_summary"]["aggregate_charge_pct"] == 48.0
    assert result["battery_status_summary"]["site_available_power_kw"] == 3.2
    assert "battery_order" not in result["battery_status_summary"]
    assert "per_battery_status" not in result["battery_status_summary"]


@pytest.mark.asyncio
async def test_device_diagnostics_encharge_does_not_prefetch_battery_status(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_system_dashboard_diagnostics = AsyncMock()
    coord.async_ensure_battery_status_diagnostics = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    coord._battery_status_payload = None
    coord.battery_status_summary = {}
    coord.type_bucket = lambda type_key: (
        {  # type: ignore[attr-defined]
            "type_label": "Battery",
            "count": 1,
            "devices": [{"serial_number": "BAT-1", "name": "Battery 1"}],
        }
        if type_key == "encharge"
        else None
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:encharge")},
        manufacturer="Enphase",
        name="Battery",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)

    coord.async_ensure_system_dashboard_diagnostics.assert_not_awaited()
    coord.async_ensure_battery_status_diagnostics.assert_not_awaited()
    assert "battery_status_payload" not in result
    assert "battery_status_summary" not in result


@pytest.mark.asyncio
async def test_device_diagnostics_does_not_prefetch_dashboard(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord.async_ensure_system_dashboard_diagnostics = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    coord.type_bucket = lambda type_key: (
        {  # type: ignore[attr-defined]
            "type_label": "Gateway",
            "count": 1,
            "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
        }
        if type_key == "envoy"
        else None
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)

    coord.async_ensure_system_dashboard_diagnostics.assert_not_awaited()
    assert result["type_key"] == "envoy"


@pytest.mark.asyncio
async def test_device_diagnostics_handles_system_dashboard_capture_error(
    hass, config_entry
) -> None:
    class BrokenDashboardCoordinator(DummyCoordinator):
        def system_dashboard_diagnostics(self):
            raise RuntimeError("boom")

    coord = BrokenDashboardCoordinator()
    coord.type_bucket = lambda type_key: (
        {  # type: ignore[attr-defined]
            "type_label": "Gateway",
            "count": 1,
            "devices": [{"serial_number": "GW-1"}],
        }
        if type_key == "envoy"
        else None
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
    )

    result = await diagnostics.async_get_device_diagnostics(hass, config_entry, device)
    assert "system_dashboard_details" not in result
