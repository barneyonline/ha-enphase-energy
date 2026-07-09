from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
import time
from unittest.mock import MagicMock

import pytest

from custom_components.enphase_ev.const import PHASE_SWITCH_CONFIG_SETTING
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

pytest.importorskip("homeassistant")


def _mk_coord_with(sn: str, payload: dict):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: payload}
    coord.serials = {sn}
    coord.last_set_amps = {}
    coord.site_id = "site"
    coord.last_update_success = True
    return coord


def test_statistical_energy_sensor_classes_declare_state_class():
    from homeassistant.components.sensor import SensorDeviceClass

    from custom_components.enphase_ev import sensor as sensor_module

    statistical_device_classes = {
        SensorDeviceClass.BATTERY,
        SensorDeviceClass.CURRENT,
        SensorDeviceClass.ENERGY,
        SensorDeviceClass.ENERGY_STORAGE,
        SensorDeviceClass.POWER,
    }

    missing_state_class = sorted(
        name
        for name, cls in vars(sensor_module).items()
        if isinstance(cls, type)
        and getattr(cls, "_attr_device_class", None) in statistical_device_classes
        and getattr(cls, "_attr_state_class", None) is None
    )

    assert missing_state_class == []


def test_charging_level_fallback():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "session_start": None,
        },
    )
    coord.set_last_set_amps = lambda s, a: None  # no-op
    coord.pick_start_amps = lambda s: 30
    coord.last_set_amps[sn] = 30

    sensor = EnphaseChargingLevelSensor(coord, sn)
    assert sensor.native_value == 30


def test_charging_level_attributes_include_limits():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging_level": 32,
            "min_amp": " 6 ",
            "max_amp": 40,
            "max_current": "48",
            "amp_granularity": "2",
            "default_charge_level": "disabled",
            "charging_amps_supported": True,
        },
    )

    sensor = EnphaseChargingLevelSensor(coord, sn)
    attrs = sensor.extra_state_attributes
    assert attrs == {
        "min_amp": 6,
        "max_amp": 40,
        "max_current": 48,
        "amp_granularity": 2,
        "default_charge_level": "disabled",
        "charging_amps_supported": True,
        "safe_limit_state": None,
        "safe_limit_active": False,
    }

    coord.data[sn]["min_amp"] = "bad"
    coord.data[sn]["max_amp"] = None
    coord.data[sn]["max_current"] = ""
    coord.data[sn]["amp_granularity"] = "bad"
    attrs = sensor.extra_state_attributes
    assert attrs == {
        "min_amp": None,
        "max_amp": None,
        "max_current": None,
        "amp_granularity": None,
        "default_charge_level": "disabled",
        "charging_amps_supported": True,
        "safe_limit_state": None,
        "safe_limit_active": False,
    }


@pytest.mark.parametrize("mode", ["GREEN_CHARGING", "SMART_CHARGING"])
def test_charging_level_unavailable_when_amp_control_not_applicable(mode):
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging_level": 32,
            "charge_mode_pref": mode,
        },
    )

    sensor = EnphaseChargingLevelSensor(coord, sn)

    assert sensor.available is False


def test_charging_level_invalid_value_falls_back():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor
    from custom_components.enphase_ev.const import SAFE_LIMIT_AMPS

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging_level": "bad",
        },
    )
    coord.pick_start_amps = lambda s: 18

    sensor = EnphaseChargingLevelSensor(coord, sn)
    assert sensor.native_value == 18

    coord.data[sn]["safe_limit_state"] = 1
    coord.data[sn]["charging_level"] = 32
    coord.data[sn]["charging"] = True
    assert sensor.native_value == SAFE_LIMIT_AMPS

    coord.data[sn]["min_amp"] = 16
    assert sensor.native_value == 16

    coord.data[sn]["charging"] = False
    assert sensor.native_value == 32

    coord.data[sn]["charging"] = "false"
    assert sensor.native_value == 32

    coord.data[sn]["safe_limit_state"] = True
    coord.data[sn]["charging"] = True
    assert sensor.native_value == 16

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord.data[sn]["safe_limit_state"] = BadStr()
    assert sensor.native_value == 32

    coord.data[sn]["safe_limit_state"] = 0
    assert sensor.native_value == 32


def test_charging_level_includes_safe_limit_state_attributes():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging_level": 32,
            "safe_limit_state": 1,
        },
    )

    sensor = EnphaseChargingLevelSensor(coord, sn)
    attrs = sensor.extra_state_attributes
    assert attrs["safe_limit_state"] == 1
    assert attrs["safe_limit_active"] is True


def test_charging_level_charging_active_coercion():
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    assert EnphaseChargingLevelSensor._charging_active(None) is False
    assert EnphaseChargingLevelSensor._charging_active(True) is True
    assert EnphaseChargingLevelSensor._charging_active(1) is True
    assert EnphaseChargingLevelSensor._charging_active(0) is False
    assert EnphaseChargingLevelSensor._charging_active("true") is True
    assert EnphaseChargingLevelSensor._charging_active("0") is False
    assert EnphaseChargingLevelSensor._charging_active("maybe") is False
    assert EnphaseChargingLevelSensor._charging_active(object()) is False


def test_electrical_phase_sensor_formats_state_and_attributes():
    from custom_components.enphase_ev.sensor import EnphaseElectricalPhaseSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "phase_mode": "3",
            PHASE_SWITCH_CONFIG_SETTING: "auto",
            "dlb_enabled": "true",
            "dlb_active": 1,
        },
    )

    sensor = EnphaseElectricalPhaseSensor(coord, sn)
    assert sensor.native_value == "Three Phase"
    attrs = sensor.extra_state_attributes
    assert attrs["phase_mode_raw"] == "3"
    assert attrs[PHASE_SWITCH_CONFIG_SETTING] == "auto"
    assert attrs["dlb_enabled"] is True
    assert attrs["dlb_active"] is True

    coord.data[sn]["phase_mode"] = " "
    coord.data[sn][PHASE_SWITCH_CONFIG_SETTING] = None
    coord.data[sn]["dlb_enabled"] = None
    coord.data[sn]["dlb_active"] = None
    assert sensor.native_value is None
    attrs = sensor.extra_state_attributes
    assert attrs["phase_mode_raw"] is None
    assert attrs[PHASE_SWITCH_CONFIG_SETTING] is None
    assert attrs["dlb_enabled"] is None
    assert attrs["dlb_active"] is None


def test_last_reported_sensor_exposes_charger_config_attributes():
    from custom_components.enphase_ev.sensor import EnphaseLastReportedSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "last_reported_at": "2025-09-09T09:00:00Z[UTC]",
            "default_charge_level": "disabled",
            "phase_switch_config": "auto",
        },
    )

    sensor = EnphaseLastReportedSensor(coord, sn)
    attrs = sensor.extra_state_attributes

    assert attrs["default_charge_level"] == "disabled"
    assert attrs["phase_switch_config"] == "auto"


def test_power_sensor_uses_lifetime_delta():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 10.0,
            "last_reported_at": "2025-09-09T10:00:00Z[UTC]",
            "charging": True,
        },
    )

    sensor = EnphasePowerSensor(coord, sn)
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 10.6  # +0.6 kWh
    coord.data[sn]["last_reported_at"] = "2025-09-09T10:05:00Z[UTC]"
    val = sensor.native_value
    assert val == 7200
    assert sensor.extra_state_attributes["last_window_seconds"] == pytest.approx(300)

    coord.data[sn]["lifetime_kwh"] = 10.6
    coord.data[sn]["last_reported_at"] = "2025-09-09T10:06:00Z[UTC]"
    assert sensor.native_value == 7200


def test_power_sensor_zero_when_idle():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 5.0,
            "last_reported_at": "2025-09-09T09:00:00Z",
            "charging": True,
        },
    )
    sensor = EnphasePowerSensor(coord, sn)
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 5.5
    coord.data[sn]["last_reported_at"] = "2025-09-09T09:05:00Z"
    assert sensor.native_value == 6000

    coord.data[sn]["charging"] = False
    coord.data[sn]["last_reported_at"] = "2025-09-09T09:06:00Z"
    assert sensor.native_value == 0


def test_storm_guard_state_sensor_normalizes():
    from custom_components.enphase_ev.sensor import EnphaseStormGuardStateSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "storm_guard_state": "enabled",
        },
    )
    sensor = EnphaseStormGuardStateSensor(coord, sn)
    assert sensor.native_value == "Enabled"
    assert sensor.entity_category is None
    assert sensor.available is True

    coord._storm_guard_pending_state = "disabled"  # noqa: SLF001
    coord._storm_guard_pending_expires_mono = time.monotonic() + 30.0  # noqa: SLF001
    coord.data[sn]["storm_guard_state"] = None
    assert sensor.available is True
    assert sensor.native_value == "Updating"
    coord.data[sn]["storm_guard_state"] = "enabled"
    coord._storm_guard_pending_state = None  # noqa: SLF001
    coord._storm_guard_pending_expires_mono = None  # noqa: SLF001

    coord.data[sn]["storm_guard_state"] = "Disabled"
    assert sensor.native_value == "Disabled"

    coord.data[sn]["storm_guard_state"] = 1
    assert sensor.native_value == "Enabled"

    coord.data[sn]["storm_guard_state"] = 0
    assert sensor.native_value == "Disabled"

    coord.data[sn]["storm_guard_state"] = "on"
    assert sensor.native_value == "Enabled"

    coord.data[sn]["storm_guard_state"] = "off"
    assert sensor.native_value == "Disabled"

    coord.data[sn]["storm_guard_state"] = True
    assert sensor.native_value == "Enabled"

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord.data[sn]["storm_guard_state"] = BadStr()
    assert sensor.native_value is None

    coord.data[sn]["storm_guard_state"] = "mystery"
    assert sensor.native_value is None

    coord.data[sn]["storm_guard_state"] = None
    assert sensor.native_value is None
    assert sensor.available is False

    coord.data[sn]["storm_guard_state"] = "enabled"
    coord.last_update_success = False
    assert sensor.available is False


def test_storm_alert_sensor_states():
    from types import SimpleNamespace
    from custom_components.enphase_ev.sensor import EnphaseStormAlertSensor
    from homeassistant.helpers.entity import EntityCategory

    coord = SimpleNamespace(
        site_id="site",
        storm_alert_active=None,
        storm_alert_critical_override=None,
        storm_alerts=[],
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )
    sensor = EnphaseStormAlertSensor(coord)
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.native_value is None
    assert sensor.device_info["identifiers"] == {("enphase_ev", "type:site:envoy")}
    assert sensor.extra_state_attributes["storm_alert_active"] is None

    coord.storm_alert_active = True
    coord.storm_alert_critical_override = True
    coord.storm_alerts = [{"type": "wind"}]
    assert sensor.native_value == "active"
    assert sensor.extra_state_attributes["storm_alert_active"] is True
    assert sensor.extra_state_attributes["critical_alert_override"] is True
    assert sensor.extra_state_attributes["storm_alert_count"] == 1

    coord.storm_alert_active = False
    coord.storm_alerts = "bad"
    assert sensor.native_value == "inactive"
    assert sensor.extra_state_attributes["storm_alert_active"] is False
    assert sensor.extra_state_attributes["storm_alert_count"] == 0


def test_battery_overall_charge_sensor_states():
    from types import SimpleNamespace

    from homeassistant.components.sensor import SensorStateClass

    from custom_components.enphase_ev.sensor import EnphaseBatteryOverallChargeSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_aggregate_charge_pct=47.8,
        battery_status_summary={
            "aggregate_status": "normal",
            "aggregate_charge_source": "computed",
            "included_count": 2,
            "contributing_count": 2,
            "missing_energy_capacity_keys": [],
            "excluded_count": 0,
            "available_energy_kwh": 4.75,
            "max_capacity_kwh": 10.0,
            "site_current_charge_pct": 48.0,
            "site_available_energy_kwh": 4.75,
            "site_max_capacity_kwh": 10.0,
            "site_available_power_kw": 7.68,
            "site_max_power_kw": 7.68,
            "site_total_micros": 12,
            "site_active_micros": 12,
            "site_inactive_micros": 0,
            "site_included_count": 2,
            "site_excluded_count": 0,
            "battery_order": ["BAT-1", "BAT-2"],
        },
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )

    sensor = EnphaseBatteryOverallChargeSensor(coord)
    assert sensor.available is True
    assert sensor.native_value == 47.8
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.device_info["identifiers"] == {("enphase_ev", "type:site:encharge")}
    provided = {"identifiers": {("enphase_ev", "type:site:encharge")}}
    coord.inventory_view.type_device_info = lambda _key: provided
    assert sensor.device_info is provided
    assert sensor.extra_state_attributes == {}

    coord.battery_aggregate_charge_pct = None
    assert sensor.available is False


def test_battery_overall_status_sensor_states():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryOverallStatusSensor
    from homeassistant.helpers.entity import EntityCategory

    coord = SimpleNamespace(
        site_id="site",
        battery_aggregate_status="warning",
        battery_status_summary={
            "aggregate_charge_pct": 30.0,
            "aggregate_charge_source": "computed",
            "included_count": 2,
            "contributing_count": 2,
            "missing_energy_capacity_keys": [],
            "excluded_count": 1,
            "worst_storage_key": "BAT-2",
            "worst_status": "warning",
            "per_battery_status": {"BAT-1": "normal", "BAT-2": "warning"},
            "per_battery_status_raw": {"BAT-1": "normal", "BAT-2": "warning"},
            "per_battery_status_text": {"BAT-1": "Normal", "BAT-2": "Warning"},
            "battery_order": ["BAT-1", "BAT-2"],
        },
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )

    sensor = EnphaseBatteryOverallStatusSensor(coord)
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.available is True
    assert sensor.native_value == "warning"
    attrs = sensor.extra_state_attributes
    assert attrs["worst_storage_key"] == "BAT-2"
    assert attrs["per_battery_status"]["BAT-2"] == "warning"

    coord.battery_aggregate_status = None
    assert sensor.available is False


def test_battery_cfg_schedule_status_sensor_states():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryCfgScheduleStatusSensor,
    )
    from homeassistant.helpers.entity import EntityCategory

    coord = SimpleNamespace(
        site_id="site",
        battery_cfg_schedule_status="pending",
        charge_from_grid_control_available=True,
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )

    sensor = EnphaseBatteryCfgScheduleStatusSensor(coord)
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.available is True
    assert sensor.native_value == "pending"

    coord.battery_cfg_schedule_status = None
    assert sensor.native_value == "none"

    coord.charge_from_grid_control_available = False
    assert sensor.available is False

    coord.charge_from_grid_control_available = True
    coord.last_update_success = False
    assert sensor.available is False


def test_battery_storage_charge_sensor_snapshot():
    from types import SimpleNamespace

    from homeassistant.components.sensor import SensorStateClass

    from custom_components.enphase_ev.sensor import EnphaseBatteryStorageChargeSensor

    snapshot = {
        "identity": "BAT-1",
        "name": "IQ Battery 5P",
        "serial_number": "BAT-1",
        "current_charge_pct": 48.0,
        "status": "normal",
    }
    coord = SimpleNamespace(
        site_id="site",
        last_update_success=True,
        battery_storage=lambda _serial: snapshot,
        inventory_view=SimpleNamespace(
            type_device_info=lambda _key: None,
            has_type_for_entities=lambda _key: True,
        ),
    )

    sensor = EnphaseBatteryStorageChargeSensor(coord, "BAT-1")
    assert sensor.available is True
    assert sensor.name == "IQ Battery 5P"
    assert sensor.native_value == 48.0
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.extra_state_attributes["serial_number"] == "BAT-1"
    assert sensor.device_info["identifiers"] == {("enphase_ev", "type:site:encharge")}


def test_battery_storage_charge_sensor_edge_paths():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryStorageChargeSensor

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord = SimpleNamespace(
        site_id="site",
        last_update_success=True,
        battery_storage="not-callable",
        inventory_view=SimpleNamespace(
            type_device_info=lambda _key: None,
            has_type_for_entities=lambda _key: True,
        ),
    )
    sensor = EnphaseBatteryStorageChargeSensor(coord, "BAT-EDGE")
    assert sensor.available is False
    assert sensor.native_value is None

    coord.battery_storage = lambda _serial: {"name": None, "serial_number": BadStr()}
    assert sensor.name == "BAT-EDGE"
    assert sensor.native_value is None

    coord.battery_storage = lambda _serial: {
        "name": "Edge Battery",
        "current_charge_pct": BadStr(),
    }
    assert sensor.name == "Edge Battery"
    assert sensor.native_value is None
    info = sensor.device_info
    assert info["name"] == "IQ Battery"

    coord.battery_storage = lambda _serial: ["bad"]
    assert sensor.available is False

    coord.inventory_view.has_type_for_entities = lambda _key: False
    assert sensor.available is False

    coord.inventory_view.has_type_for_entities = lambda _key: True
    expected_info = {"identifiers": {("enphase_ev", "type:site:encharge")}}
    coord.inventory_view.type_device_info = lambda _key: expected_info
    assert sensor.device_info == expected_info


def test_battery_storage_detail_sensors_state_and_attributes():
    from types import SimpleNamespace

    from homeassistant.components.sensor import SensorStateClass

    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryStorageCycleCountSensor,
        EnphaseBatteryStorageHealthSensor,
        EnphaseBatteryStorageLastReportedSensor,
        EnphaseBatteryStorageStatusSensor,
    )

    snapshot = {
        "identity": "BAT-1",
        "name": "IQ Battery 5P",
        "serial_number": "BAT-1",
        "status": "warning",
        "status_text": "Warning",
        "status_normalized": "warning",
        "led_status": 12,
        "battery_soh": "98.6",
        "cycle_count": "123",
        "last_reported": "2026-02-15T05:31:33Z[UTC]",
    }
    coord = SimpleNamespace(
        site_id="site",
        last_update_success=True,
        battery_storage=lambda _serial: snapshot,
        inventory_view=SimpleNamespace(
            type_device_info=lambda _key: None,
            has_type_for_entities=lambda _key: True,
        ),
    )

    status = EnphaseBatteryStorageStatusSensor(coord, "BAT-1")
    health = EnphaseBatteryStorageHealthSensor(coord, "BAT-1")
    cycle = EnphaseBatteryStorageCycleCountSensor(coord, "BAT-1")
    last_reported = EnphaseBatteryStorageLastReportedSensor(coord, "BAT-1")

    assert status.available is True
    assert status.entity_category is None
    assert status.native_value == "charging"
    status_attrs = status.extra_state_attributes
    assert status_attrs["state"] == 12

    assert health.available is True
    assert health.native_value == 98.6
    assert health.state_class == SensorStateClass.MEASUREMENT
    assert cycle.native_value == 123
    assert last_reported.entity_registry_enabled_default is False
    assert last_reported.available is True

    timestamp = last_reported.native_value
    assert timestamp is not None
    assert timestamp == datetime(2026, 2, 15, 5, 31, 33, tzinfo=timezone.utc)

    snapshot["status_text"] = None
    snapshot["status_normalized"] = "normal"
    snapshot["led_status"] = 17
    snapshot["battery_soh"] = None
    snapshot["soh"] = "97.5%"
    snapshot["cycle_count"] = "bad"
    snapshot["last_reported"] = None
    assert status.native_value == "idle"
    assert health.native_value == 97.5
    assert health.available is True
    assert cycle.native_value is None
    assert last_reported.native_value is None
    assert last_reported.available is False

    snapshot["led_status"] = 13
    assert status.native_value == "discharging"
    assert status.extra_state_attributes["state"] == 13

    snapshot["led_status"] = 14
    assert status.native_value == "idle"
    assert status.extra_state_attributes["state"] == 14

    snapshot["led_status"] = 15
    assert status.native_value == "idle"
    assert status.extra_state_attributes["state"] == 15

    snapshot["led_status"] = 16
    assert status.native_value == "idle"
    assert status.extra_state_attributes["state"] == 16

    snapshot["led_status"] = 99
    assert status.native_value == "unknown"
    assert status.extra_state_attributes["state"] == 99

    snapshot["led_status"] = None
    assert status.native_value == "unknown"
    assert status.extra_state_attributes == {}

    snapshot["soh"] = None
    assert health.native_value is None
    assert health.available is False

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    snapshot["status_text"] = BadStr()
    snapshot["status_normalized"] = "warning"
    snapshot["led_status"] = BadStr()
    assert status.native_value == "unknown"
    assert status.extra_state_attributes == {}

    snapshot["status"] = BadStr()
    snapshot["status_text"] = "Degraded"
    snapshot["led_status"] = "12"
    attrs = status.extra_state_attributes
    assert status.native_value == "charging"
    assert attrs["state"] == 12
    snapshot["status"] = "warning"
    snapshot["status_text"] = None

    snapshot["status"] = "NOT_REPORTING"
    snapshot["status_text"] = "Not Reporting"
    snapshot["led_status"] = "12.0"
    attrs = status.extra_state_attributes
    assert status.native_value == "charging"
    assert attrs["state"] == 12
    snapshot["status_text"] = "   "
    snapshot["led_status"] = "12.5"
    assert status.native_value == "unknown"
    assert status.extra_state_attributes == {}
    snapshot["led_status"] = "   "
    assert status.native_value == "unknown"
    assert status.extra_state_attributes == {}
    snapshot["led_status"] = "bad"
    assert status.native_value == "unknown"
    assert status.extra_state_attributes == {}
    snapshot["status"] = "warning"
    snapshot["status_text"] = None

    snapshot["status_normalized"] = None
    snapshot["led_status"] = " 17 "
    assert status.native_value == "idle"
    snapshot["status_normalized"] = BadStr()
    snapshot["led_status"] = 14.0
    assert status.native_value == "idle"

    snapshot["battery_soh"] = BadStr()
    assert health.native_value is None
    assert health.available is False
    assert EnphaseBatteryStorageHealthSensor._parse_health_value("") is None
    assert EnphaseBatteryStorageHealthSensor._parse_health_value("%") is None
    assert EnphaseBatteryStorageHealthSensor._parse_health_value("bad") is None
    assert EnphaseBatteryStorageHealthSensor._parse_health_value("NaN") is None
    assert EnphaseBatteryStorageHealthSensor._parse_health_value("inf") is None
    assert EnphaseBatteryStorageHealthSensor._parse_health_value("-inf") is None
    snapshot["cycle_count"] = None
    assert cycle.native_value is None

    snapshot["last_reported"] = "   "
    assert last_reported.native_value is None
    snapshot["last_reported"] = "bad-timestamp"
    assert last_reported.native_value is None
    snapshot["last_reported"] = "2026-02-15T05:31:33"
    assert last_reported.native_value == datetime(
        2026, 2, 15, 5, 31, 33, tzinfo=timezone.utc
    )
    snapshot["last_reported"] = datetime(2026, 2, 15, 5, 31, 33, tzinfo=timezone.utc)
    assert last_reported.native_value == datetime(
        2026, 2, 15, 5, 31, 33, tzinfo=timezone.utc
    )
    snapshot["last_reported"] = None
    snapshot["last_report"] = 1_771_144_293
    assert last_reported.native_value == datetime(
        2026, 2, 15, 8, 31, 33, tzinfo=timezone.utc
    )


def test_battery_site_summary_sensors_state_and_attributes():
    from types import SimpleNamespace
    from datetime import datetime, timezone

    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryAvailableEnergySensor,
        EnphaseBatteryAvailablePowerSensor,
    )

    coord = SimpleNamespace(
        site_id="site",
        battery_status_summary={
            "site_available_energy_kwh": 4.75,
            "site_max_capacity_kwh": 10.0,
            "site_current_charge_pct": 47.5,
            "site_included_count": 2,
            "site_excluded_count": 0,
            "site_available_power_kw": 7.68,
            "site_max_power_kw": 7.68,
            "site_inactive_micros": 1,
            "site_total_micros": 12,
            "site_active_micros": 11,
        },
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
        battery_summary_sample_utc=datetime(2026, 3, 11, 5, 40, tzinfo=timezone.utc),
    )

    energy = EnphaseBatteryAvailableEnergySensor(coord)
    power = EnphaseBatteryAvailablePowerSensor(coord)

    assert energy.device_class == SensorDeviceClass.ENERGY_STORAGE
    assert energy.state_class == SensorStateClass.MEASUREMENT
    assert energy.available is True
    assert energy.native_value == 4.75
    assert (
        energy.extra_state_attributes["sampled_at_utc"] == "2026-03-11T05:40:00+00:00"
    )

    assert power.available is True
    assert power.native_value == 7.68
    assert power.extra_state_attributes["sampled_at_utc"] == "2026-03-11T05:40:00+00:00"

    coord.battery_status_summary = {}
    assert energy.available is False
    assert power.available is False

    coord.battery_status_summary = {
        "site_available_energy_kwh": "bad",
        "site_available_power_kw": "bad",
    }
    assert energy.native_value is None
    assert power.native_value is None

    coord.last_update_success = False
    assert energy.available is False
    assert power.available is False


def test_battery_last_reported_sensor_states_and_attributes():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryLastReportedSensor
    from homeassistant.helpers.entity import EntityCategory

    payload = {
        "storages": [
            {
                "serial_number": "BAT-1",
                "name": "Battery One",
                "status": "normal",
                "last_report": 1_771_133_493,
            },
            {
                "serial_number": "BAT-2",
                "name": "Battery Two",
                "statusText": "Warning",
                "last_report": 1_771_137_093,
            },
            {
                "serial_number": "BAT-3",
                "name": "Battery Three",
            },
            {
                "serial_number": "BAT-4",
                "name": "Battery Four",
                "excluded": True,
                "last_report": 1_771_144_293,
            },
            {
                "serial_number": "BAT-5",
                "name": "Battery Five",
                "statusText": "Normal",
                "last_report": 1_771_144_293,
            },
        ]
    }
    coord = SimpleNamespace(
        site_id="site",
        battery_status_payload=payload,
        iter_battery_serials=lambda: ["BAT-FALLBACK"],
        battery_storage=lambda _serial: {
            "serial_number": "BAT-FALLBACK",
            "last_report": 1_771_144_293,
        },
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )

    sensor = EnphaseBatteryLastReportedSensor(coord)
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.entity_registry_enabled_default is False
    assert sensor.available is True
    assert sensor.native_value == datetime(2026, 2, 15, 8, 31, 33, tzinfo=timezone.utc)
    attrs = sensor.extra_state_attributes
    assert attrs["total_batteries"] == 4
    assert attrs["without_last_report_count"] == 1
    assert attrs["latest_reported_device"]["serial_number"] == "BAT-5"
    assert attrs["latest_reported_device"]["status"] == "Normal"


def test_ac_battery_storage_sensors_state_and_attributes():
    from types import SimpleNamespace

    from homeassistant.components.sensor import SensorStateClass

    from custom_components.enphase_ev.sensor import (
        EnphaseAcBatteryStorageChargeSensor,
        EnphaseAcBatteryStorageCycleCountSensor,
        EnphaseAcBatteryStorageLastReportedSensor,
        EnphaseAcBatteryStorageOperatingModeSensor,
        EnphaseAcBatteryStoragePowerSensor,
        EnphaseAcBatteryStorageStatusSensor,
    )

    snapshot = {
        "serial_number": "BAT-AC-1",
        "battery_id": "67890",
        "part_number": "ACB-Model",
        "phase": "Single Phase",
        "current_charge_pct": 48.4,
        "status_text": "Warning",
        "status_normalized": "warning",
        "sleep_state": "pending",
        "sleep_control_class": "cancel",
        "sleep_control_label": "Entering Sleep Mode",
        "power_w": 260.0,
        "operating_mode": "Charging",
        "cycle_count": 123,
        "last_reported": datetime(2026, 4, 9, 3, 15, tzinfo=timezone.utc),
    }
    coord = SimpleNamespace(
        site_id="site",
        last_update_success=True,
        ac_battery_storage=lambda _serial: snapshot,
        inventory_view=SimpleNamespace(
            type_device_info=lambda _key: None,
            has_type_for_entities=lambda _key: True,
        ),
    )

    charge = EnphaseAcBatteryStorageChargeSensor(coord, "BAT-AC-1")
    status = EnphaseAcBatteryStorageStatusSensor(coord, "BAT-AC-1")
    power = EnphaseAcBatteryStoragePowerSensor(coord, "BAT-AC-1")
    mode = EnphaseAcBatteryStorageOperatingModeSensor(coord, "BAT-AC-1")
    cycle = EnphaseAcBatteryStorageCycleCountSensor(coord, "BAT-AC-1")
    last_reported = EnphaseAcBatteryStorageLastReportedSensor(coord, "BAT-AC-1")

    assert charge.available is True
    assert charge.name == "BAT-AC-1"
    assert charge.native_value == 48.4
    assert charge.state_class == SensorStateClass.MEASUREMENT
    assert charge.extra_state_attributes["battery_id"] == "67890"

    assert status.native_value == "warning"
    assert status.extra_state_attributes["sleep_state"] == "pending"
    assert power.native_value == 260.0
    assert power.extra_state_attributes["operating_mode"] == "Charging"
    assert mode.native_value == "Charging"
    assert cycle.native_value == 123
    assert last_reported.available is True
    assert last_reported.native_value == datetime(
        2026, 4, 9, 3, 15, tzinfo=timezone.utc
    )


def test_ac_battery_storage_sensor_edge_branches():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import (
        EnphaseAcBatteryStorageChargeSensor,
        EnphaseAcBatteryStorageCycleCountSensor,
        EnphaseAcBatteryStorageOperatingModeSensor,
        EnphaseAcBatteryStoragePowerSensor,
        EnphaseAcBatteryStorageStatusSensor,
    )

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    snapshot = {
        "serial_number": BadStr(),
        "status_normalized": BadStr(),
        "operating_mode": BadStr(),
        "power_w": float("inf"),
        "cycle_count": BadStr(),
        "current_charge_pct": None,
    }
    coord = SimpleNamespace(
        site_id="site",
        last_update_success=False,
        ac_battery_storage=lambda _serial: snapshot,
        inventory_view=SimpleNamespace(
            type_device_info=lambda _key: None,
            has_type_for_entities=lambda _key: False,
        ),
    )

    charge = EnphaseAcBatteryStorageChargeSensor(coord, "BAT-AC-1")
    cycle = EnphaseAcBatteryStorageCycleCountSensor(coord, "BAT-AC-1")
    status = EnphaseAcBatteryStorageStatusSensor(coord, "BAT-AC-1")
    power = EnphaseAcBatteryStoragePowerSensor(coord, "BAT-AC-1")
    mode = EnphaseAcBatteryStorageOperatingModeSensor(coord, "BAT-AC-1")

    assert charge.available is False
    assert charge.native_value is None
    assert charge.name == "BAT-AC-1"
    assert charge._as_int(None) is None  # noqa: SLF001
    assert charge._as_int(BadStr()) is None  # noqa: SLF001
    assert power._as_float(BadStr()) is None  # noqa: SLF001
    assert cycle.native_value is None
    assert status.native_value is None
    assert power.native_value is None
    assert mode.native_value is None
    snapshot["status_normalized"] = None
    snapshot["operating_mode"] = None
    assert status.native_value is None
    assert mode.native_value is None
    assert mode.extra_state_attributes == {
        "status_text": snapshot.get("status_text"),
        "sleep_state": snapshot.get("sleep_state"),
    }
    assert power.device_info["name"] == "AC Battery"


def test_ac_battery_site_summary_sensors_state_and_attributes():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import (
        EnphaseAcBatteryLastReportedSensor,
        EnphaseAcBatteryOverallStatusSensor,
        EnphaseAcBatteryPowerSensor,
    )

    coord = SimpleNamespace(
        site_id="site",
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        last_failure_endpoint=None,
        payload_failure_kind=None,
        payload_using_stale=False,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
        ac_battery_aggregate_status="warning",
        ac_battery_status_summary={
            "aggregate_status": "warning",
            "battery_count": 1,
            "sleep_state": "pending",
            "selected_sleep_min_soc": 25,
            "sleep_state_map": {"BAT-AC-1": "pending"},
            "sleep_state_raw": {"BAT-AC-1": "cancel"},
            "worst_status": "warning",
            "power_w": 260.0,
            "latest_reported_utc": "2026-04-09T03:15:00+00:00",
            "power_map_w": {"BAT-AC-1": 260.0},
        },
        ac_battery_control_pending=True,
        ac_battery_selected_sleep_min_soc=25,
        ac_battery_sleep_state="pending",
        ac_battery_summary_sample_utc=datetime(2026, 4, 9, 3, 16, tzinfo=timezone.utc),
        iter_ac_battery_serials=lambda: ["BAT-AC-1"],
        ac_battery_storage=lambda _serial: {
            "serial_number": "BAT-AC-1",
            "status_text": "Warning",
            "sleep_state": "pending",
            "last_reported": datetime(2026, 4, 9, 3, 15, tzinfo=timezone.utc),
        },
        _ac_battery_last_command={"action": "sleep"},
        inventory_view=SimpleNamespace(
            type_device_info=lambda _key: None,
            has_type_for_entities=lambda _key: True,
        ),
    )

    overall = EnphaseAcBatteryOverallStatusSensor(coord)
    power = EnphaseAcBatteryPowerSensor(coord)
    last_reported = EnphaseAcBatteryLastReportedSensor(coord)

    assert overall.available is True
    assert overall.native_value == "warning"
    assert overall.extra_state_attributes["battery_count"] == 1
    assert power.available is True
    assert power.native_value == 260.0
    assert power.extra_state_attributes["sampled_at_utc"] == "2026-04-09T03:16:00+00:00"
    assert last_reported.available is True
    assert last_reported.native_value == datetime(
        2026, 4, 9, 3, 15, tzinfo=timezone.utc
    )
    attrs = last_reported.extra_state_attributes
    assert attrs["total_batteries"] == 1


def test_ac_battery_site_summary_sensor_edge_branches():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import (
        EnphaseAcBatteryLastReportedSensor,
        EnphaseAcBatteryOverallStatusSensor,
        EnphaseAcBatteryPowerSensor,
    )

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord = SimpleNamespace(
        site_id="site",
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        last_failure_endpoint=None,
        payload_failure_kind=None,
        payload_using_stale=False,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=False,
        ac_battery_aggregate_status=None,
        ac_battery_status_summary={"power_w": BadStr()},
        ac_battery_control_pending=False,
        ac_battery_selected_sleep_min_soc=None,
        ac_battery_sleep_state=None,
        ac_battery_summary_sample_utc=None,
        iter_ac_battery_serials=lambda: [],
        ac_battery_storage=lambda _serial: None,
        inventory_view=SimpleNamespace(
            type_device_info=lambda _key: None,
            has_type_for_entities=lambda _key: True,
        ),
    )

    overall = EnphaseAcBatteryOverallStatusSensor(coord)
    power = EnphaseAcBatteryPowerSensor(coord)
    last_reported = EnphaseAcBatteryLastReportedSensor(coord)

    assert overall.available is False
    assert power.available is False
    assert power.native_value is None
    coord.ac_battery_status_summary = {"power_w": None}
    assert power.native_value is None
    assert last_reported.available is False


def test_battery_last_reported_member_helpers_fallback() -> None:
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryLastReportedSensor,
        _battery_last_reported_members,
    )

    coord = SimpleNamespace(
        site_id="site",
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=False,
        battery_status_payload={},
        iter_battery_serials=lambda: ["BAT-1", "BAT-2"],
        battery_storage=lambda serial: (
            None if serial == "BAT-1" else {"serial_number": "BAT-2"}
        ),
    )

    assert _battery_last_reported_members(coord) == [
        {"serial_number": "BAT-1"},
        {"serial_number": "BAT-2"},
    ]
    assert EnphaseBatteryLastReportedSensor(coord).available is False
    coord.battery_storage = None
    assert _battery_last_reported_members(coord) == [
        {"serial_number": "BAT-1"},
        {"serial_number": "BAT-2"},
    ]


@pytest.mark.asyncio
async def test_sensor_platform_adds_and_prunes_ac_battery_entities(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
    from custom_components.enphase_ev.sensor import (
        EnphaseAcBatteryLastReportedSensor,
        EnphaseAcBatteryOverallStatusSensor,
        EnphaseAcBatteryStorageChargeSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._ac_battery_devices_payload = {}  # noqa: SLF001
    coord._ac_battery_data = {  # noqa: SLF001
        "BAT-AC-1": {
            "serial_number": "BAT-AC-1",
            "battery_id": "67890",
            "status_text": "Warning",
            "status_normalized": "warning",
            "current_charge_pct": 48.0,
            "cycle_count": 123,
            "power_w": 260.0,
            "operating_mode": "Charging",
            "last_reported": datetime(2026, 4, 9, 3, 15, tzinfo=timezone.utc),
        }
    }
    coord._ac_battery_order = ["BAT-AC-1"]  # noqa: SLF001
    coord._ac_battery_aggregate_status = "warning"  # noqa: SLF001
    coord._ac_battery_aggregate_status_details = {  # noqa: SLF001
        "battery_count": 1,
        "sleep_state_map": {"BAT-AC-1": "pending"},
        "sleep_state_raw": {"BAT-AC-1": "cancel"},
        "latest_reported_utc": "2026-04-09T03:15:00+00:00",
        "power_map_w": {"BAT-AC-1": 260.0},
    }
    coord._ac_battery_power_w = 260.0  # noqa: SLF001
    coord._ac_battery_summary_sample_utc = datetime(  # noqa: SLF001
        2026, 4, 9, 3, 16, tzinfo=timezone.utc
    )
    callbacks: list[object] = []
    added: list[list[object]] = []

    def capture_listener(callback, *, context=None):
        callbacks.append(callback)
        return lambda: None

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    coord.async_add_topology_listener = capture_listener  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    retired = ent_reg.async_get_or_create(
        "sensor",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_ac_battery_BAT-OLD_last_reported_at",
        config_entry=config_entry,
    )
    ent_reg.async_get_or_create(
        "sensor",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_ac_battery_overall_status",
        config_entry=config_entry,
    )
    ent_reg.async_get_or_create(
        "sensor",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_ac_battery__status",
        config_entry=config_entry,
    )
    ent_reg.async_get_or_create(
        "sensor",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_ac_battery_BAT-UNKNOWN_suffixless",
        config_entry=config_entry,
    )
    stale = ent_reg.async_get_or_create(
        "sensor",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_ac_battery_BAT-OLD_status",
        config_entry=config_entry,
    )
    current = ent_reg.async_get_or_create(
        "sensor",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_ac_battery_BAT-AC-1_charge_level",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, capture_add)

    flat_added = [entity for group in added for entity in group]
    assert any(
        isinstance(entity, EnphaseAcBatteryOverallStatusSensor) for entity in flat_added
    )
    assert any(
        isinstance(entity, EnphaseAcBatteryLastReportedSensor) for entity in flat_added
    )
    assert any(
        isinstance(entity, EnphaseAcBatteryStorageChargeSensor) for entity in flat_added
    )
    remove_spy.assert_any_call(retired.entity_id)
    remove_spy.assert_any_call(stale.entity_id)

    ent_reg.async_get_or_create(
        "sensor",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_ac_battery_BAT-AC-1_last_reported",
        config_entry=config_entry,
    )
    coord._ac_battery_data = {}  # noqa: SLF001
    coord._ac_battery_order = []  # noqa: SLF001
    callbacks[0]()

    remove_spy.assert_any_call(current.entity_id)


def test_battery_overall_sensors_unavailable_paths():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryOverallChargeSensor,
        EnphaseBatteryOverallStatusSensor,
    )

    coord = SimpleNamespace(
        site_id="site",
        battery_aggregate_charge_pct=None,
        battery_aggregate_status=None,
        battery_status_summary={},
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=False,
    )
    charge = EnphaseBatteryOverallChargeSensor(coord)
    status = EnphaseBatteryOverallStatusSensor(coord)
    assert charge.available is False
    assert status.available is False
    assert charge.native_value is None

    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    coord.last_update_success = True
    coord.battery_aggregate_charge_pct = BadFloat()
    assert charge.native_value is None


def test_battery_mode_sensor_states():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryModeSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_grid_mode="ImportExport",
        battery_mode_display="Import and Export",
        battery_charge_from_grid_allowed=True,
        battery_discharge_to_grid_allowed=True,
        battery_charge_from_grid_enabled=True,
        battery_charge_from_grid_schedule_enabled=True,
        battery_charge_from_grid_start_time=dt_time(2, 0),
        battery_charge_from_grid_end_time=dt_time(5, 0),
        battery_shutdown_level=15,
        battery_shutdown_level_min=10,
        battery_shutdown_level_max=25,
        battery_use_battery_for_self_consumption=True,
        _battery_hide_charge_from_grid=False,
        _battery_envoy_supports_vls=True,
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )
    sensor = EnphaseBatteryModeSensor(coord)
    assert sensor.available is True
    assert sensor.native_value == "Import and Export"
    assert sensor.icon == "mdi:battery"
    attrs = sensor.extra_state_attributes
    assert attrs["mode_raw"] == "ImportExport"
    assert attrs["charge_from_grid_allowed"] is True
    assert attrs["discharge_to_grid_allowed"] is True
    assert attrs["shutdown_level"] == 15
    assert attrs["use_battery_for_self_consumption"] is True

    coord.battery_grid_mode = None
    coord.battery_mode_display = None
    assert sensor.available is False


def test_battery_mode_sensor_unavailable_when_coordinator_unavailable():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryModeSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_grid_mode="ImportExport",
        battery_mode_display="Import and Export",
        battery_charge_from_grid_allowed=True,
        battery_discharge_to_grid_allowed=True,
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=False,
    )

    assert EnphaseBatteryModeSensor(coord).available is False


def test_battery_mode_sensor_falls_back_to_status_payload():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryModeSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_grid_mode=None,
        battery_mode_display=None,
        battery_charge_from_grid_allowed=None,
        battery_discharge_to_grid_allowed=None,
        battery_charge_from_grid_enabled=False,
        battery_charge_from_grid_schedule_enabled=True,
        battery_charge_from_grid_start_time=dt_time(3, 0),
        battery_charge_from_grid_end_time=dt_time(16, 0),
        battery_shutdown_level=None,
        battery_shutdown_level_min=5,
        battery_shutdown_level_max=100,
        battery_use_battery_for_self_consumption=None,
        battery_status_payload={
            "storages": [
                {
                    "battery_mode": "Self-Consumption",
                }
            ]
        },
        _battery_hide_charge_from_grid=True,
        _battery_envoy_supports_vls=True,
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )

    sensor = EnphaseBatteryModeSensor(coord)

    assert sensor.available is True
    assert sensor.native_value == "Self-Consumption"
    assert sensor.extra_state_attributes["mode_raw"] == "Self-Consumption"


def test_battery_mode_sensor_skips_invalid_status_payload_entries():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseBatteryModeSensor

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord = SimpleNamespace(
        site_id="site",
        battery_grid_mode=None,
        battery_mode_display=None,
        battery_charge_from_grid_allowed=None,
        battery_discharge_to_grid_allowed=None,
        battery_charge_from_grid_enabled=False,
        battery_charge_from_grid_schedule_enabled=False,
        battery_charge_from_grid_start_time=None,
        battery_charge_from_grid_end_time=None,
        battery_shutdown_level=None,
        battery_shutdown_level_min=5,
        battery_shutdown_level_max=100,
        battery_use_battery_for_self_consumption=None,
        battery_status_payload={
            "storages": [
                None,
                {},
                {"battery_mode": BadStr()},
                {"battery_mode": "Backup"},
            ]
        },
        _battery_hide_charge_from_grid=False,
        _battery_envoy_supports_vls=True,
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
    )

    sensor = EnphaseBatteryModeSensor(coord)

    assert sensor.available is True
    assert sensor.native_value == "Backup"


def test_grid_control_status_sensor_states_and_attributes():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseGridControlStatusSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_has_encharge=True,
        has_type=lambda key: key in ("envoy", "enpower"),
        grid_control_supported=True,
        grid_toggle_pending=False,
        grid_toggle_allowed=True,
        grid_toggle_blocked_reasons=[],
        grid_control_disable=False,
        grid_control_active_download=False,
        grid_control_sunlight_backup_system_check=False,
        grid_control_grid_outage_check=False,
        grid_control_user_initiated_toggle=False,
        last_success_utc=None,
        last_update_success=True,
    )
    sensor = EnphaseGridControlStatusSensor(coord)
    assert sensor.available is True
    assert sensor.entity_category is None
    assert sensor.native_value == "ready"
    assert sensor.icon == "mdi:check-circle"

    coord.last_success_utc = datetime.now(timezone.utc)
    coord.last_update_success = False
    assert sensor.available is True

    coord.grid_toggle_allowed = False
    coord.grid_toggle_blocked_reasons = ["active_download"]
    coord.grid_control_active_download = True
    assert sensor.native_value == "blocked"
    assert sensor.icon == "mdi:alert-circle"

    coord.grid_toggle_pending = True
    coord.grid_control_user_initiated_toggle = True
    assert sensor.native_value == "pending"
    assert sensor.icon == "mdi:progress-clock"

    coord.grid_toggle_pending = False
    coord.grid_toggle_allowed = None
    assert sensor.native_value is None
    assert sensor.icon == "mdi:transmission-tower"

    attrs = sensor.extra_state_attributes
    assert attrs["blocked_reasons"] == ["active_download"]
    assert attrs["active_download"] is True
    assert attrs["user_initiated_grid_toggle"] is True
    assert "grid_control_supported" not in attrs
    assert "grid_toggle_allowed" not in attrs

    coord.grid_control_supported = False
    assert sensor.native_value is None
    assert sensor.icon == "mdi:transmission-tower"


def test_grid_control_status_sensor_unavailable_when_no_system_or_gateway_type():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseGridControlStatusSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_has_encharge=True,
        has_type=lambda _key: False,
        grid_control_supported=True,
        grid_toggle_pending=False,
        grid_toggle_allowed=True,
        grid_toggle_blocked_reasons=[],
        grid_control_disable=False,
        grid_control_active_download=False,
        grid_control_sunlight_backup_system_check=False,
        grid_control_grid_outage_check=False,
        grid_control_user_initiated_toggle=False,
        last_success_utc=None,
        last_update_success=True,
    )
    assert EnphaseGridControlStatusSensor(coord).available is False


def test_grid_control_status_sensor_unavailable_when_site_not_battery_capable():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseGridControlStatusSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_has_encharge=False,
        battery_has_enpower=False,
        has_type=lambda key: key == "envoy",
        grid_control_supported=True,
        grid_toggle_pending=False,
        grid_toggle_allowed=True,
        grid_toggle_blocked_reasons=[],
        grid_control_disable=False,
        grid_control_active_download=False,
        grid_control_sunlight_backup_system_check=False,
        grid_control_grid_outage_check=False,
        grid_control_user_initiated_toggle=False,
        last_success_utc=None,
        last_update_success=True,
    )
    assert EnphaseGridControlStatusSensor(coord).available is False


def test_grid_control_status_sensor_unavailable_when_battery_unknown_gateway_only():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseGridControlStatusSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_has_encharge=None,
        battery_has_enpower=None,
        has_type=lambda key: key == "envoy",
        grid_control_supported=True,
        grid_toggle_pending=False,
        grid_toggle_allowed=True,
        grid_toggle_blocked_reasons=[],
        grid_control_disable=False,
        grid_control_active_download=False,
        grid_control_sunlight_backup_system_check=False,
        grid_control_grid_outage_check=False,
        grid_control_user_initiated_toggle=False,
        last_success_utc=None,
        last_update_success=True,
    )

    assert EnphaseGridControlStatusSensor(coord).available is False


def test_grid_control_status_sensor_available_when_inventory_readiness_unknown():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseGridControlStatusSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_has_encharge=None,
        battery_has_enpower=None,
        has_type=lambda _key: False,
        has_type_for_entities=lambda key: key in ("envoy", "enpower", "encharge"),
        grid_control_supported=True,
        grid_toggle_pending=False,
        grid_toggle_allowed=True,
        grid_toggle_blocked_reasons=[],
        grid_control_disable=False,
        grid_control_active_download=False,
        grid_control_sunlight_backup_system_check=False,
        grid_control_grid_outage_check=False,
        grid_control_user_initiated_toggle=False,
        last_success_utc=None,
        last_update_success=True,
    )

    sensor = EnphaseGridControlStatusSensor(coord)
    assert sensor.available is True
    assert sensor.native_value == "ready"


def test_grid_mode_sensor_states_and_attributes():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseGridModeSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_has_encharge=True,
        has_type=lambda key: key in ("envoy", "enpower"),
        grid_mode="on_grid",
        grid_mode_raw_states=["is_grid_outage:false"],
        grid_outage_context_supported=True,
        grid_outage_is_grid_outage=False,
        grid_outage_show_grid_connect=True,
        grid_outage_has_battery=True,
        grid_outage_is_sunlight_backup=False,
        grid_control_supported=True,
        grid_toggle_allowed=True,
        last_success_utc=None,
        last_update_success=True,
    )
    sensor = EnphaseGridModeSensor(coord)
    assert sensor.available is True
    assert sensor.native_value == "on_grid"
    attrs = sensor.extra_state_attributes
    assert attrs["source"] == "grid_outage_context"
    assert attrs["raw_states"] == ["is_grid_outage:false"]
    assert attrs["grid_outage_context_supported"] is True
    assert attrs["is_grid_outage"] is False
    assert attrs["show_grid_connect"] is True
    assert attrs["has_battery"] is True
    assert attrs["is_sunlight_backup"] is False
    assert attrs["grid_control_supported"] is True
    assert attrs["grid_toggle_allowed"] is True

    coord.grid_mode = "off_grid"
    coord.grid_mode_raw_states = ["is_grid_outage:true"]
    coord.grid_outage_is_grid_outage = True
    assert sensor.native_value == "off_grid"

    coord.grid_mode = None
    coord.grid_mode_raw_states = []
    coord.grid_outage_is_grid_outage = None
    assert sensor.native_value == "unknown"
    coord.last_success_utc = datetime(2026, 2, 15, 5, 31, 33, tzinfo=timezone.utc)
    coord.last_update_success = False
    assert sensor.available is True


def test_grid_mode_sensor_unavailable_without_supported_types():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseGridModeSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_has_encharge=True,
        has_type=lambda _key: False,
        grid_mode="on_grid",
        grid_mode_raw_states=["is_grid_outage:false"],
        grid_outage_context_supported=True,
        grid_outage_is_grid_outage=False,
        grid_outage_show_grid_connect=True,
        grid_outage_has_battery=True,
        grid_outage_is_sunlight_backup=False,
        grid_control_supported=True,
        grid_toggle_allowed=True,
        last_success_utc=None,
        last_update_success=True,
    )
    assert EnphaseGridModeSensor(coord).available is False


def test_grid_mode_sensor_unavailable_when_site_not_battery_capable():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseGridModeSensor

    coord = SimpleNamespace(
        site_id="site",
        battery_has_encharge=False,
        battery_has_enpower=False,
        has_type=lambda key: key in ("envoy", "enpower"),
        grid_mode="on_grid",
        grid_mode_raw_states=["is_grid_outage:false"],
        grid_outage_context_supported=True,
        grid_outage_is_grid_outage=False,
        grid_outage_show_grid_connect=True,
        grid_outage_has_battery=True,
        grid_outage_is_sunlight_backup=False,
        grid_control_supported=True,
        grid_toggle_allowed=True,
        last_success_utc=None,
        last_update_success=True,
    )
    assert EnphaseGridModeSensor(coord).available is False


def test_microinverter_inventory_zero_total_paths():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import (
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterReportingCountSensor,
    )

    bucket = {
        "devices": "invalid",
        "status_counts": {"total": 0, "unknown": 0},
        "reporting_inverters": 0,
        "not_reporting_inverters": 0,
        "unknown_inverters": 0,
        "connectivity_state": "unknown",
    }
    coord = SimpleNamespace(
        site_id="site",
        include_inverters=True,
        last_success_utc=datetime(2026, 2, 15, 5, 31, 33, tzinfo=timezone.utc),
        last_update_success=False,
        _devices_inventory_ready=False,
        type_bucket=lambda _key: bucket,
        has_type=lambda key: key == "microinverter",
        has_type_for_entities=lambda key: key == "microinverter",
    )

    connectivity = EnphaseMicroinverterConnectivityStatusSensor(coord)
    reporting = EnphaseMicroinverterReportingCountSensor(coord)
    assert connectivity.available is True
    assert reporting.available is True


def test_system_profile_status_sensor_states():
    from types import SimpleNamespace

    from custom_components.enphase_ev.sensor import EnphaseSystemProfileStatusSensor

    coord = SimpleNamespace(
        site_id="site",
        storm_alert_active=None,
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_source=None,
        last_failure_response=None,
        backoff_ends_utc=None,
        latency_ms=None,
        last_update_success=True,
        battery_controls_available=True,
        battery_profile_pending=False,
        battery_profile="self-consumption",
        battery_live_profile=None,
        battery_effective_profile="self-consumption",
        battery_effective_profile_display="Self-Consumption",
        battery_effective_backup_percentage=20,
        battery_effective_operation_mode_sub_type=None,
        battery_pending_profile=None,
        battery_pending_backup_percentage=None,
        battery_pending_operation_mode_sub_type=None,
        battery_pending_requested_at=None,
        battery_selected_profile="self-consumption",
        battery_profile_display="Self-Consumption",
        battery_selected_backup_percentage=20,
        battery_selected_operation_mode_sub_type=None,
        battery_profile_option_keys=[
            "self-consumption",
            "cost_savings",
            "ai_optimisation",
        ],
        battery_profile_option_labels={
            "self-consumption": "Self-Consumption",
            "cost_savings": "Savings",
            "ai_optimisation": "AI Optimisation",
        },
        battery_supports_mqtt=True,
        battery_profile_polling_interval=60,
        battery_cfg_control_show=True,
        battery_cfg_control_enabled=True,
        battery_cfg_control_schedule_supported=True,
        battery_cfg_control_force_schedule_supported=False,
        battery_cfg_control_locked=False,
        battery_cfg_control_show_day_schedule=True,
        battery_cfg_control_force_schedule_opted=True,
        battery_dtg_control={"show": True, "locked": False},
        battery_cfg_control={"show": True, "locked": False},
        battery_rbd_control={"show": True, "locked": False},
        battery_system_task=False,
        battery_show_production=True,
        battery_show_consumption=True,
        battery_show_storm_guard=True,
        battery_show_battery_backup_percentage=True,
        battery_has_encharge=True,
        battery_has_enpower=False,
        battery_is_charging_modes_enabled=True,
        battery_country_code="US",
        battery_region="CA",
        battery_locale="en-US",
        battery_timezone="America/Los_Angeles",
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_site_status_code="normal",
        battery_site_status_text="Normal",
        battery_site_status_severity="info",
        battery_feature_details={"HEMS_EV_Custom_Schedule": True},
        battery_profile_evse_device={"uuid": "evse-1"},
    )
    sensor = EnphaseSystemProfileStatusSensor(coord)
    assert sensor.available is True
    assert sensor.native_value == "Self-Consumption"
    assert sensor.device_info["identifiers"] == {("enphase_ev", "type:site:envoy")}

    coord.battery_profile_pending = True
    coord.battery_pending_profile = "ai_optimisation"
    coord.battery_pending_backup_percentage = 25
    coord.battery_pending_operation_mode_sub_type = "prioritize-energy"
    coord.battery_pending_requested_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    coord.battery_selected_profile = "ai_optimisation"
    coord.battery_profile_display = "AI Optimisation"
    coord.battery_selected_backup_percentage = 25
    coord.battery_selected_operation_mode_sub_type = "prioritize-energy"
    assert sensor.native_value == "AI Optimisation"
    attrs = sensor.extra_state_attributes
    assert attrs["pending"] is True
    assert attrs["effective_profile"] == "self-consumption"
    assert attrs["configured_profile"] == "self-consumption"
    assert attrs["live_profile"] is None
    assert attrs["live_profile_label"] is None
    assert attrs["requested_profile"] == "ai_optimisation"
    assert attrs["requested_profile_label"] == "AI Optimisation"
    assert attrs["supports_mqtt"] is True
    assert attrs["cfg_control_show"] is True
    assert attrs["cfg_control_locked"] is False
    assert attrs["cfg_control_show_day_schedule"] is True
    assert attrs["cfg_control_force_schedule_opted"] is True
    assert attrs["cfg_control"]["locked"] is False
    assert attrs["rbd_control"]["show"] is True
    assert attrs["battery_system_task"] is False
    assert attrs["site_country_code"] == "US"
    assert attrs["feature_details"] == {"HEMS_EV_Custom_Schedule": True}
    assert attrs["evse_profile"]["uuid"] == "evse-1"

    coord.battery_profile_pending = False
    coord.battery_live_profile = "backup_only"
    coord.battery_effective_profile = "backup_only"
    coord.battery_effective_profile_display = "Full Backup"
    coord._battery_live_profile_label = "Full Backup"
    assert sensor.native_value == "Full Backup"
    attrs = sensor.extra_state_attributes
    assert attrs["effective_profile"] == "backup_only"
    assert attrs["configured_profile"] == "self-consumption"
    assert attrs["live_profile"] == "backup_only"
    assert attrs["live_profile_label"] == "Full Backup"

    coord.last_update_success = False
    assert sensor.available is False


def test_last_session_sensor_tracks_session_and_persists(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.components.sensor import SensorStateClass
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    start = datetime(2025, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "session_kwh": 4.25,
            "session_start": int(start.timestamp()),
            "session_end": int(end.timestamp()),
            "charging": False,
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.state_class == SensorStateClass.TOTAL
    monkeypatch.setattr(dt_util, "utcnow", lambda: end)
    assert sensor.native_value == pytest.approx(4.25)
    attrs = sensor.extra_state_attributes
    assert attrs["session_duration_min"] == 120

    coord.data[sn]["session_kwh"] = None
    coord.data[sn]["session_start"] = None
    coord.data[sn]["session_end"] = None
    assert sensor.native_value == pytest.approx(4.25)


def test_last_session_sensor_uses_history_when_available(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "energy_today_sessions": [
                {
                    "session_id": "a1",
                    "start": "2025-10-01T01:00:00+00:00",
                    "end": "2025-10-01T02:00:00+00:00",
                    "energy_kwh_total": 6.5,
                    "active_charge_time_s": 3600,
                    "avg_cost_per_kwh": 0.25,
                    "cost_calculated": True,
                    "session_cost_state": "calculated",
                    "manual_override": False,
                    "charge_profile_stack_level": 2,
                }
            ],
            "energy_today_sessions_kwh": 6.5,
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(6.5)
    attrs = sensor.extra_state_attributes
    assert attrs["energy_consumed_kwh"] == pytest.approx(6.5)
    assert attrs["energy_consumed_wh"] == pytest.approx(6500.0)
    assert "session_id" not in attrs
    assert attrs["session_started_at"] == "2025-10-01T01:00:00+00:00"
    assert attrs["session_ended_at"] == "2025-10-01T02:00:00+00:00"
    assert attrs["active_charge_time_s"] == 3600
    assert attrs["avg_cost_per_kwh"] == pytest.approx(0.25)
    assert attrs["cost_calculated"] is True
    assert attrs["session_cost_state"] == "calculated"
    assert attrs["manual_override"] is False
    assert attrs["charge_profile_stack_level"] == 2


def test_last_session_prefers_history_when_idle(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging": False,
            "session_kwh": 1.25,
            "session_start": "2025-10-01T03:00:00+00:00",
            "session_end": "2025-10-01T03:30:00+00:00",
            "energy_today_sessions": [
                {
                    "session_id": "old",
                    "start": "2025-10-01T01:00:00+00:00",
                    "end": "2025-10-01T02:00:00+00:00",
                    "energy_kwh_total": 2.0,
                },
                {
                    "session_id": "history-123",
                    "start": "2025-10-01T03:00:00+00:00",
                    "end": "2025-10-01T04:00:00+00:00",
                    "energy_kwh_total": 6.5,
                    "session_cost": 2.2,
                    "avg_cost_per_kwh": 0.34,
                },
            ],
            "energy_today_sessions_kwh": 8.5,
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(6.5)
    attrs = sensor.extra_state_attributes
    assert attrs["energy_consumed_kwh"] == pytest.approx(6.5)
    assert "session_id" not in attrs
    assert attrs["session_cost"] == pytest.approx(2.2)
    assert attrs["avg_cost_per_kwh"] == pytest.approx(0.34)


def test_last_session_merges_history_metadata(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging": True,
            "session_kwh": 1.25,
            "session_start": "2025-10-01T03:00:00+00:00",
            "session_end": None,
            "energy_today_sessions": [
                {
                    "session_id": "old",
                    "start": "2025-10-01T01:00:00+00:00",
                    "end": "2025-10-01T02:00:00+00:00",
                    "energy_kwh_total": 2.0,
                },
                {
                    "session_id": "history-123",
                    "start": "2025-10-01T03:00:00+00:00",
                    "end": "2025-10-01T04:00:00+00:00",
                    "energy_kwh_total": 4.5,
                    "session_cost": 1.2,
                    "avg_cost_per_kwh": 0.24,
                    "manual_override": True,
                    "session_cost_state": "COST_CALCULATED",
                    "charge_profile_stack_level": 3,
                },
            ],
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(1.25)
    attrs = sensor.extra_state_attributes
    assert attrs["energy_consumed_kwh"] == pytest.approx(1.25)
    assert "session_id" not in attrs
    assert attrs["session_started_at"] == "2025-10-01T03:00:00+00:00"
    assert attrs["session_ended_at"] == "2025-10-01T04:00:00+00:00"
    assert attrs["session_cost"] == pytest.approx(1.2)
    assert attrs["avg_cost_per_kwh"] == pytest.approx(0.24)
    assert attrs["manual_override"] is True
    assert attrs["session_cost_state"] == "COST_CALCULATED"
    assert attrs["charge_profile_stack_level"] == 3


def test_last_session_attributes_convert_units_and_duration(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.const import UnitOfLength
    from homeassistant.util import dt as dt_util
    from homeassistant.util.unit_conversion import DistanceConverter

    sn = RANDOM_SERIAL
    plug_in = "2025-10-24T20:00:00.000Z[UTC]"
    plug_out = "2025-10-24T22:30:15.000Z[UTC]"
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "session_energy_wh": 3561.53,
            "session_plug_in_at": plug_in,
            "session_plug_out_at": plug_out,
            "session_start": datetime(
                2025, 10, 24, 20, 0, 0, tzinfo=timezone.utc
            ).timestamp(),
            "session_end": datetime(
                2025, 10, 24, 22, 30, 15, tzinfo=timezone.utc
            ).timestamp(),
            "session_miles": 14.35368,
            "session_cost": 4.75,
            "session_charge_level": 32,
        },
    )

    base_utc = datetime(2025, 10, 24, 20, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: base_utc)
    monkeypatch.setattr(
        dt_util,
        "as_local",
        lambda dt: dt.replace(tzinfo=timezone.utc),  # type: ignore[override]
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)

    class DummyUnits:
        length_unit = UnitOfLength.KILOMETERS

    class DummyConfig:
        units = DummyUnits()

    class DummyHass:
        config = DummyConfig()

    sensor.hass = DummyHass()  # type: ignore[assignment]

    assert sensor.native_value == round(3561.53 / 1000.0, 2)
    attrs = sensor.extra_state_attributes
    assert attrs["plugged_in_at"] == "2025-10-24T20:00:00+00:00"
    assert attrs["plugged_out_at"] == "2025-10-24T22:30:15+00:00"
    assert attrs["energy_consumed_kwh"] == 3.56
    assert attrs["energy_consumed_wh"] == pytest.approx(3561.53)
    assert attrs["session_started_at"] == "2025-10-24T20:00:00+00:00"
    assert attrs["session_ended_at"] == "2025-10-24T22:30:15+00:00"
    expected_km = round(
        DistanceConverter.convert(
            14.35368, UnitOfLength.MILES, UnitOfLength.KILOMETERS
        ),
        3,
    )
    assert attrs["range_added"] == expected_km
    assert attrs["session_cost"] == 4.75
    assert attrs["session_charge_level"] == 32
    assert attrs["session_duration_min"] == 150


def test_last_session_restore_data_roundtrip():
    from custom_components.enphase_ev.sensor import _LastSessionRestoreData

    restored = _LastSessionRestoreData.from_dict(
        {
            "last_session_kwh": "2.5",
            "last_session_wh": "2500",
            "last_session_start": "100",
            "last_session_end": "200",
            "session_key": "abc",
            "last_duration_min": "45",
        }
    )
    assert restored.last_session_kwh == pytest.approx(2.5)
    assert restored.last_session_wh == pytest.approx(2500)
    assert restored.last_session_start == pytest.approx(100)
    assert restored.last_session_end == pytest.approx(200)
    assert restored.session_key == "abc"
    assert restored.last_duration_min == 45

    empty = _LastSessionRestoreData.from_dict(None)
    assert empty.last_session_kwh is None
    assert empty.session_key is None


def test_last_reported_sensor_exposes_reporting_interval(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseLastReportedSensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "last_reported_at": "2025-09-07T11:38:31Z[UTC]",
            "reporting_interval": " 300 ",
            "connection": "wifi",
            "ip_address": "192.0.2.10",
            "mac_address": "00:11:22:33:44:55",
            "network_interface_count": "2",
            "operating_v": "240",
            "firmware_version": "25.37.1.14",
            "system_version": "25.37.1.14",
            "application_version": "25.37.1.5",
            "sw_version": "25.37.1.14",
            "hw_version": "2.0.713.0",
            "processor_board_version": "2.0.713.0",
            "power_board_version": "25.28.9.0",
            "kernel_version": "6.6.23",
            "bootloader_version": "2024.04",
            "default_route": "interface=mlan0",
            "wifi_config": "status=connected",
            "cellular_config": "status=disconnected",
            "warranty_start_date": "2025-01-01T00:00:00Z[UTC]",
            "warranty_due_date": "2030-01-01T00:00:00Z[UTC]",
            "warranty_period_years": "5",
            "created_at": "2025-01-01T00:00:00Z[UTC]",
            "breaker_rating": "48",
            "rated_current": "32",
            "grid_type": "2",
            "phase_count": "1",
            "commissioning_status": "1",
            "is_connected": "true",
            "is_locally_connected": 0,
            "ho_control": True,
            "gateway_connection_count": "2",
            "gateway_connected_count": "1",
            "gateway_last_connection_at": 1_714_550_600,
            "gateway_connectivity_details": [
                {
                    "status": "0",
                    "failure_reason": "0",
                    "last_connection_at": 1_714_550_000,
                },
                {
                    "status": "1",
                    "failure_reason": "7",
                    "last_connection_at": 1_714_550_600,
                },
                "bad",
            ],
            "functional_validation_state": "1",
            "functional_validation_updated_at": 1_714_550_000,
            "charger_timezone": "Region/City",
        },
    )
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)

    sensor = EnphaseLastReportedSensor(coord, sn)
    assert sensor.entity_registry_enabled_default is True
    assert sensor.native_value is not None
    assert sensor.available is True

    attrs = sensor.extra_state_attributes
    assert attrs["reporting_interval"] == 300
    assert attrs["connection"] == "wifi"
    assert attrs["ip_address"] == "192.0.2.10"
    assert attrs["mac_address"] == "00:11:22:33:44:55"
    assert attrs["network_interface_count"] == 2
    assert attrs["operating_voltage"] == 240
    assert attrs["is_connected"] is True
    assert attrs["is_locally_connected"] is False
    assert attrs["gateway_last_connection_at"] is not None
    assert attrs["gateway_connectivity_details"] == [
        {
            "status": 0,
            "failure_reason": 0,
            "last_connection_at": "2024-05-01T07:53:20+00:00",
        },
        {
            "status": 1,
            "failure_reason": 7,
            "last_connection_at": "2024-05-01T08:03:20+00:00",
        },
    ]
    assert attrs["functional_validation_updated_at"] is not None

    coord.data[sn]["reporting_interval"] = 150
    assert sensor.extra_state_attributes["reporting_interval"] == 150

    coord.data[sn]["reporting_interval"] = "not-int"
    assert sensor.extra_state_attributes["reporting_interval"] is None


def test_last_reported_sensor_handles_missing_and_invalid_values():
    from custom_components.enphase_ev.sensor import EnphaseLastReportedSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "last_reported_at": None,
        },
    )

    sensor = EnphaseLastReportedSensor(coord, sn)
    assert sensor.native_value is None
    assert sensor.available is False

    coord.data[sn]["last_reported_at"] = "not-a-timestamp"
    assert sensor.native_value is None
    assert sensor.available is False


def test_last_reported_sensor_attribute_edge_cases(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseLastReportedSensor
    from homeassistant.util import dt as dt_util

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "last_reported_at": "2025-09-07T11:38:31Z[UTC]",
            "reporting_interval": None,
            "is_connected": "off",
            "is_locally_connected": None,
            "created_at": None,
            "warranty_start_date": {},
            "warranty_due_date": "2025-01-01T00:00:00",
            "functional_validation_updated_at": " ",
            "wifi_config": BadStr(),
        },
    )
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)

    sensor = EnphaseLastReportedSensor(coord, sn)
    attrs = sensor.extra_state_attributes
    assert attrs["reporting_interval"] is None
    assert attrs["is_connected"] is False
    assert attrs["is_locally_connected"] is None
    assert attrs["created_at"] is None
    assert attrs["warranty_start_date"] is None
    assert attrs["warranty_due_date"] == "2025-01-01T00:00:00+00:00"
    assert attrs["functional_validation_updated_at"] is None
    assert attrs["wifi_config"] is None

    coord.data[sn]["created_at"] = "invalid"
    assert sensor.extra_state_attributes["created_at"] is None
    coord.data[sn]["is_connected"] = "maybe"
    assert sensor.extra_state_attributes["is_connected"] is None


def test_power_sensor_caps_max_output():
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 100.0,
            "last_reported_at": "2025-09-09T08:00:00Z",
            "charging": True,
        },
    )
    sensor = EnphasePowerSensor(coord, sn)
    assert sensor.native_value == 0

    coord.data[sn]["lifetime_kwh"] = 110.0
    coord.data[sn]["last_reported_at"] = "2025-09-09T08:05:00Z"
    assert sensor.native_value == 19200


def test_power_sensor_fallback_window_when_timestamp_missing(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphasePowerSensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 1.0,
            "charging": True,
        },
    )
    sensor = EnphasePowerSensor(coord, sn)

    anchor = datetime(2025, 9, 9, 7, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: anchor)
    monkeypatch.setattr(dt_util, "now", lambda: anchor)
    assert sensor.native_value == 0

    monkeypatch.setattr(dt_util, "utcnow", lambda: anchor + timedelta(minutes=5))
    monkeypatch.setattr(dt_util, "now", lambda: anchor + timedelta(minutes=5))
    coord.data[sn]["lifetime_kwh"] = 1.5
    coord.data[sn].pop("last_reported_at", None)
    assert sensor.native_value == 6000


def test_lifetime_energy_accepts_resets(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseLifetimeEnergySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    payload = {"sn": sn, "name": "Garage EV", "lifetime_kwh": 200.5}
    coord = _mk_coord_with(sn, payload)

    sensor = EnphaseLifetimeEnergySensor(coord, sn)
    assert sensor.native_value == pytest.approx(200.5)

    coord.data[sn]["lifetime_kwh"] = 200.75
    assert sensor.native_value == pytest.approx(200.75)

    coord.data[sn]["lifetime_kwh"] = 200.74
    assert sensor.native_value == pytest.approx(200.75)

    reset_time = datetime(2025, 9, 9, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dt_util, "utcnow", lambda: reset_time)
    coord.data[sn]["lifetime_kwh"] = 0
    assert sensor.native_value == pytest.approx(0)
    attrs = sensor.extra_state_attributes
    assert attrs["last_reset_value"] == pytest.approx(0)
    assert attrs["last_reset_at"] is not None

    coord.data[sn]["lifetime_kwh"] = 0.4
    assert sensor.native_value == pytest.approx(0.4)

    coord.data[sn]["lifetime_kwh"] = 0.38
    assert sensor.native_value == pytest.approx(0.4)

    coord.data[sn]["lifetime_kwh"] = 0.9
    assert sensor.native_value == pytest.approx(0.9)


def test_lifetime_energy_falls_back_to_evse_timeseries():
    from custom_components.enphase_ev.sensor import EnphaseLifetimeEnergySensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": None,
            "evse_lifetime_energy_kwh": 45.6,
        },
    )

    sensor = EnphaseLifetimeEnergySensor(coord, sn)

    assert sensor.native_value == pytest.approx(45.6)


def test_lifetime_energy_exposes_sampled_at_utc():
    from custom_components.enphase_ev.sensor import EnphaseLifetimeEnergySensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 45.6,
            "sampled_at_utc": "2026-03-11T05:40:00+00:00",
        },
    )

    sensor = EnphaseLifetimeEnergySensor(coord, sn)

    assert (
        sensor.extra_state_attributes["sampled_at_utc"] == "2026-03-11T05:40:00+00:00"
    )


def test_lifetime_energy_ignores_bad_evse_timeseries_fallback():
    from custom_components.enphase_ev.sensor import EnphaseLifetimeEnergySensor

    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": None,
            "evse_lifetime_energy_kwh": BadFloat(),
        },
    )

    sensor = EnphaseLifetimeEnergySensor(coord, sn)

    assert sensor.native_value is None


def test_last_session_sensor_ignores_evse_daily_fallback():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "evse_daily_energy_kwh": 7.0,
            "charging": False,
            "energy_today_sessions": [],
            "session_kwh": None,
            "session_start": None,
            "session_end": None,
        },
    )

    sensor = EnphaseEnergyTodaySensor(coord, sn)

    assert sensor.native_value is None


def test_status_sensor_exposes_attributes(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseStatusSensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "status": "CONNECTED",
            "faulted": True,
            "commissioned": False,
            "suspended_by_evse": True,
            "offline_since": "2025-01-02T03:04:05Z",
        },
    )
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    sensor = EnphaseStatusSensor(coord, sn)
    assert sensor.native_value == "Connected"
    coord.data[sn]["status"] = "NORMAL"
    assert sensor.native_value == "Normal"
    coord.data[sn]["status"] = "NOT_READY"
    assert sensor.native_value == "Not Ready"
    coord.data[sn]["status"] = "SUSPENDED_EVSE"
    assert sensor.native_value == "Suspended EVSE"
    coord.data[sn]["status"] = "AC/DC"
    assert sensor.native_value == "AC/DC"
    attrs = sensor.extra_state_attributes
    assert attrs["status_raw"] == "AC/DC"
    assert attrs["commissioned"] is False
    assert attrs["charger_problem"] is True
    assert attrs["suspended_by_evse"] is True
    assert attrs["offline_since"] == "2025-01-02T03:04:05+00:00"


def test_connector_status_reports_reason_attribute():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "AVAILABLE",
            "connector_reason": "INSUFFICIENT_SOLAR",
            "connector_status_info": "  see manual ",
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": "INSUFFICIENT_SOLAR",
        "connector_status_info": "see manual",
    }


def test_connector_status_reason_absent_returns_empty_attributes():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "AVAILABLE",
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": None,
        "connector_status_info": None,
    }


def test_connector_status_reason_numeric_gets_stringified():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "SUSPENDED",
            "connector_reason": 123,
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": "123",
        "connector_status_info": None,
    }


def test_connector_status_reason_handles_non_string_value():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    sentinel = BadStr()
    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "FAULTED",
            "connector_reason": sentinel,
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert sensor.extra_state_attributes == {
        "status_reason": sentinel,
        "connector_status_info": None,
    }


def test_connector_status_reports_suspended_by_evse():
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "connector_status": "SUSPENDED_EVSE",
            "suspended_by_evse": True,
        },
    )
    sensor = EnphaseConnectorStatusSensor(coord, sn)
    assert "suspended_by_evse" not in sensor.extra_state_attributes

    class BadBool:
        def __bool__(self):
            raise ValueError("boom")

    coord.data[sn]["suspended_by_evse"] = BadBool()
    assert "suspended_by_evse" not in sensor.extra_state_attributes


def test_charge_mode_sensor_attributes():
    from custom_components.enphase_ev.sensor import EnphaseChargeModeSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charge_mode": "IMMEDIATE",
            "charge_mode_pref": "SCHEDULED_CHARGING",
            "charge_mode_supported": True,
            "schedule_status": 1,
            "schedule_type": "CUSTOM",
            "schedule_slot_id": "slot-1",
            "schedule_start": "23:00",
            "schedule_end": "06:00",
            "schedule_days": [1, 2, 3],
            "schedule_reminder_enabled": "true",
            "schedule_reminder_min": 10,
            "green_battery_supported": True,
            "green_battery_enabled": False,
        },
    )
    sensor = EnphaseChargeModeSensor(coord, sn)
    attrs = sensor.extra_state_attributes
    assert attrs["preferred_mode"] == "SCHEDULED_CHARGING"
    assert attrs["effective_mode"] == "IMMEDIATE"
    assert attrs["charge_mode_supported"] is True
    assert attrs["amp_control_applicable"] is True
    assert attrs["amp_control_managed_by_mode"] is None
    assert attrs["amp_control_applies_in_modes"] == [
        "MANUAL_CHARGING",
        "SCHEDULED_CHARGING",
        "IMMEDIATE",
    ]
    assert attrs["schedule_slot_id"] == "slot-1"
    assert attrs["schedule_days"] == [1, 2, 3]
    assert attrs["schedule_reminder_enabled"] is True
    assert attrs["green_battery_supported"] is True
    assert attrs["green_battery_enabled"] is False

    coord.data[sn]["schedule_reminder_enabled"] = "off"
    coord.data[sn]["green_battery_supported"] = 1
    coord.data[sn]["green_battery_enabled"] = "disabled"
    attrs = sensor.extra_state_attributes
    assert attrs["schedule_reminder_enabled"] is False
    assert attrs["green_battery_supported"] is True
    assert attrs["green_battery_enabled"] is False

    coord.data[sn]["schedule_reminder_enabled"] = None
    assert sensor.extra_state_attributes["schedule_reminder_enabled"] is None
    coord.data[sn]["schedule_reminder_enabled"] = "maybe"
    assert sensor.extra_state_attributes["schedule_reminder_enabled"] is None


def test_charge_mode_sensor_smart_icon():
    from custom_components.enphase_ev.sensor import EnphaseChargeModeSensor

    sn = RANDOM_SERIAL
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charge_mode_pref": "SMART_CHARGING",
            "charge_mode": "SMART_CHARGING",
        },
    )

    sensor = EnphaseChargeModeSensor(coord, sn)
    assert sensor.native_value == "SMART_CHARGING"
    assert sensor.icon == "mdi:leaf"
    assert sensor.extra_state_attributes["amp_control_applicable"] is False
    assert (
        sensor.extra_state_attributes["amp_control_managed_by_mode"] == "SMART_CHARGING"
    )


def test_last_session_sensor_exposes_auth_metadata(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from homeassistant.util import dt as dt_util

    sn = RANDOM_SERIAL
    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)
    coord = _mk_coord_with(
        sn,
        {
            "sn": sn,
            "name": "Garage EV",
            "charging": False,
            "session_auth_status": 1,
            "session_auth_type": "APP",
            "session_auth_identifier": "user@example.com",
            "session_auth_token_present": True,
            "energy_today_sessions": [
                {
                    "session_id": "history-1",
                    "start": "2025-10-01T03:00:00+00:00",
                    "end": "2025-10-01T04:00:00+00:00",
                    "energy_kwh_total": 2.5,
                    "auth_type": "RFID",
                    "auth_identifier": "tag-123",
                    "auth_token": "present",
                }
            ],
        },
    )
    sensor = EnphaseEnergyTodaySensor(coord, sn)
    assert sensor.native_value == pytest.approx(2.5)
    attrs = sensor.extra_state_attributes
    assert attrs["session_auth_status"] == 1
    assert attrs["session_auth_type"] == "RFID"
    assert "session_auth_identifier" not in attrs
    assert attrs["session_auth_token_present"] is True
