"""Heat-pump sensor models and entities.

Payload interpretation is kept here so the Home Assistant platform entry point only
coordinates discovery and entity registration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from typing import Any, TypedDict, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.helpers.entity import EntityCategory

from .coordinator import EnphaseCoordinator
from .labels import friendly_status_text, status_label
from .parsing_helpers import heatpump_status_text
from .runtime_helpers import coerce_optional_text as _gateway_clean_text
from .sensor_base import EnphaseSiteSensorEntity as _SiteBaseEntity
from .sensor_snapshot_helpers import (
    parse_gateway_timestamp as _gateway_parse_timestamp,
)


class HeatPumpInventorySnapshot(TypedDict, total=False):
    """Normalized inventory contract consumed by heat-pump entities."""

    total_devices: int
    status_counts: dict[str, int]
    status_summary: str | None
    overall_status_text: str | None
    device_type_counts: dict[str, int]
    model_summary: str | None
    firmware_summary: str | None
    latest_reported: datetime | None
    latest_reported_utc: str | None
    latest_reported_device: object
    without_last_report_count: int
    hems_data_stale: bool
    hems_last_success_utc: str | None
    hems_last_success_age_s: float | None
    members: list[dict[str, object]]


def _title_case_status(value: object, hass: object | None = None) -> str | None:
    return status_label(value, hass=hass) or friendly_status_text(value)


def _heatpump_member_device_type(member: dict[str, object] | None) -> str | None:
    if not isinstance(member, dict):
        return None
    value = (
        member.get("device_type")
        if member.get("device_type") is not None
        else member.get("device-type")
    )
    text = _gateway_clean_text(value)
    if not text:
        return None
    return text.upper()


def _heatpump_member_status_text(member: dict[str, object] | None) -> str | None:
    return heatpump_status_text(member)


def _heatpump_status_counts(members: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {
        "total": len(members),
        "normal": 0,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
        "unknown": 0,
    }
    for member in members:
        status_key = EnphaseCoordinator._normalize_inverter_status(
            _heatpump_member_status_text(member)
        )
        counts[status_key] = int(counts.get(status_key, 0)) + 1
    return counts


def _heatpump_worst_status_text(status_counts: dict[str, int]) -> str | None:
    if int(status_counts.get("error", 0) or 0) > 0:
        return "Error"
    if int(status_counts.get("warning", 0) or 0) > 0:
        return "Warning"
    if int(status_counts.get("not_reporting", 0) or 0) > 0:
        return "Not Reporting"
    if int(status_counts.get("unknown", 0) or 0) > 0:
        return "Unknown"
    if int(status_counts.get("normal", 0) or 0) > 0:
        return "Normal"
    return None


def _heatpump_member_last_reported(member: dict[str, object] | None) -> datetime | None:
    if not isinstance(member, dict):
        return None
    for key in ("last_report", "last_reported", "last_reported_at", "last-report"):
        parsed = _gateway_parse_timestamp(member.get(key))
        if parsed is not None:
            return parsed
    return None


def _heatpump_snapshot(coord: EnphaseCoordinator) -> HeatPumpInventorySnapshot:
    summary_getter = getattr(coord, "heatpump_inventory_summary", None)
    if callable(summary_getter):
        try:
            snapshot = summary_getter()
        except Exception:  # noqa: BLE001
            snapshot = None
        if isinstance(snapshot, dict):
            return cast(HeatPumpInventorySnapshot, snapshot)
    bucket = coord.inventory_view.type_bucket("heatpump") or {}
    members = bucket.get("devices")
    safe_members = (
        [dict(item) for item in members if isinstance(item, dict)]
        if isinstance(members, list)
        else []
    )
    status_counts_raw = bucket.get("status_counts")
    status_counts: dict[str, int] | None = None
    if isinstance(status_counts_raw, dict):
        parsed_counts = {
            "total": 0,
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
            "unknown": 0,
        }
        try:
            for key in parsed_counts:
                parsed_counts[key] = int(status_counts_raw.get(key, 0) or 0)
            status_counts = parsed_counts
        except Exception:
            status_counts = None
    if status_counts is None:
        status_counts = _heatpump_status_counts(safe_members)

    try:
        total_devices = int(cast(Any, bucket.get("count", len(safe_members)) or 0))
    except Exception:
        total_devices = len(safe_members)
    if total_devices <= 0:
        total_devices = len(safe_members)
    status_counts["total"] = max(int(status_counts.get("total", 0) or 0), total_devices)

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
    without_last_report_count = 0
    if latest_reported is None:
        for member in safe_members:
            member_last_reported = _heatpump_member_last_reported(member)
            if member_last_reported is None:
                without_last_report_count += 1
                continue
            if latest_reported is None or member_last_reported > latest_reported:
                latest_reported = member_last_reported
                latest_reported_device = {
                    "device_type": _heatpump_member_device_type(member),
                    "name": _gateway_clean_text(member.get("name")),
                    "device_uid": _gateway_clean_text(
                        member.get("device_uid")
                        if member.get("device_uid") is not None
                        else member.get("device-uid")
                    ),
                    "status": _heatpump_member_status_text(member),
                }
    overall_status_text = _gateway_clean_text(bucket.get("overall_status_text"))
    if not overall_status_text:
        for member in safe_members:
            if _heatpump_member_device_type(member) != "HEAT_PUMP":
                continue
            overall_status_text = _heatpump_member_status_text(member)
            if overall_status_text:
                break
    if not overall_status_text:
        overall_status_text = _heatpump_worst_status_text(status_counts)

    device_type_counts: dict[str, int]
    device_type_counts_raw = bucket.get("device_type_counts")
    if isinstance(device_type_counts_raw, dict):
        device_type_counts = {}
        for key, value in device_type_counts_raw.items():
            if key is None:
                continue
            try:
                count = int(value)
            except Exception:
                continue
            if count <= 0:
                continue
            device_type_counts[str(key)] = count
    else:
        device_type_counts = {}
        for member in safe_members:
            device_type = _heatpump_member_device_type(member) or "UNKNOWN"
            device_type_counts[device_type] = device_type_counts.get(device_type, 0) + 1

    status_summary = bucket.get("status_summary")
    if not isinstance(status_summary, str) or not status_summary.strip():
        status_summary = EnphaseCoordinator._format_inverter_status_summary(
            status_counts
        )

    hems_last_success_utc = getattr(coord, "_hems_devices_last_success_utc", None)
    if not isinstance(hems_last_success_utc, datetime):
        hems_last_success_utc = None
    hems_last_success_mono = getattr(coord, "_hems_devices_last_success_mono", None)
    hems_last_success_age_s: float | None = None
    if isinstance(hems_last_success_mono, (int, float)):
        age = time.monotonic() - float(hems_last_success_mono)
        if age >= 0:
            hems_last_success_age_s = round(age, 1)

    return {
        "total_devices": total_devices,
        "members": safe_members,
        "status_counts": status_counts,
        "status_summary": status_summary,
        "device_type_counts": device_type_counts,
        "model_summary": _gateway_clean_text(bucket.get("model_summary")),
        "firmware_summary": _gateway_clean_text(bucket.get("firmware_summary")),
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_reported_device,
        "without_last_report_count": without_last_report_count,
        "overall_status_text": overall_status_text,
        "hems_data_stale": bool(getattr(coord, "_hems_devices_using_stale", False)),
        "hems_last_success_utc": (
            hems_last_success_utc.isoformat()
            if hems_last_success_utc is not None
            else None
        ),
        "hems_last_success_age_s": hems_last_success_age_s,
    }


def _heatpump_type_snapshot(
    coord: EnphaseCoordinator, *, device_type: str
) -> dict[str, object]:
    summary_getter = getattr(coord, "heatpump_type_summary", None)
    if callable(summary_getter):
        try:
            snapshot = summary_getter(device_type)
        except Exception:  # noqa: BLE001
            snapshot = None
        if isinstance(snapshot, dict):
            return snapshot
    snapshot = _heatpump_snapshot(coord)
    members = [
        member
        for member in snapshot.get("members", [])
        if isinstance(member, dict)
        and _heatpump_member_device_type(member) == device_type.upper()
    ]
    counts = _heatpump_status_counts(members)
    latest_reported: datetime | None = None
    latest_device: dict[str, object] | None = None
    for member in members:
        member_last_reported = _heatpump_member_last_reported(member)
        if member_last_reported is None:
            continue
        if latest_reported is None or member_last_reported > latest_reported:
            latest_reported = member_last_reported
            latest_device = {
                "name": _gateway_clean_text(member.get("name")),
                "device_uid": _gateway_clean_text(
                    member.get("device_uid")
                    if member.get("device_uid") is not None
                    else member.get("device-uid")
                ),
                "status": _heatpump_member_status_text(member),
            }
    status_texts = [
        status
        for status in (_heatpump_member_status_text(member) for member in members)
        if status
    ]
    unique_statuses = list(dict.fromkeys(status_texts))
    if len(unique_statuses) == 1:
        native_status = unique_statuses[0]
    else:
        native_status = _heatpump_worst_status_text(counts)  # type: ignore[assignment]
    return {
        "device_type": device_type.upper(),
        "members": members,
        "member_count": len(members),
        "status_counts": counts,
        "status_summary": EnphaseCoordinator._format_inverter_status_summary(counts),
        "native_status": native_status,
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_device,
        "hems_data_stale": snapshot.get("hems_data_stale"),
        "hems_last_success_utc": snapshot.get("hems_last_success_utc"),
        "hems_last_success_age_s": snapshot.get("hems_last_success_age_s"),
    }


def _heatpump_runtime_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    snapshot = getattr(coord, "heatpump_runtime_state", None)
    if isinstance(snapshot, dict):
        return dict(snapshot)
    return {}


def _heatpump_daily_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    snapshot = getattr(coord, "heatpump_daily_consumption", None)
    if isinstance(snapshot, dict):
        return dict(snapshot)
    return {}


def _heatpump_runtime_device_uid(coord: EnphaseCoordinator) -> str | None:
    getter = getattr(coord, "_heatpump_runtime_device_uid", None)
    if callable(getter):
        try:
            return _gateway_clean_text(getter())
        except Exception:  # noqa: BLE001
            return None
    return None


def _heatpump_runtime_last_reported(snapshot: dict[str, object]) -> datetime | None:
    return _gateway_parse_timestamp(
        snapshot.get("last_report_at")
        if snapshot.get("last_report_at") is not None
        else snapshot.get("last_reported_at")
    )


def _heatpump_runtime_common_attrs(
    coord: EnphaseCoordinator, snapshot: dict[str, object]
) -> dict[str, object]:
    last_reported = _heatpump_runtime_last_reported(snapshot)
    return {
        "device_uid": snapshot.get("device_uid"),
        "member_name": snapshot.get("member_name"),
        "member_device_type": snapshot.get("member_device_type"),
        "pairing_status": snapshot.get("pairing_status"),
        "device_state": snapshot.get("device_state"),
        "runtime_endpoint_type": snapshot.get("endpoint_type"),
        "runtime_endpoint_timestamp": snapshot.get("endpoint_timestamp"),
        "last_report_at_utc": (
            last_reported.isoformat() if last_reported is not None else None
        ),
        "source": snapshot.get("source"),
        "using_stale": bool(
            getattr(coord, "heatpump_runtime_state_using_stale", False)
        ),
        "last_success_utc": (
            last_success.isoformat()
            if (last_success := coord.heatpump_runtime_state_last_success_utc)
            is not None
            else None
        ),
        "last_error": getattr(coord, "heatpump_runtime_state_last_error", None),
    }


def _heatpump_daily_common_attrs(
    coord: EnphaseCoordinator, snapshot: dict[str, object]
) -> dict[str, object]:
    return {
        "sampled_at_utc": snapshot.get("sampled_at_utc"),
        "device_uid": snapshot.get("device_uid"),
        "device_name": snapshot.get("device_name"),
        "split_device_uid": snapshot.get("split_device_uid"),
        "split_device_name": snapshot.get("split_device_name"),
        "member_name": snapshot.get("member_name"),
        "member_device_type": snapshot.get("member_device_type"),
        "pairing_status": snapshot.get("pairing_status"),
        "device_state": snapshot.get("device_state"),
        "daily_endpoint_type": snapshot.get("endpoint_type"),
        "daily_endpoint_timestamp": snapshot.get("endpoint_timestamp"),
        "split_endpoint_type": snapshot.get("split_endpoint_type"),
        "split_endpoint_timestamp": snapshot.get("split_endpoint_timestamp"),
        "day_key": snapshot.get("day_key"),
        "timezone": snapshot.get("timezone"),
        "source": snapshot.get("source"),
        "split_source": snapshot.get("split_source"),
        "using_stale": bool(
            getattr(coord, "heatpump_daily_consumption_using_stale", False)
        ),
        "last_success_utc": (
            last_success.isoformat()
            if (last_success := coord.heatpump_daily_consumption_last_success_utc)
            is not None
            else None
        ),
        "last_error": getattr(coord, "heatpump_daily_consumption_last_error", None),
    }


def _heatpump_sg_ready_semantics(status_text: object) -> dict[str, object]:
    text = _gateway_clean_text(status_text)
    if not text:
        return {}
    normalized = text.casefold()
    if normalized in {"recommended", "mode_3", "mode3"}:
        return {
            "sg_ready_mode": 3,
            "sg_ready_contact_state": "closed",
            "status_explanation": "Recommended means the SG Ready contact is closed.",
        }
    if normalized in {"normal", "mode_2", "mode2"}:
        return {
            "sg_ready_mode": 2,
            "sg_ready_contact_state": "open",
            "status_explanation": "Normal means the SG Ready contacts are open.",
        }
    return {}


@dataclass(frozen=True, slots=True)
class HeatPumpSensorModel:
    """Public normalized snapshot boundary used by heat-pump sensor entities."""

    coordinator: EnphaseCoordinator

    def inventory_snapshot(self) -> HeatPumpInventorySnapshot:
        """Return the normalized heat-pump inventory snapshot."""

        return _heatpump_snapshot(self.coordinator)

    def type_snapshot(self, device_type: str) -> dict[str, object]:
        """Return the normalized snapshot for one HEMS device type."""

        return _heatpump_type_snapshot(self.coordinator, device_type=device_type)

    def runtime_snapshot(self) -> dict[str, object]:
        """Return a safe copy of the public runtime snapshot."""

        return _heatpump_runtime_snapshot(self.coordinator)

    def daily_snapshot(self) -> dict[str, object]:
        """Return a safe copy of the public daily energy snapshot."""

        return _heatpump_daily_snapshot(self.coordinator)

    @property
    def runtime_device_uid(self) -> str | None:
        """Return the dedicated runtime-controller identity when available."""

        return _heatpump_runtime_device_uid(self.coordinator) or _gateway_clean_text(
            self.runtime_snapshot().get("device_uid")
        )


class EnphaseHeatPumpStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "heat_pump_status"
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord, "heat_pump_status", "Heat Pump Status", type_key="heatpump"
        )
        self._model = HeatPumpSensorModel(coord)

    def _snapshot(self) -> dict[str, object]:
        return self._model.runtime_snapshot()

    def _runtime_device_uid(self) -> str | None:
        getter = getattr(self._coord, "_heatpump_runtime_device_uid", None)
        if callable(getter):
            try:
                return getter()  # type: ignore[no-any-return]
            except Exception:  # noqa: BLE001
                return None
        uid = self._snapshot().get("device_uid")
        return _gateway_clean_text(uid)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if not self._runtime_device_uid():
            return False
        return self.native_value is not None

    @property
    def native_value(self) -> Any:
        return _title_case_status(
            self._snapshot().get("heatpump_status"),
            getattr(self, "hass", None) or self._coord.hass,
        )

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot()
        sg_ready_details = _heatpump_sg_ready_semantics(
            snapshot.get("sg_ready_mode_label") or snapshot.get("sg_ready_mode_raw")
        )
        attrs = _heatpump_runtime_common_attrs(self._coord, snapshot)
        attrs.update(
            {
                "heatpump_status_raw": snapshot.get("heatpump_status"),
                "sg_ready_mode_raw": snapshot.get("sg_ready_mode_raw"),
                "sg_ready_mode_label": snapshot.get("sg_ready_mode_label"),
                "sg_ready_active": snapshot.get("sg_ready_active"),
                "sg_ready_contact_state": snapshot.get("sg_ready_contact_state"),
                "vpp_sgready_mode_override": snapshot.get("vpp_sgready_mode_override"),
                **sg_ready_details,
            }
        )
        return attrs


class EnphaseHeatPumpConnectivityStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "heat_pump_connectivity_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"members", "latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "heat_pump_connectivity_status",
            "Heat Pump Connectivity Status",
            type_key="heatpump",
        )
        self._model = HeatPumpSensorModel(coord)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = self._model.inventory_snapshot()
        if int(snapshot.get("total_devices", 0) or 0) > 0:
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self) -> Any:
        return _title_case_status(
            self._model.inventory_snapshot().get("overall_status_text"),
            getattr(self, "hass", None) or self._coord.hass,
        )

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._model.inventory_snapshot()
        members = snapshot.get("members")
        safe_members = (
            [dict(member) for member in members if isinstance(member, dict)]
            if isinstance(members, list)
            else []
        )
        return {
            "total_devices": snapshot.get("total_devices"),
            "status_counts": snapshot.get("status_counts"),
            "status_summary": snapshot.get("status_summary"),
            "device_type_counts": snapshot.get("device_type_counts"),
            "model_summary": snapshot.get("model_summary"),
            "firmware_summary": snapshot.get("firmware_summary"),
            "latest_reported_utc": snapshot.get("latest_reported_utc"),
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "hems_data_stale": snapshot.get("hems_data_stale"),
            "hems_last_success_utc": snapshot.get("hems_last_success_utc"),
            "hems_last_success_age_s": snapshot.get("hems_last_success_age_s"),
            "members": safe_members,
        }


class _EnphaseHeatPumpDeviceTypeSensor(_SiteBaseEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"members", "latest_reported_device"}
    )
    _heatpump_device_type: str

    def __init__(
        self,
        coord: EnphaseCoordinator,
        *,
        key: str,
        name: str,
        device_type: str,
    ) -> None:
        super().__init__(coord, key, name, type_key="heatpump")
        self._heatpump_device_type = device_type
        self._model = HeatPumpSensorModel(coord)

    def _snapshot(self) -> dict[str, object]:
        return self._model.type_snapshot(self._heatpump_device_type)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return int(self._snapshot().get("member_count", 0) or 0) > 0  # type: ignore[call-overload,no-any-return]

    @property
    def native_value(self) -> Any:
        return self._snapshot().get("native_status")

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot()
        members = snapshot.get("members")
        return {
            "device_type": snapshot.get("device_type"),
            "member_count": snapshot.get("member_count"),
            "status_counts": snapshot.get("status_counts"),
            "status_summary": snapshot.get("status_summary"),
            "latest_reported_utc": snapshot.get("latest_reported_utc"),
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "hems_data_stale": snapshot.get("hems_data_stale"),
            "hems_last_success_utc": snapshot.get("hems_last_success_utc"),
            "hems_last_success_age_s": snapshot.get("hems_last_success_age_s"),
            "members": (
                [dict(member) for member in members if isinstance(member, dict)]
                if isinstance(members, list)
                else []
            ),
        }


class EnphaseHeatPumpSgReadyModeSensor(_SiteBaseEntity):
    _attr_translation_key = "heat_pump_sg_ready_mode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "heat_pump_sg_ready_mode",
            "Heat Pump SG-Ready Mode",
            type_key="heatpump",
        )

    def _snapshot(self) -> dict[str, object]:
        return _heatpump_runtime_snapshot(self._coord)

    def _runtime_device_uid(self) -> str | None:
        getter = getattr(self._coord, "_heatpump_runtime_device_uid", None)
        if callable(getter):
            try:
                return getter()  # type: ignore[no-any-return]
            except Exception:  # noqa: BLE001
                return None
        uid = self._snapshot().get("device_uid")
        return _gateway_clean_text(uid)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if not self._runtime_device_uid():
            return False
        snapshot = self._snapshot()
        return any(
            snapshot.get(key) is not None
            for key in ("sg_ready_mode_label", "sg_ready_mode_raw")
        )

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot()
        return snapshot.get("sg_ready_mode_label") or snapshot.get("sg_ready_mode_raw")

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot()
        details = _heatpump_sg_ready_semantics(
            snapshot.get("sg_ready_mode_label") or snapshot.get("sg_ready_mode_raw")
        )
        return {
            "heatpump_status_raw": snapshot.get("heatpump_status"),
            "sg_ready_mode_raw": snapshot.get("sg_ready_mode_raw"),
            "sg_ready_mode_label": snapshot.get("sg_ready_mode_label"),
            "sg_ready_active": snapshot.get("sg_ready_active"),
            "sg_ready_contact_state": snapshot.get("sg_ready_contact_state"),
            "vpp_sgready_mode_override": snapshot.get("vpp_sgready_mode_override"),
            **details,
        }


class EnphaseHeatPumpEnergyMeterSensor(_EnphaseHeatPumpDeviceTypeSensor):
    _attr_translation_key = "heat_pump_energy_meter"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            key="heat_pump_energy_meter",
            name="Heat Pump Energy Meter Status",
            device_type="ENERGY_METER",
        )


class EnphaseHeatPumpSgReadyGatewaySensor(_EnphaseHeatPumpDeviceTypeSensor):
    _attr_translation_key = "heat_pump_sg_ready_gateway"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            key="heat_pump_sg_ready_gateway",
            name="Heat Pump SG-Ready Gateway Status",
            device_type="SG_READY_GATEWAY",
        )


class EnphaseHeatPumpLastReportedSensor(_SiteBaseEntity):
    _attr_translation_key = "heat_pump_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "heat_pump_last_reported",
            "Heat Pump Last Reported",
            type_key="heatpump",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if _heatpump_runtime_device_uid(self._coord) is None:
            return False
        return (
            _heatpump_runtime_last_reported(_heatpump_runtime_snapshot(self._coord))
            is not None
        )

    @property
    def native_value(self) -> Any:
        return _heatpump_runtime_last_reported(_heatpump_runtime_snapshot(self._coord))

    @property
    def extra_state_attributes(self) -> Any:
        return {}


class _EnphaseHeatPumpDailyEnergySensor(_SiteBaseEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 3
    _daily_key: str

    def __init__(self, coord: EnphaseCoordinator, key: str, name: str) -> None:
        super().__init__(coord, key, name, type_key="heatpump")

    def _snapshot(self) -> dict[str, object]:
        return _heatpump_daily_snapshot(self._coord)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = self._snapshot()
        return snapshot.get(self._daily_key) is not None

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot()
        value = snapshot.get(self._daily_key)
        if value is None:
            return None
        try:
            return round(float(value) / 1000.0, 3)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot()
        attrs = _heatpump_daily_common_attrs(self._coord, snapshot)
        if self._daily_key == "daily_energy_wh":
            attrs["source"] = snapshot.get("source")
            attrs["device_uid"] = snapshot.get("device_uid")
            attrs["device_name"] = snapshot.get("device_name")
        else:
            attrs["source"] = snapshot.get("split_source") or snapshot.get("source")
            attrs["device_uid"] = snapshot.get("split_device_uid")
            attrs["device_name"] = snapshot.get("split_device_name")
            attrs["using_stale"] = bool(
                getattr(self._coord, "heatpump_daily_split_using_stale", False)
            )
            attrs["last_success_utc"] = (
                last_success.isoformat()
                if (last_success := self._coord.heatpump_daily_split_last_success_utc)
                is not None
                else None
            )
            attrs["last_error"] = getattr(
                self._coord, "heatpump_daily_split_last_error", None
            )
        attrs["details"] = (
            list(snapshot.get("details"))  # type: ignore[call-overload]
            if isinstance(snapshot.get("details"), list)
            else []
        )
        attrs["daily_energy_wh"] = snapshot.get("daily_energy_wh")
        attrs["split_daily_energy_wh"] = snapshot.get("split_daily_energy_wh")
        attrs["daily_grid_wh"] = snapshot.get("daily_grid_wh")
        attrs["daily_solar_wh"] = snapshot.get("daily_solar_wh")
        attrs["daily_battery_wh"] = snapshot.get("daily_battery_wh")
        return attrs


class EnphaseHeatPumpDailyEnergySensor(_EnphaseHeatPumpDailyEnergySensor):
    _attr_translation_key = "heat_pump_daily_energy"
    _daily_key = "daily_energy_wh"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "heat_pump_daily_energy", "Heat Pump Daily Energy")


class EnphaseHeatPumpDailyGridEnergySensor(_EnphaseHeatPumpDailyEnergySensor):
    _attr_translation_key = "heat_pump_daily_grid_energy"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _daily_key = "daily_grid_wh"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "heat_pump_daily_grid_energy",
            "Heat Pump Daily Grid Energy",
        )


class EnphaseHeatPumpDailySolarEnergySensor(_EnphaseHeatPumpDailyEnergySensor):
    _attr_translation_key = "heat_pump_daily_solar_energy"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _daily_key = "daily_solar_wh"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "heat_pump_daily_solar_energy",
            "Heat Pump Daily Solar Energy",
        )


class EnphaseHeatPumpDailyBatteryEnergySensor(_EnphaseHeatPumpDailyEnergySensor):
    _attr_translation_key = "heat_pump_daily_battery_energy"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _daily_key = "daily_battery_wh"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "heat_pump_daily_battery_energy",
            "Heat Pump Daily Battery Energy",
        )


class EnphaseHeatPumpPowerSensor(_SiteBaseEntity):
    _attr_translation_key = "heat_pump_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord, "heat_pump_power", "Heat Pump Power", type_key="heatpump"
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coord.heatpump_power_w is not None

    @property
    def native_value(self) -> Any:
        value = self._coord.heatpump_power_w
        if value is None:
            return None
        return round(value, 3)

    @property
    def extra_state_attributes(self) -> Any:
        runtime_snapshot = _heatpump_runtime_snapshot(self._coord)
        daily_snapshot = _heatpump_daily_snapshot(self._coord)
        attrs: dict[str, object] = {
            "sampled_at_utc": (
                self._coord.heatpump_power_sample_utc.isoformat()
                if self._coord.heatpump_power_sample_utc is not None
                else None
            ),
            "series_start_utc": (
                self._coord.heatpump_power_start_utc.isoformat()
                if self._coord.heatpump_power_start_utc is not None
                else None
            ),
            "device_uid": self._coord.heatpump_power_device_uid,
            "source": self._coord.heatpump_power_source,
            "raw_power_w": self._coord.heatpump_power_raw_w,
            "power_window_seconds": self._coord.heatpump_power_window_seconds,
            "power_validation": self._coord.heatpump_power_validation,
            "smoothed": self._coord.heatpump_power_smoothed,
            "using_stale": bool(
                getattr(self._coord, "heatpump_power_using_stale", False)
            ),
            "last_success_utc": (
                last_success.isoformat()
                if (last_success := self._coord.heatpump_power_last_success_utc)
                is not None
                else None
            ),
            "last_error": self._coord.heatpump_power_last_error,
        }
        attrs.update(
            {
                key: value
                for key, value in _heatpump_runtime_common_attrs(
                    self._coord, runtime_snapshot
                ).items()
                if key
                not in {"device_uid", "source", "last_error", "last_report_at_utc"}
            }
        )
        attrs.update(
            {
                key: value
                for key, value in _heatpump_daily_common_attrs(
                    self._coord, daily_snapshot
                ).items()
                if key
                in {
                    "device_name",
                    "member_name",
                    "member_device_type",
                    "pairing_status",
                    "device_state",
                    "daily_endpoint_type",
                    "daily_endpoint_timestamp",
                    "day_key",
                    "timezone",
                }
            }
        )
        if self._coord.heatpump_power_last_error:
            attrs["last_error"] = self._coord.heatpump_power_last_error
        return attrs
