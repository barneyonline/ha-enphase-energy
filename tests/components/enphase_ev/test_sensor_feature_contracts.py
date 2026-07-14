"""Public contract tests for decomposed sensor feature modules."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

from custom_components.enphase_ev.coordinator import EnphaseCoordinator
from custom_components.enphase_ev.sensor import (
    EnphaseBatteryStorageChargeSensor as PlatformBatteryStorageChargeSensor,
    EnphaseHeatPumpStatusSensor as PlatformHeatPumpStatusSensor,
)
from custom_components.enphase_ev.sensor_battery import (
    BatterySensorModel,
    EnphaseBatteryStorageChargeSensor,
)
from custom_components.enphase_ev.sensor_heatpump import (
    EnphaseHeatPumpStatusSensor,
    HeatPumpSensorModel,
)


def test_platform_keeps_feature_entity_compatibility_exports() -> None:
    """The HA platform preserves class identity for existing imports."""

    assert PlatformBatteryStorageChargeSensor is EnphaseBatteryStorageChargeSensor
    assert PlatformHeatPumpStatusSensor is EnphaseHeatPumpStatusSensor


def test_battery_sensor_model_snapshot_contract_and_revision_cache() -> None:
    """Battery entities consume the public model instead of raw payload fields."""

    battery_storage = Mock(
        return_value={
            "serial_number": "BAT-1",
            "charge_level": 72.5,
            "status_text": "normal",
        }
    )
    coordinator = cast(
        EnphaseCoordinator,
        SimpleNamespace(data=object(), battery_storage=battery_storage),
    )
    model = BatterySensorModel(coordinator, "BAT-1")

    assert model.snapshot() == {
        "serial_number": "BAT-1",
        "charge_level": 72.5,
        "status_text": "normal",
    }
    assert model.snapshot() is model.snapshot()
    battery_storage.assert_called_once_with("BAT-1")

    coordinator.data = object()
    assert model.snapshot() is not None
    assert battery_storage.call_count == 2


def test_heatpump_sensor_model_uses_public_runtime_snapshots() -> None:
    """Heat-pump entity state is copied through a typed feature boundary."""

    coordinator = cast(
        EnphaseCoordinator,
        SimpleNamespace(
            heatpump_runtime_state={
                "device_uid": "hp-1",
                "heatpump_status": "RUNNING",
            },
            heatpump_daily_consumption={
                "device_uid": "hp-1",
                "daily_energy_wh": 1250.0,
            },
        ),
    )
    model = HeatPumpSensorModel(coordinator)

    runtime = model.runtime_snapshot()
    daily = model.daily_snapshot()
    assert runtime == {"device_uid": "hp-1", "heatpump_status": "RUNNING"}
    assert daily == {"device_uid": "hp-1", "daily_energy_wh": 1250.0}
    assert model.runtime_device_uid == "hp-1"

    runtime["heatpump_status"] = "STOPPED"
    daily["daily_energy_wh"] = 0
    assert coordinator.heatpump_runtime_state["heatpump_status"] == "RUNNING"
    assert coordinator.heatpump_daily_consumption["daily_energy_wh"] == 1250.0
