"""Per-device Enphase battery sensor entities."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass, field
import math
from typing import Any, TypedDict, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ac_battery_support import (
    ac_battery_device_info,
    ac_battery_snapshot_last_reported,
    ac_battery_storage_snapshot,
)
from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .runtime_helpers import (
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
)
from .sensor_battery_helpers import (
    battery_parse_timestamp as _battery_parse_timestamp,
    battery_snapshot_last_reported as _battery_snapshot_last_reported,
)

BATTERY_LED_STATUS_STATE_MAP: dict[int, str] = {
    12: "charging",
    13: "discharging",
    14: "idle",
    15: "idle",
    16: "idle",
    17: "idle",
}


class BatteryStorageSnapshot(TypedDict, total=False):
    """Normalized battery fields consumed by per-device sensors."""

    battery_id: object
    serial_number: str
    name: object
    identity: object
    charge_level: float
    current_charge_pct: object
    led_status: object
    status: object
    status_text: object
    status_normalized: object
    health: object
    battery_soh: object
    soh: object
    state_of_health: object
    stateOfHealth: object
    battery_health: object
    cycle_count: object
    last_reported: object
    part_number: object
    phase: object
    sleep_state: object
    sleep_control_class: object
    sleep_control_label: object
    power_w: object
    operating_mode: object


@dataclass(slots=True)
class BatterySensorModel:
    """Public snapshot boundary between battery entities and coordinator state."""

    coordinator: EnphaseCoordinator
    serial: str
    ac_battery: bool = False
    _cache_token: tuple[int, int] | None = field(default=None, init=False)
    _cache: BatteryStorageSnapshot | None = field(default=None, init=False)

    def snapshot(self) -> BatteryStorageSnapshot | None:
        """Return the current normalized per-device battery snapshot."""

        coordinator_data = getattr(self.coordinator, "data", None)
        family_data = getattr(
            self.coordinator,
            "_ac_battery_data" if self.ac_battery else "_battery_storage_data",
            None,
        )
        cacheable = coordinator_data is not None or family_data is not None
        token = (id(coordinator_data), id(family_data))
        if cacheable and token == self._cache_token:
            return self._cache

        if self.ac_battery:
            payload = ac_battery_storage_snapshot(self.coordinator, self.serial)
        else:
            getter = getattr(self.coordinator, "battery_storage", None)
            payload = getter(self.serial) if callable(getter) else None
        snapshot = (
            cast(BatteryStorageSnapshot, payload) if isinstance(payload, dict) else None
        )
        if cacheable:
            self._cache_token = token
            self._cache = snapshot
        return snapshot

    @property
    def available(self) -> bool:
        """Return whether this battery family is present in inventory."""

        family = "ac_battery" if self.ac_battery else "encharge"
        return bool(_type_available(self.coordinator, family))

    @property
    def device_info(self) -> Any:
        """Return inventory-backed device information for this battery."""

        if self.ac_battery:
            return ac_battery_device_info(self.coordinator)
        return _type_device_info(self.coordinator, "encharge")


class _EnphaseBatteryStorageBaseSensor(CoordinatorEntity, SensorEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset(
        {"serial_number", "status", "sampled_at_utc", "state"}
    )

    def __init__(
        self, coord: EnphaseCoordinator, serial: str, unique_suffix: str
    ) -> None:
        super().__init__(coord)
        self._coord = coord
        self._sn = str(serial)
        self._model = BatterySensorModel(coord, self._sn)
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_battery_{self._sn}{unique_suffix}"
        )

    def _snapshot(self) -> BatteryStorageSnapshot | None:
        return self._model.snapshot()

    @staticmethod
    def _as_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        return _battery_parse_timestamp(value)

    @property
    def available(self) -> bool:
        if not self._model.available:
            return False
        return bool(super().available and self._snapshot() is not None)

    @property
    def _battery_label(self) -> str:
        snapshot = self._snapshot() or {}
        for key in ("name", "serial_number", "identity"):
            value = snapshot.get(key)
            if value is None:
                continue
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                return text
        return self._sn

    @property
    def device_info(self) -> Any:
        from homeassistant.helpers.entity import DeviceInfo

        info = self._model.device_info
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
            name="IQ Battery",
        )


class EnphaseBatteryStorageChargeSensor(_EnphaseBatteryStorageBaseSensor):
    """Per-battery state-of-charge sensor under the shared battery type device."""

    _attr_translation_key = "battery_storage_charge"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_charge_level")

    @property
    def name(self) -> str:
        return self._battery_label

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot() or {}
        value = snapshot.get("current_charge_pct")
        if value is None:
            return None
        try:
            return round(float(value), 1)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot() or {}
        sampled_at = _battery_snapshot_last_reported(dict(snapshot))
        return {
            "serial_number": snapshot.get("serial_number") or self._sn,
            "status": snapshot.get("status"),
            "sampled_at_utc": (
                sampled_at.isoformat() if sampled_at is not None else None
            ),
        }


class EnphaseBatteryStorageStatusSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_status"

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_status")
        self._attr_translation_placeholders = {"serial": self._sn}

    @staticmethod
    def _led_status_value(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value) if value.is_integer() else None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        if not text:
            return None
        try:
            parsed = float(text)
        except Exception:  # noqa: BLE001
            return None
        if not math.isfinite(parsed) or not parsed.is_integer():
            return None
        return int(parsed)

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot() or {}
        led_status = self._led_status_value(snapshot.get("led_status"))
        return BATTERY_LED_STATUS_STATE_MAP.get(led_status, "unknown")  # type: ignore[arg-type]

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot() or {}
        attrs: dict[str, object] = {}
        led_status = self._led_status_value(snapshot.get("led_status"))
        if led_status is not None:
            attrs["state"] = led_status
        return attrs


class EnphaseBatteryStorageHealthSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_health"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_health")
        self._attr_translation_placeholders = {"serial": self._sn}

    @staticmethod
    def _parse_health_value(value: object) -> float | None:
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        if not text:
            return None
        if text.endswith("%"):
            text = text[:-1].strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except Exception:  # noqa: BLE001
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    def _health_value(self) -> float | None:
        snapshot = self._snapshot() or {}
        for key in (
            "battery_soh",
            "soh",
            "state_of_health",
            "stateOfHealth",
            "battery_health",
            "health",
        ):
            parsed = self._parse_health_value(snapshot.get(key))
            if parsed is not None:
                return parsed
        return None

    @property
    def available(self) -> bool:
        return bool(super().available and self._health_value() is not None)

    @property
    def native_value(self) -> Any:
        value = self._health_value()
        if value is None:
            return None
        return round(value, 1)


class EnphaseBatteryStorageCycleCountSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_cycle_count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_cycle_count")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot() or {}
        return self._as_int(snapshot.get("cycle_count"))


class EnphaseBatteryStorageLastReportedSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_last_reported")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def available(self) -> bool:
        return bool(super().available and self.native_value is not None)

    @property
    def native_value(self) -> Any:
        return _battery_snapshot_last_reported(dict(self._snapshot() or {}))


class _EnphaseAcBatteryStorageBaseSensor(CoordinatorEntity, SensorEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset(
        {
            "battery_id",
            "part_number",
            "phase",
            "status_text",
            "sleep_state",
            "sleep_control_class",
            "sleep_control_label",
            "operating_mode",
            "last_reported_utc",
        }
    )

    def __init__(
        self, coord: EnphaseCoordinator, serial: str, unique_suffix: str
    ) -> None:
        super().__init__(coord)
        self._coord = coord
        self._sn = str(serial)
        self._model = BatterySensorModel(coord, self._sn, ac_battery=True)
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_ac_battery_{self._sn}{unique_suffix}"
        )

    def _snapshot(self) -> BatteryStorageSnapshot | None:
        return self._model.snapshot()

    @staticmethod
    def _as_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(round(float(str(value).strip())))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _as_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except Exception:
            try:
                parsed = float(str(value).strip())
            except Exception:  # noqa: BLE001
                return None
        if not math.isfinite(parsed):
            return None
        return parsed

    @property
    def available(self) -> bool:
        if not self._model.available:
            return False
        return bool(super().available and self._snapshot() is not None)

    @property
    def _battery_label(self) -> str:
        snapshot = self._snapshot() or {}
        serial_number = snapshot.get("serial_number")
        if serial_number is not None:
            try:
                text = str(serial_number).strip()
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                return text
        return self._sn

    @property
    def device_info(self) -> Any:
        return self._model.device_info


class EnphaseAcBatteryStorageChargeSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_charge"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_charge_level")

    @property
    def name(self) -> str:
        return self._battery_label

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot() or {}
        value = self._as_float(snapshot.get("current_charge_pct"))
        if value is None:
            return None
        return round(value, 1)

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = dict(self._snapshot() or {})
        return {
            "battery_id": snapshot.get("battery_id"),
            "part_number": snapshot.get("part_number"),
            "phase": snapshot.get("phase"),
            "status_text": snapshot.get("status_text"),
            "sleep_state": snapshot.get("sleep_state"),
        }


class EnphaseAcBatteryStorageStatusSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_status"

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_status")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot() or {}
        value = snapshot.get("status_normalized")
        if value is None:
            return None
        try:
            text = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot() or {}
        return {
            "battery_id": snapshot.get("battery_id"),
            "status_text": snapshot.get("status_text"),
            "sleep_state": snapshot.get("sleep_state"),
            "sleep_control_class": snapshot.get("sleep_control_class"),
            "sleep_control_label": snapshot.get("sleep_control_label"),
        }


class EnphaseAcBatteryStoragePowerSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_power")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot() or {}
        value = self._as_float(snapshot.get("power_w"))
        if value is None:
            return None
        return round(value, 3)

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot() or {}
        reported = ac_battery_snapshot_last_reported(dict(snapshot))
        return {
            "operating_mode": snapshot.get("operating_mode"),
            "last_reported_utc": (
                reported.isoformat() if reported is not None else None
            ),
        }


class EnphaseAcBatteryStorageOperatingModeSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_operating_mode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_operating_mode")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot() or {}
        value = snapshot.get("operating_mode")
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot() or {}
        return {
            "status_text": snapshot.get("status_text"),
            "sleep_state": snapshot.get("sleep_state"),
        }


class EnphaseAcBatteryStorageCycleCountSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_cycle_count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_cycle_count")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot() or {}
        return self._as_int(snapshot.get("cycle_count"))


class EnphaseAcBatteryStorageLastReportedSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_last_reported")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def available(self) -> bool:
        return bool(super().available and self.native_value is not None)

    @property
    def native_value(self) -> Any:
        return ac_battery_snapshot_last_reported(dict(self._snapshot() or {}))
