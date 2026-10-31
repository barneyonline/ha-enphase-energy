"""Sensor fallbacks preserve identities when aggregate metadata is incomplete."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.enphase_ev import sensor_gateway as gateway
from custom_components.enphase_ev import sensor_heatpump as heatpump
from custom_components.enphase_ev import sensor_inverter as inverter
from custom_components.enphase_ev.sensor_battery import (
    EnphaseBatteryStorageChargeSensor,
)
from custom_components.enphase_ev.sensor_tariff import (
    _tariff_now,
    EnphaseTariffRateValueSensor,
)


def legacy_coord(buckets, **extra):
    return SimpleNamespace(
        inventory_view=SimpleNamespace(type_bucket=lambda key: buckets.get(key)),
        **extra
    )


def test_gateway_fallback_snapshot_uses_newest_member_and_boolean_connection():
    coord = legacy_coord(
        {
            "envoy": {
                "devices": [
                    {
                        "serial_number": "new",
                        "connected": True,
                        "last_report": 1700000000,
                    },
                    {"serial_number": "old", "last_report": 1600000000},
                ]
            }
        }
    )
    snapshot = gateway._gateway_inventory_snapshot(coord)
    assert snapshot["connected_devices"] == 1
    assert snapshot["latest_reported_device"]["serial_number"] == "new"


def test_gateway_fallback_without_report_dates_has_no_timestamp():
    coord = legacy_coord({}, system_dashboard_envoy_detail=lambda: {})
    assert gateway._gateway_inventory_snapshot(coord)["latest_reported"] is None


def test_gateway_flattening_preserves_numeric_values_and_ignores_nested_terminals():
    assert gateway._gateway_flat_member_attributes({"count": 2}) == {"count": 2}
    assert gateway._gateway_terminal_values({"MID": {"nested": True}}) == {}
    assert (
        gateway._gateway_ip_member_kind({"type": "other", "channel_type": "unknown"})
        is None
    )
    assert (
        gateway._gateway_iq_energy_router_inventory_buckets({"value": {"result": None}})
        == []
    )


def test_router_records_preserve_restored_members_without_summary_extension():
    view = SimpleNamespace(
        gateway_iq_energy_router_summary_records=None,
        gateway_iq_energy_router_records=lambda: [
            {"device_type": "IQ_ENERGY_ROUTER", "device_uid": "ROUTER"}
        ],
    )
    records = gateway._gateway_iq_energy_router_records(
        SimpleNamespace(inventory_view=view)
    )
    assert len(records) == 1


def test_gateway_meter_without_dashboard_detail_extension():
    assert gateway._gateway_meter_member(legacy_coord({}), "production") is None


def test_dry_contact_legacy_host_without_type_bucket_map():
    assert gateway._gateway_dry_contact_members(legacy_coord({})) == []


@pytest.mark.parametrize("reported", [None, "2026-10-31T00:00:00Z"])
def test_heatpump_fallback_snapshot_preserves_provided_summary_fields(reported):
    bucket = {
        "count": 2,
        "status_summary": "Known summary",
        "overall_status_text": "Normal",
        "latest_reported_utc": reported,
        "devices": [
            {
                "device_type": "HEAT_PUMP",
                "device_uid": "new",
                "last_report": 1700000000,
            },
            {
                "device_type": "HEAT_PUMP",
                "device_uid": "old",
                "last_report": 1600000000,
            },
        ],
    }
    coord = legacy_coord(
        {"heatpump": bucket}, _hems_devices_last_success_mono=time.monotonic() + 60
    )
    snapshot = heatpump._heatpump_snapshot(coord)
    assert snapshot["status_summary"] == "Known summary"
    assert snapshot["overall_status_text"] == "Normal"
    assert snapshot["hems_last_success_age_s"] is None
    if reported is None:
        assert snapshot["latest_reported_device"]["device_uid"] == "new"


def test_heatpump_type_fallback_skips_members_without_status_and_older_reports():
    coord = legacy_coord(
        {
            "heatpump": {
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "new",
                        "last_report": 1700000000,
                    },
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "old",
                        "last_report": 1600000000,
                    },
                ]
            }
        }
    )
    snapshot = heatpump._heatpump_type_snapshot(coord, device_type="HEAT_PUMP")
    assert snapshot["latest_reported_device"]["device_uid"] == "new"


def test_inverter_fallback_uses_newest_inventory_report():
    coord = legacy_coord(
        {
            "microinverter": {
                "devices": [
                    {"serial_number": "new", "last_report": 1700000000},
                    {"serial_number": "old", "last_report": 1600000000},
                ]
            }
        }
    )
    snapshot = inverter._microinverter_inventory_snapshot(coord)
    assert snapshot["latest_reported_device"]["serial_number"] == "new"


@pytest.mark.parametrize("snapshot", [None, {"lifetime_kwh": 1}])
def test_inverter_sensor_without_revision_cache_reads_each_snapshot(
    coordinator_factory, monkeypatch, snapshot
):
    coord = coordinator_factory()
    coord.data = None
    coord._inverter_data = None
    read = Mock(return_value=snapshot)
    monkeypatch.setattr(coord, "inverter_data", read)
    entity = inverter.EnphaseInverterLifetimeEnergySensor(coord, "INV")
    assert entity._snapshot() == snapshot
    assert entity._snapshot() == snapshot
    assert read.call_count == 2


def test_battery_label_skips_blank_friendly_name(coordinator_factory):
    coord = coordinator_factory()
    coord._battery_storage_data = {"BAT": {"name": " ", "serial_number": "BAT"}}
    assert EnphaseBatteryStorageChargeSensor(coord, "BAT")._battery_label == "BAT"


def test_tariff_time_without_site_timezone_helper_uses_hass_timezone(hass):
    hass.config.time_zone = "Australia/Melbourne"
    assert _tariff_now(SimpleNamespace(), hass).tzinfo.key == "Australia/Melbourne"


def test_tariff_rate_sensor_ignores_nonmatching_rate_details(coordinator_factory):
    coord = coordinator_factory()
    coord._tariff_import_rate = {"rate": 0.25}
    entity = EnphaseTariffRateValueSensor(
        coord, {"key": "missing", "name": "Missing"}, is_import=True
    )
    assert entity.native_value is None


@pytest.mark.parametrize("storages", [{}, [{"battery_mode": " "}], []])
def test_battery_mode_without_usable_storage_value(coordinator_factory, storages):
    from custom_components.enphase_ev.sensor import EnphaseBatteryModeSensor

    coord = coordinator_factory()
    coord._battery_grid_mode = None
    coord._battery_status_payload = {"storages": storages}
    assert EnphaseBatteryModeSensor(coord)._mode_raw() is None


@pytest.mark.parametrize("row", [{"energy_kwh": 1}, {"energy_kwh": 1, "start": 100}])
def test_session_history_without_session_identifier(coordinator_factory, row):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    coord = coordinator_factory(data={"EV": {"energy_today_sessions": [row]}})
    sensor = EnphaseEnergyTodaySensor(coord, "EV")
    result = sensor._extract_history_session(coord.data["EV"])
    assert result["session_id"] is None
    assert result["session_key"] == ("100.0:none" if "start" in row else None)


@pytest.mark.parametrize(
    "context,row",
    [
        ({"start": 200}, {"start": 100}),
        ({"start": 100, "end": 300}, {"start": 100, "end": 400}),
        ({"end": 300}, {"end": 400}),
    ],
)
def test_history_does_not_merge_distinct_session_times(
    coordinator_factory, context, row
):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    coord = coordinator_factory(data={"EV": {"energy_today_sessions": [row]}})
    sensor = EnphaseEnergyTodaySensor(coord, "EV")
    sensor._last_context_source = "live"
    assert sensor._merge_history_context(context) == context


def test_session_metadata_prefers_explicit_authentication_status():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    attrs = EnphaseEnergyTodaySensor._session_metadata_attributes(
        {"session_auth_status": 1}, context={"session_auth_status": 2}
    )
    assert attrs["session_auth_status"] == 2


@pytest.mark.parametrize("kind", ["last_report", "mode"])
def test_boolean_attributes_reject_container_values(coordinator_factory, kind):
    from custom_components.enphase_ev.sensor import (
        EnphaseLastReportedSensor,
        EnphaseChargeModeSensor,
    )

    coord = coordinator_factory(
        data={"EV": {"dlb_enabled": {}, "green_battery_enabled": {}}}
    )
    sensor = (
        EnphaseLastReportedSensor(coord, "EV")
        if kind == "last_report"
        else EnphaseChargeModeSensor(coord, "EV")
    )
    assert sensor.extra_state_attributes is not None
    assert EnphaseChargeModeSensor._as_bool({}) is None


def test_power_without_lifetime_preserves_charging_estimate(coordinator_factory):
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    coord = coordinator_factory(data={"EV": {"charging": True}})
    sensor = EnphasePowerSensor(coord, "EV")
    sensor._last_power_w = 1200
    assert sensor.native_value == 1200
    assert sensor._power_topology({"phase_mode": "unknown"}) == "unknown"


@pytest.mark.parametrize(
    "last_ts,method", [(None, "derived"), (100.0, "derived"), (None, None)]
)
async def test_power_legacy_restore_with_existing_timestamp_and_method(
    coordinator_factory, monkeypatch, last_ts, method
):
    from unittest.mock import AsyncMock
    from homeassistant.core import State
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sensor = EnphasePowerSensor(coordinator_factory(), "EV")
    restored = {"last_reset_at": 100.0, "last_energy_ts": last_ts}
    monkeypatch.setattr(
        sensor,
        "async_get_last_extra_data",
        AsyncMock(return_value=SimpleNamespace(as_dict=lambda: restored)),
    )
    monkeypatch.setattr(
        sensor,
        "async_get_last_state",
        AsyncMock(
            return_value=State(
                "sensor.power",
                "50",
                {
                    "baseline_kwh": 1,
                    "last_energy_today_kwh": 2,
                    "last_ts": 200.0,
                    "method": method,
                },
            )
        ),
    )
    await sensor.async_added_to_hass()
    assert sensor._last_lifetime_kwh == 3
    assert sensor._last_energy_ts == (last_ts or 200.0)


def test_reported_connection_rejects_object_value(coordinator_factory):
    from custom_components.enphase_ev.sensor import EnphaseLastReportedSensor

    coord = coordinator_factory(data={"EV": {"is_connected": {}}})
    assert (
        EnphaseLastReportedSensor(coord, "EV").extra_state_attributes["is_connected"]
        is None
    )


def test_tariff_detail_skips_valid_nonmatching_spec(coordinator_factory):
    from custom_components.enphase_ev.tariff import parse_tariff_rate
    from .test_tariff import _write_test_branch

    coord = coordinator_factory()
    coord.tariff_import_rate = parse_tariff_rate(
        {"purchase": _write_test_branch("0.2")}, "purchase"
    )
    entity = EnphaseTariffRateValueSensor(
        coord, {"key": "missing", "name": "Missing"}, is_import=True
    )
    assert entity.native_value is None


def test_dry_contact_settings_without_schedule_or_status(
    coordinator_factory, monkeypatch
):
    coord = coordinator_factory()
    entity = gateway.EnphaseDryContactsInventorySensor(coord)
    monkeypatch.setattr(
        entity,
        "_members",
        lambda: [
            {"serial_number": "DC", "last_report": 200},
            {"serial_number": "OLD", "last_report": 100},
        ],
    )
    coord.battery_runtime.parse_dry_contact_settings_payload(
        {"contacts": [{"serial": "DC", "controlMode": "manual"}]}
    )
    assert entity.native_value is None
    assert entity.extra_state_attributes["member_count"] == 2
    monkeypatch.setattr(entity, "_members", lambda: [{"serial_number": "DC"}])
    assert "schedule_windows" not in entity.extra_state_attributes


def test_site_power_skips_unknown_flow_sources_and_intervals(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor_site_energy import (
        EnphaseGridPowerSensor,
        _SiteLifetimePowerRestoreData,
    )

    coord = coordinator_factory()
    sensor = EnphaseGridPowerSensor(coord)
    parsed = _SiteLifetimePowerRestoreData.from_dict(
        {"last_live_flow_sources": {"grid_import": [" "]}}
    )
    assert parsed.last_live_flow_sources == {}
    sensor._restored_power_w = 123
    sensor._clear_restored_live_history()
    assert sensor._restored_power_w == 123
    assert (
        sensor._current_flow_sources(
            {"grid_import": {"fields_used": [" "]}}, {"grid_import": 1}
        )
        == {}
    )
    assert (
        sensor._minimum_window_seconds({"grid_import": object()}, {"grid_import": 1})
        is None
    )
    monkeypatch.setattr(coord.discovery_snapshot, "site_energy_channel_known", None)
    assert not sensor._flow_supported("unknown")


async def test_restore_current_power_ignores_invalid_dates(
    coordinator_factory, monkeypatch
):
    from unittest.mock import AsyncMock
    from homeassistant.core import State
    from custom_components.enphase_ev.sensor_site_energy import (
        EnphaseCurrentPowerConsumptionSensor,
    )

    entity = EnphaseCurrentPowerConsumptionSensor(coordinator_factory())
    monkeypatch.setattr(
        entity, "async_get_last_extra_data", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        entity,
        "async_get_last_state",
        AsyncMock(
            return_value=State(
                "sensor.power",
                "100",
                {"sampled_at_utc": "invalid", "cached_at_utc": "invalid"},
            )
        ),
    )
    await entity._async_restore_power()
    assert entity._last_good_sample_utc is None
    assert entity._last_good_cached_at_utc is None


def test_heatpump_energy_device_info_falls_back_to_cloud(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor_site_energy import EnphaseSiteEnergySensor

    coord = coordinator_factory()
    monkeypatch.setattr(coord.inventory_view, "has_type", lambda key: True)
    monkeypatch.setattr(coord.inventory_view, "type_device_info", lambda key: None)
    entity = EnphaseSiteEnergySensor(
        coord, "heat_pump", "heat_pump_energy", "Heat Pump Energy"
    )
    assert entity.device_info


async def test_negative_restored_cumulative_flow_is_discarded(
    coordinator_factory, monkeypatch
):
    from unittest.mock import AsyncMock
    from homeassistant.core import State
    from custom_components.enphase_ev.sensor_site_energy import EnphaseGridPowerSensor

    sensor = EnphaseGridPowerSensor(coordinator_factory())
    monkeypatch.setattr(
        sensor,
        "async_get_last_state",
        AsyncMock(
            return_value=State(
                "sensor.power", "100", {"last_flow_kwh": {"grid_import": -1}}
            )
        ),
    )
    monkeypatch.setattr(
        sensor, "async_get_last_extra_data", AsyncMock(return_value=None)
    )
    await sensor._async_restore_power()
    assert sensor._last_flow_kwh == {}


def test_consumption_processing_without_energy_diagnostics(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor_site_energy import (
        EnphaseSiteConsumptionPowerSensor,
    )

    coord = coordinator_factory()
    entity = EnphaseSiteConsumptionPowerSensor(coord)
    with monkeypatch.context() as context:
        context.setattr(coord, "energy", None)
        entity._process_current_sample()
    assert entity.native_value is None


def test_system_controller_without_terminal_descriptions(
    coordinator_factory, monkeypatch
):
    entity = gateway.EnphaseSystemControllerInventorySensor(coordinator_factory())
    monkeypatch.setattr(entity, "_member", lambda: {"serial_number": "CTRL"})
    assert "terminal_descriptions" not in entity.extra_state_attributes


def test_heatpump_binary_sensor_without_optional_runtime_uid(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.binary_sensor import (
        HeatPumpSgReadyActiveBinarySensor,
    )

    coord = coordinator_factory()
    monkeypatch.setattr(coord.inventory_view, "has_type_for_entities", lambda key: True)
    monkeypatch.setattr(coord, "_heatpump_runtime_device_uid", None)
    entity = HeatPumpSgReadyActiveBinarySensor(coord)
    monkeypatch.setattr(entity, "_snapshot", lambda: {"sg_ready_active": True})
    assert entity.available


async def test_restored_reset_with_nonfinite_sample_has_no_freshness(
    coordinator_factory, monkeypatch
):
    from unittest.mock import AsyncMock
    from homeassistant.core import State
    from custom_components.enphase_ev.sensor_site_energy import EnphaseGridPowerSensor

    entity = EnphaseGridPowerSensor(coordinator_factory())
    monkeypatch.setattr(
        entity,
        "async_get_last_state",
        AsyncMock(
            return_value=State(
                "sensor.power",
                "0",
                {
                    "last_flow_kwh": {"grid_import": 1},
                    "last_sample_ts": float("nan"),
                    "last_energy_ts": 200,
                },
            )
        ),
    )
    previous = {
        "previous_live_flow_kwh": {"grid_import": 10},
        "previous_live_sample_ts": 100,
        "previous_live_energy_ts": 100,
        "last_live_flow_sources": {"grid_import": ["import"]},
        "previous_live_flow_sources": {"grid_import": ["import"]},
    }
    monkeypatch.setattr(
        entity,
        "async_get_last_extra_data",
        AsyncMock(return_value=SimpleNamespace(as_dict=lambda: previous)),
    )
    await entity._async_restore_power()
    assert entity._accepted_power_freshness is None
