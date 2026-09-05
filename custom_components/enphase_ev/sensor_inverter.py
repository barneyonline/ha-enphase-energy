"""Microinverter telemetry, lifetime energy, and connectivity entities."""

from __future__ import annotations

import math
from typing import Any, cast

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .parsing_helpers import coerce_optional_float
from .runtime_helpers import (
    coerce_optional_text as _gateway_clean_text,
)
from .runtime_helpers import (
    inventory_type_device_info as _type_device_info,
)
from .sensor_base import EnphaseSiteSensorEntity as _SiteBaseEntity
from .sensor_common import _battery_parse_timestamp, _title_case_status
from .sensor_snapshot_helpers import parse_gateway_timestamp as _gateway_parse_timestamp


class EnphaseInverterTelemetrySensor(CoordinatorEntity, SensorEntity):  # type: ignore[misc]
    """Optional live parameter telemetry for one microinverter."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"sampled_at", "parameter_ids"})
    _attr_translation_key = "inverter_telemetry"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coord: EnphaseCoordinator,
        serial: str,
        *,
        enabled_default: bool = False,
    ) -> None:
        super().__init__(coord)
        self._coord = coord
        self._sn = str(serial)
        self._attr_unique_id = f"{DOMAIN}_inverter_{self._sn}_telemetry"
        self._attr_entity_registry_enabled_default = enabled_default
        self._attr_translation_placeholders = {"serial_number": self._sn}

    def _snapshot(self) -> dict[str, object]:
        payload = self._coord.inverter_data(self._sn)
        return payload if isinstance(payload, dict) else {}

    def _telemetry(self) -> dict[str, object]:
        telemetry = self._snapshot().get("telemetry")
        return dict(telemetry) if isinstance(telemetry, dict) else {}

    @property
    def available(self) -> bool:
        return bool(super().available and self._telemetry())

    @property
    def native_value(self) -> Any:
        number = coerce_optional_float(self._telemetry().get("power"))
        return number if number is not None and math.isfinite(number) else None

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot()
        telemetry = self._telemetry()
        attrs: dict[str, object] = {}
        attribute_names = {
            "power": "power_w",
            "ac_voltage": "ac_voltage_v",
            "dc_voltage": "dc_voltage_v",
            "ac_current": "ac_current_a",
            "dc_current": "dc_current_a",
            "ac_frequency": "ac_frequency_hz",
            "temperature": "temperature_c",
            "signal_strength": "signal_strength",
            "firmware": "firmware",
        }
        for key, attribute_name in attribute_names.items():
            value = telemetry.get(key)
            if value is not None:
                attrs[attribute_name] = value
        for key in ("sampled_at", "parameter_ids"):
            value = telemetry.get(key)
            if isinstance(value, dict) and value:
                attrs[key] = dict(value)
        if snapshot.get("fw1") is not None:
            attrs["firmware_primary"] = snapshot["fw1"]
        if snapshot.get("fw2") is not None:
            attrs["firmware_secondary"] = snapshot["fw2"]
        if snapshot.get("rssi") is not None and "signal_strength" not in attrs:
            attrs["signal_strength"] = snapshot["rssi"]
        return attrs

    @property
    def device_info(self) -> Any:
        from homeassistant.helpers.entity import DeviceInfo

        info = _type_device_info(self._coord, "microinverter")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:microinverter")},
            manufacturer="Enphase",
            name="IQ Microinverters",
        )


class EnphaseInverterLifetimeEnergySensor(CoordinatorEntity, RestoreSensor):  # type: ignore[misc]
    """Lifetime production for one inverter under the shared microinverter device."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2
    _attr_translation_key = "inverter_lifetime_energy"
    _unrecorded_attributes = frozenset(
        {"sampled_at_utc", "status", "status_text", "rssi"}
    )

    def __init__(
        self,
        coord: EnphaseCoordinator,
        serial: str,
        *,
        enabled_default: bool = True,
    ) -> None:
        super().__init__(coord)
        self._coord = coord
        self._sn = str(serial)
        self._attr_translation_placeholders = {"serial": self._sn}
        self._attr_unique_id = f"{DOMAIN}_inverter_{self._sn}_lifetime_energy"
        self._attr_entity_registry_enabled_default = enabled_default
        self._last_good_native_value: float | None = None
        self._snapshot_cache_sources: tuple[object, object] | None = None
        self._snapshot_cache: dict[str, object] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Legacy builds briefly published MWh. Force canonical unit for this sensor.
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        last = await self.async_get_last_sensor_data()
        if last is None:
            return
        try:
            restored = (
                float(last.native_value) if last.native_value is not None else None
            )
        except Exception:  # noqa: BLE001
            restored = None
        if restored is not None and restored >= 0:
            restored_unit = getattr(last, "native_unit_of_measurement", None)
            unit_text = ""
            if restored_unit is not None:
                try:
                    unit_text = str(restored_unit).strip().lower()
                except Exception:  # noqa: BLE001
                    unit_text = ""
            if unit_text == "mwh":
                restored *= 1000.0
            elif unit_text == "wh":
                restored /= 1000.0
            if not math.isfinite(restored) or restored < 0:
                return
            restored = round(restored, 2)
            self._last_good_native_value = restored
            self._attr_native_value = restored

    def _snapshot(self) -> dict[str, object] | None:
        coordinator_data = getattr(self._coord, "data", None)
        inverter_data = getattr(self._coord, "_inverter_data", None)
        cacheable = coordinator_data is not None or inverter_data is not None
        sources = self._snapshot_cache_sources
        if (
            cacheable
            and sources is not None
            and coordinator_data is sources[0]
            and inverter_data is sources[1]
        ):
            return self._snapshot_cache
        getter = getattr(self._coord, "inverter_data", None)
        if not callable(getter):
            return None
        data = getter(self._sn)
        if isinstance(data, dict):
            if cacheable:
                self._snapshot_cache_sources = (coordinator_data, inverter_data)
                self._snapshot_cache = data
            return data
        if cacheable:
            self._snapshot_cache_sources = (coordinator_data, inverter_data)
            self._snapshot_cache = None
        return None

    @property
    def available(self) -> bool:
        return bool(super().available and self._snapshot() is not None)

    @property
    def native_value(self) -> Any:
        data = self._snapshot()
        if not isinstance(data, dict):
            return self._last_good_native_value
        raw_wh = data.get("lifetime_production_wh")
        try:
            value_wh = float(raw_wh) if raw_wh is not None else None  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value_wh = None
        if self._last_good_native_value is not None and not math.isfinite(
            self._last_good_native_value
        ):
            self._last_good_native_value = None
        if value_wh is None or not math.isfinite(value_wh) or value_wh < 0:
            return self._last_good_native_value
        value_kwh = round(value_wh / 1000.0, 2)
        if (
            self._last_good_native_value is not None
            and value_kwh < self._last_good_native_value
        ):
            return self._last_good_native_value
        self._last_good_native_value = value_kwh
        return value_kwh

    @property
    def extra_state_attributes(self) -> Any:
        data = self._snapshot() or {}
        sampled_at = _battery_parse_timestamp(
            data.get("last_report")
            or data.get("last_reported")
            or data.get("last_reported_at")
            or data.get("lastReportedAt")
        )
        return {
            "sampled_at_utc": (
                sampled_at.isoformat() if sampled_at is not None else None
            ),
            "status": data.get("status"),
            "status_text": data.get("status_text"),
            "rssi": data.get("rssi"),
        }

    @property
    def device_info(self) -> Any:
        from homeassistant.helpers.entity import DeviceInfo

        info = _type_device_info(self._coord, "microinverter")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:microinverter")},
            manufacturer="Enphase",
            name="IQ Microinverters",
        )


def _microinverter_connectivity_state(snapshot: dict[str, object]) -> str | None:
    total = int(snapshot.get("total_inverters", 0) or 0)  # type: ignore[call-overload]
    reporting = int(snapshot.get("reporting_inverters", 0) or 0)  # type: ignore[call-overload]
    not_reporting = int(snapshot.get("not_reporting_inverters", 0) or 0)  # type: ignore[call-overload]
    unknown = int(snapshot.get("unknown_inverters", 0) or 0)  # type: ignore[call-overload]
    if total <= 0:
        return None
    if reporting >= total:
        return "online"
    if reporting == 0 and not_reporting > 0:
        return "offline"
    if reporting > 0 and reporting < total:
        return "degraded"
    if unknown >= total:
        return "unknown"
    return "degraded"


def _microinverter_inventory_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    summary_getter = getattr(coord, "microinverter_inventory_summary", None)
    if callable(summary_getter):
        try:
            snapshot = summary_getter()
        except Exception:  # noqa: BLE001
            snapshot = None
        if isinstance(snapshot, dict):
            return snapshot
    bucket = coord.inventory_view.type_bucket("microinverter") or {}
    members = bucket.get("devices")
    if isinstance(members, list):
        safe_members = [dict(item) for item in members if isinstance(item, dict)]
    else:
        safe_members = []

    status_counts_raw = bucket.get("status_counts")
    status_counts: dict[str, int] = {}
    has_status_counts = isinstance(status_counts_raw, dict)
    if isinstance(status_counts_raw, dict):
        for key in ("total", "normal", "warning", "error", "not_reporting", "unknown"):
            try:
                status_counts[key] = int(status_counts_raw.get(key, 0) or 0)
            except Exception:
                status_counts[key] = 0

    try:
        total_inverters = int(cast(Any, bucket.get("count", len(safe_members)) or 0))
    except Exception:
        total_inverters = len(safe_members)
    if status_counts.get("total", 0) > 0:
        total_inverters = max(total_inverters, int(status_counts.get("total", 0)))

    not_reporting = max(0, int(status_counts.get("not_reporting", 0)))
    unknown = max(0, int(status_counts.get("unknown", 0)))
    if not has_status_counts:
        unknown = total_inverters
    elif (
        total_inverters > 0
        and int(status_counts.get("total", 0) or 0) <= 0
        and max(
            0,
            int(status_counts.get("normal", 0) or 0)
            + int(status_counts.get("warning", 0) or 0)
            + int(status_counts.get("error", 0) or 0)
            + not_reporting
            + unknown,
        )
        == 0
    ):
        unknown = total_inverters
    known_status_total = not_reporting + unknown
    if known_status_total > total_inverters:
        overflow = known_status_total - total_inverters
        unknown = max(0, unknown - overflow)
    reporting = max(0, total_inverters - not_reporting - unknown)

    latest_reported = _gateway_parse_timestamp(
        bucket.get("latest_reported_utc")
        if bucket.get("latest_reported_utc") is not None
        else bucket.get("latest_reported")
    )
    latest_reported_device = (
        dict(cast(dict[str, Any], bucket.get("latest_reported_device")))
        if isinstance(bucket.get("latest_reported_device"), dict)
        else None
    )
    for member in safe_members:
        parsed_last = None
        for key in (
            "last_report",
            "last_reported",
            "last_reported_at",
            "last-report",
        ):
            parsed_last = _gateway_parse_timestamp(member.get(key))
            if parsed_last is not None:
                break
        if parsed_last is None:
            continue
        if latest_reported is None or parsed_last > latest_reported:
            latest_reported = parsed_last
            latest_reported_device = {
                "serial_number": _gateway_clean_text(member.get("serial_number")),
                "name": _gateway_clean_text(member.get("name")),
                "status": _gateway_clean_text(
                    member.get("statusText")
                    if member.get("statusText") is not None
                    else member.get("status")
                ),
            }

    snapshot: dict[str, object] = {  # type: ignore[no-redef]
        "total_inverters": total_inverters,
        "reporting_inverters": reporting,
        "not_reporting_inverters": not_reporting,
        "unknown_inverters": unknown,
        "status_counts": status_counts,
        "status_summary": bucket.get("status_summary"),
        "model_summary": bucket.get("model_summary"),
        "firmware_summary": bucket.get("firmware_summary"),
        "array_summary": bucket.get("array_summary"),
        "panel_info": (
            dict(cast(dict[str, Any], bucket.get("panel_info")))
            if isinstance(bucket.get("panel_info"), dict)
            else None
        ),
        "status_type_counts": (
            dict(cast(dict[str, Any], bucket.get("status_type_counts")))
            if isinstance(bucket.get("status_type_counts"), dict)
            else None
        ),
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_reported_device,
        "production_start_date": bucket.get("production_start_date"),
        "production_end_date": bucket.get("production_end_date"),
    }
    connectivity_state = bucket.get("connectivity_state")
    if not isinstance(connectivity_state, str) or not connectivity_state.strip():
        connectivity_state = _microinverter_connectivity_state(snapshot)
    snapshot["connectivity_state"] = connectivity_state
    return snapshot  # type: ignore[no-any-return]


class EnphaseMicroinverterConnectivityStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "microinverter_connectivity_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "microinverter_connectivity_status",
            "Microinverter Connectivity Status",
            type_key="microinverter",
        )

    @property
    def available(self) -> bool:
        if not bool(getattr(self._coord, "include_inverters", True)):
            return False
        if not super().available:
            return False
        snapshot = _microinverter_inventory_snapshot(self._coord)
        if int(snapshot.get("total_inverters", 0) or 0) > 0:  # type: ignore[call-overload]
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self) -> Any:
        return _title_case_status(
            _microinverter_inventory_snapshot(self._coord).get("connectivity_state"),
            getattr(self, "hass", None) or self._coord.hass,
        )

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = _microinverter_inventory_snapshot(self._coord)
        return {
            "total_inverters": snapshot.get("total_inverters"),
            "reporting_inverters": snapshot.get("reporting_inverters"),
            "not_reporting_inverters": snapshot.get("not_reporting_inverters"),
            "unknown_inverters": snapshot.get("unknown_inverters"),
            "status_counts": snapshot.get("status_counts"),
            "status_summary": snapshot.get("status_summary"),
        }


class EnphaseMicroinverterReportingCountSensor(_SiteBaseEntity):
    _attr_translation_key = "microinverter_reporting_count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "devices",
            "model_counts",
            "firmware_counts",
            "array_counts",
            "panel_info",
            "status_type_counts",
        }
    )

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "microinverter_reporting_count",
            "Active Microinverters",
            type_key="microinverter",
        )

    @property
    def available(self) -> bool:
        if not bool(getattr(self._coord, "include_inverters", True)):
            return False
        if not super().available:
            return False
        snapshot = _microinverter_inventory_snapshot(self._coord)
        if int(snapshot.get("total_inverters", 0) or 0) > 0:  # type: ignore[call-overload]
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self) -> Any:
        snapshot = _microinverter_inventory_snapshot(self._coord)
        if int(snapshot.get("total_inverters", 0) or 0) <= 0:  # type: ignore[call-overload]
            return None
        return int(snapshot.get("reporting_inverters", 0) or 0)  # type: ignore[call-overload]

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = _microinverter_inventory_snapshot(self._coord)
        bucket = self._coord.inventory_view.type_bucket("microinverter") or {}
        members = bucket.get("devices")
        safe_members = (
            [dict(item) for item in members if isinstance(item, dict)]
            if isinstance(members, list)
            else []
        )
        try:
            device_count = int(
                cast(
                    Any,
                    bucket.get("count", snapshot.get("total_inverters", 0)) or 0,
                )
            )
        except Exception:
            device_count = int(snapshot.get("total_inverters", 0) or 0)  # type: ignore[call-overload]
        type_label = bucket.get("type_label")
        if not isinstance(type_label, str) or not type_label.strip():
            candidate = self._coord.inventory_view.type_label("microinverter")
            if isinstance(candidate, str) and candidate.strip():
                type_label = candidate
            else:
                type_label = "Microinverters"
        return {
            "type_key": bucket.get("type_key") or "microinverter",
            "type_label": type_label,
            "device_count": device_count,
            "devices": safe_members,
            "model_counts": (
                dict(cast(dict[str, Any], bucket.get("model_counts")))
                if isinstance(bucket.get("model_counts"), dict)
                else None
            ),
            "model_summary": snapshot.get("model_summary"),
            "firmware_counts": (
                dict(cast(dict[str, Any], bucket.get("firmware_counts")))
                if isinstance(bucket.get("firmware_counts"), dict)
                else None
            ),
            "firmware_summary": snapshot.get("firmware_summary"),
            "array_counts": (
                dict(cast(dict[str, Any], bucket.get("array_counts")))
                if isinstance(bucket.get("array_counts"), dict)
                else None
            ),
            "array_summary": snapshot.get("array_summary"),
            "panel_info": snapshot.get("panel_info"),
            "status_type_counts": snapshot.get("status_type_counts"),
            "production_start_date": snapshot.get("production_start_date"),
            "production_end_date": snapshot.get("production_end_date"),
        }


class EnphaseMicroinverterLastReportedSensor(_SiteBaseEntity):
    _attr_translation_key = "microinverter_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "microinverter_last_reported",
            "Microinverter Last Reported",
            type_key="microinverter",
        )

    @property
    def available(self) -> bool:
        if not bool(getattr(self._coord, "include_inverters", True)):
            return False
        if not super().available:
            return False
        snapshot = _microinverter_inventory_snapshot(self._coord)
        return snapshot.get("latest_reported") is not None

    @property
    def native_value(self) -> Any:
        return _microinverter_inventory_snapshot(self._coord).get("latest_reported")

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = _microinverter_inventory_snapshot(self._coord)
        return {
            "latest_reported_device": snapshot.get("latest_reported_device"),
        }
