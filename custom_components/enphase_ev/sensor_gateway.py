"""Gateway equipment sensor models and inventory presentation."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, cast

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import EnphaseCoordinator
from .device_types import is_dry_contact_type_key, member_is_retired
from .labels import friendly_status_text, status_label
from .runtime_helpers import coerce_optional_text as _gateway_clean_text
from .scalar_helpers import coerce_optional_bool as _gateway_optional_bool
from .sensor_base import EnphaseSiteSensorEntity as _SiteBaseEntity
from .sensor_common import _title_case_status
from .sensor_snapshot_helpers import parse_gateway_timestamp as _gateway_parse_timestamp

_GATEWAY_STATUS_KEYS: tuple[str, ...] = ("statusText", "status")


_GATEWAY_MODEL_KEYS: tuple[str, ...] = ("model", "channel_type", "sku_id")


_GATEWAY_FIRMWARE_KEYS: tuple[str, ...] = ("envoy_sw_version", "sw_version")


_GATEWAY_LAST_REPORT_KEYS: tuple[str, ...] = (
    "last_report",
    "last_reported",
    "lastReportedAt",
)


_GATEWAY_IP_KEYS: tuple[str, ...] = ("ip", "ip_address", "ip-address")


def _gateway_normalize_status(value: object) -> str:
    text = _gateway_clean_text(value)
    if not text:
        return "unknown"
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    if any(token in normalized for token in ("fault", "error", "critical")):
        return "error"
    if "warn" in normalized:
        return "warning"
    if any(
        token in normalized
        for token in ("not_reporting", "offline", "disconnected", "retired")
    ):
        return "not_reporting"
    if any(token in normalized for token in ("normal", "online", "connected", "ok")):
        return "normal"
    return "unknown"


def _gateway_member_ip_address(member: dict[str, object]) -> str | None:
    for key in _GATEWAY_IP_KEYS:
        ip_address = _gateway_clean_text(member.get(key))
        if ip_address:
            return cast(str | None, ip_address)
    return None


def _gateway_ip_member_kind(member: dict[str, object]) -> str | None:
    for key in ("channel_type", "channelType", "meter_type"):
        channel_type = _gateway_clean_text(member.get(key))
        if not channel_type:
            continue
        normalized = "".join(ch if ch.isalnum() else "_" for ch in channel_type.lower())
        if (
            normalized in ("enpower", "system_controller", "systemcontroller")
            or "enpower" in normalized
            or "system_controller" in normalized
            or normalized.startswith("systemcontroller")
        ):
            return "controller"
        if "production" in normalized or normalized in ("prod", "pv", "solar"):
            return "production"
        if "consumption" in normalized or normalized in (
            "cons",
            "load",
            "site_load",
        ):
            return "consumption"
    name = (_gateway_clean_text(member.get("name")) or "").lower()
    if "system controller" in name:
        return "controller"
    if "controller" in name and "meter" not in name:
        return "controller"
    if "production" in name:
        return "production"
    if "consumption" in name:
        return "consumption"
    return None


def _gateway_member_preferred_for_ip(member: dict[str, object]) -> bool:
    if _gateway_ip_member_kind(member) in {"production", "consumption", "controller"}:
        return False
    name = (_gateway_clean_text(member.get("name")) or "").lower()
    if "gateway" in name:
        return True
    return any(
        member.get(key) is not None
        for key in (
            "envoy_sw_version",
            "ap_mode",
            "supportsEntrez",
            "show_connection_details",
        )
    )


def _gateway_summary_ip_address(
    members: list[dict[str, object]],
    dashboard_envoy: object,
) -> str | None:
    candidate_members = list(members)
    if isinstance(dashboard_envoy, dict):
        candidate_members.append(dashboard_envoy)
    for member in candidate_members:
        if _gateway_member_preferred_for_ip(member):
            ip_address = _gateway_member_ip_address(member)
            if ip_address:
                return ip_address
    for member in candidate_members:
        ip_address = _gateway_member_ip_address(member)
        if ip_address and _gateway_ip_member_kind(member) not in {
            "production",
            "consumption",
            "controller",
        }:
            return ip_address
    return None


def _gateway_format_counts(counts: dict[str, int]) -> str | None:
    clean: dict[str, int] = {}
    for key, value in (counts or {}).items():
        label = _gateway_clean_text(key)
        if not label:
            continue
        try:
            count = int(value)
        except Exception:  # noqa: BLE001
            continue
        if count <= 0:
            continue
        clean[label] = count
    if not clean:
        return None
    ordered = sorted(clean.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{name} x{count}" for name, count in ordered)


def _gateway_inventory_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    summary_getter = getattr(coord, "gateway_inventory_summary", None)
    if callable(summary_getter):
        try:
            snapshot = summary_getter()
        except Exception:  # noqa: BLE001
            snapshot = None
        if isinstance(snapshot, dict):
            return snapshot
    bucket = coord.inventory_view.type_bucket("envoy") or {}
    members_raw = bucket.get("devices")
    members = (
        [item for item in members_raw if isinstance(item, dict)]
        if isinstance(members_raw, list)
        else []
    )
    detail_getter = getattr(coord, "system_dashboard_envoy_detail", None)
    dashboard_envoy = detail_getter() if callable(detail_getter) else None
    if not members and isinstance(dashboard_envoy, dict):
        members = [dict(dashboard_envoy)]
    ip_address = _gateway_summary_ip_address(members, dashboard_envoy)
    try:
        total_devices = int(cast(Any, bucket.get("count", len(members))))
    except Exception:  # noqa: BLE001
        total_devices = len(members)
    total_devices = max(total_devices, len(members))

    status_counts: dict[str, int] = {
        "normal": 0,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
        "unknown": 0,
    }
    model_counts: dict[str, int] = {}
    firmware_counts: dict[str, int] = {}
    property_keys: set[str] = set()
    connected_devices = 0
    disconnected_devices = 0
    latest_reported: datetime | None = None
    latest_reported_device: dict[str, object] | None = None
    without_last_report_count = 0

    for member in members:
        property_keys.update(str(key) for key in member.keys())

        status_source = None
        for key in _GATEWAY_STATUS_KEYS:
            if member.get(key) is not None:
                status_source = member.get(key)
                break
        status = _gateway_normalize_status(status_source)
        status_counts[status] = status_counts.get(status, 0) + 1

        connected = _gateway_optional_bool(member.get("connected"))
        if connected is None:
            if status == "normal":
                connected = True
            elif status == "not_reporting":
                connected = False
        if connected is True:
            connected_devices += 1
        elif connected is False:
            disconnected_devices += 1

        model_name = None
        for key in _GATEWAY_MODEL_KEYS:
            model_name = _gateway_clean_text(member.get(key))
            if model_name:
                break
        if model_name:
            model_counts[model_name] = model_counts.get(model_name, 0) + 1

        firmware_version = None
        for key in _GATEWAY_FIRMWARE_KEYS:
            firmware_version = _gateway_clean_text(member.get(key))
            if firmware_version:
                break
        if firmware_version:
            firmware_counts[firmware_version] = (
                firmware_counts.get(firmware_version, 0) + 1
            )

        parsed_last_report = None
        for key in _GATEWAY_LAST_REPORT_KEYS:
            parsed_last_report = _gateway_parse_timestamp(member.get(key))
            if parsed_last_report is not None:
                break
        if parsed_last_report is None:
            without_last_report_count += 1
            continue
        if latest_reported is None or parsed_last_report > latest_reported:
            latest_reported = parsed_last_report
            latest_reported_device = {
                "name": _gateway_clean_text(member.get("name")),
                "serial_number": _gateway_clean_text(member.get("serial_number")),
                "status": _gateway_clean_text(status_source),
            }

    unknown_connection_devices = max(
        0, total_devices - connected_devices - disconnected_devices
    )
    status_summary = (
        f"Normal {status_counts.get('normal', 0)} | "
        f"Warning {status_counts.get('warning', 0)} | "
        f"Error {status_counts.get('error', 0)} | "
        f"Not Reporting {status_counts.get('not_reporting', 0)} | "
        f"Unknown {status_counts.get('unknown', 0)}"
    )
    if total_devices <= 0:
        status_summary = None  # type: ignore[assignment]
    if latest_reported is None and isinstance(dashboard_envoy, dict):
        fallback_last = None
        for key in ("last_report", "last_interval_end_date"):
            fallback_last = _gateway_parse_timestamp(dashboard_envoy.get(key))
            if fallback_last is not None:
                break
        if fallback_last is not None:
            latest_reported = fallback_last
            latest_reported_device = {
                "name": _gateway_clean_text(dashboard_envoy.get("name"))
                or "IQ Gateway",
                "serial_number": _gateway_clean_text(
                    dashboard_envoy.get("serial_number")
                ),
                "status": _gateway_clean_text(
                    dashboard_envoy.get("statusText")
                    if dashboard_envoy.get("statusText") is not None
                    else dashboard_envoy.get("status")
                ),
            }

    return {
        "total_devices": total_devices,
        "connected_devices": connected_devices,
        "disconnected_devices": disconnected_devices,
        "unknown_connection_devices": unknown_connection_devices,
        "without_last_report_count": without_last_report_count,
        "status_counts": status_counts,
        "status_summary": status_summary,
        "model_counts": model_counts,
        "model_summary": _gateway_format_counts(model_counts),
        "firmware_counts": firmware_counts,
        "firmware_summary": _gateway_format_counts(firmware_counts),
        "ip_address": ip_address,
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_reported_device,
        "property_keys": sorted(property_keys),
    }


def _gateway_connectivity_state(snapshot: dict[str, object]) -> str | None:
    total = int(snapshot.get("total_devices", 0) or 0)  # type: ignore[call-overload]
    connected = int(snapshot.get("connected_devices", 0) or 0)  # type: ignore[call-overload]
    disconnected = int(snapshot.get("disconnected_devices", 0) or 0)  # type: ignore[call-overload]
    unknown = int(snapshot.get("unknown_connection_devices", 0) or 0)  # type: ignore[call-overload]
    if total <= 0:
        return None
    if connected >= total:
        return "online"
    if connected == 0 and disconnected > 0:
        return "offline"
    if connected > 0 and connected < total:
        return "degraded"
    if unknown >= total:
        return "unknown"
    return "degraded"


def _gateway_channel_type_kind(value: object) -> str | None:
    text = _gateway_clean_text(value)
    if not text:
        return None
    normalized = "".join(ch if ch.isalnum() else "_" for ch in text.lower())
    if "production" in normalized or normalized in ("prod", "pv", "solar"):
        return "production"
    if "consumption" in normalized or normalized in ("cons", "load", "site_load"):
        return "consumption"
    return None


_NON_ATTR_CHARS_RE = re.compile(r"[^a-z0-9]+")


_SYSTEM_CONTROLLER_TERMINAL_DESCRIPTIONS: dict[str, str] = {
    "mid": "Microgrid interconnection device line",
    "mid_n": "Microgrid interconnection device neutral",
    "der_l1": "Distributed energy resource line 1",
    "der_l2": "Distributed energy resource line 2",
    "der_l3": "Distributed energy resource line 3",
    "der_n": "Distributed energy resource neutral",
    "nc1": "Load-control relay NC1 (normally closed)",
    "nc2": "Load-control relay NC2 (normally closed)",
    "no1": "Load-control relay NO1 (normally open)",
    "no2": "Load-control relay NO2 (normally open)",
}


_SYSTEM_CONTROLLER_TERMINAL_KEYS: dict[str, str] = {
    "MID": "mid",
    "MID_N": "mid_n",
    "DER_L1": "der_l1",
    "DER_L2": "der_l2",
    "DER_L3": "der_l3",
    "DER_N": "der_n",
    "NC1": "nc1",
    "NC2": "nc2",
    "NO1": "no1",
    "NO2": "no2",
}


def _gateway_attr_key(key: object) -> str | None:
    text = _gateway_clean_text(key)
    if not text:
        return None
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", text)
    normalized = _NON_ATTR_CHARS_RE.sub("_", normalized.lower()).strip("_")
    return normalized or None


def _gateway_flat_member_attributes(
    member: dict[str, object],
    *,
    skip_keys: set[str] | None = None,
) -> dict[str, object]:
    flattened: dict[str, object] = {}
    skip = skip_keys or set()
    for raw_key, raw_value in member.items():
        key = _gateway_attr_key(raw_key)
        if not key or key in skip:
            continue
        if raw_value is None:
            continue
        if isinstance(raw_value, (str, int, float, bool)):
            value = raw_value
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
            flattened[key] = value
    return flattened


def _gateway_terminal_descriptions(
    member: dict[str, object] | None,
) -> dict[str, str]:
    if not isinstance(member, dict):
        return {}
    descriptions: dict[str, str] = {}
    for raw_key, raw_value in member.items():
        key = _gateway_terminal_key(raw_key)
        if key is None:
            continue
        if raw_value is None:
            continue
        if isinstance(raw_value, str) and not raw_value.strip():
            continue
        descriptions[key] = _SYSTEM_CONTROLLER_TERMINAL_DESCRIPTIONS[key]
    return descriptions


def _gateway_terminal_key(raw_key: object) -> str | None:
    text = _gateway_clean_text(raw_key)
    if not text:
        return None
    normalized = re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")
    return _SYSTEM_CONTROLLER_TERMINAL_KEYS.get(normalized)


def _gateway_terminal_values(member: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(member, dict):
        return {}
    values: dict[str, object] = {}
    for raw_key, raw_value in member.items():
        key = _gateway_terminal_key(raw_key)
        if key is None or raw_value is None:
            continue
        if isinstance(raw_value, str):
            value = raw_value.strip()
            if not value:
                continue
            values[key] = value
            continue
        if isinstance(raw_value, (int, float, bool)):
            values[key] = raw_value
    return values


def _gateway_iq_energy_router_inventory_buckets(
    payload: object,
) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    wrapped = payload.get("value")
    if isinstance(wrapped, dict):
        wrapped_result = wrapped.get("result")
        if isinstance(wrapped_result, list):
            return [item for item in wrapped_result if isinstance(item, dict)]
    return []


def _gateway_iq_energy_router_identity(value: object) -> str | None:
    text = _gateway_clean_text(value)
    if not text:
        return None
    normalized = _NON_ATTR_CHARS_RE.sub("_", text.lower()).strip("_")
    return normalized or None


def _gateway_iq_energy_router_member_key(
    member: dict[str, object],
    *,
    fallback_index: int,
) -> str:
    for key in ("device-uid", "device_uid", "uid"):
        identity = _gateway_iq_energy_router_identity(member.get(key))
        if identity:
            return identity
    name_identity = _gateway_iq_energy_router_identity(member.get("name"))
    if name_identity:
        return f"name_{name_identity}"
    return f"index_{fallback_index}"


def _gateway_iq_energy_router_records(
    coord: EnphaseCoordinator,
) -> list[dict[str, object]]:
    records_getter = coord.inventory_view.gateway_iq_energy_router_summary_records
    if callable(records_getter):
        try:
            records = records_getter()
        except Exception:  # noqa: BLE001
            records = None
        if isinstance(records, list):
            return [dict(record) for record in records if isinstance(record, dict)]
    router_members: list[dict[str, object]] = []
    restored_records = coord.inventory_view.gateway_iq_energy_router_records
    if callable(restored_records):
        try:
            router_members = [
                dict(member)
                for member in restored_records()
                if isinstance(member, dict)
            ]
        except Exception:  # noqa: BLE001
            router_members = []
    grouped_fetch = getattr(coord, "_hems_group_members", None)
    if not router_members and callable(grouped_fetch):
        for member in grouped_fetch("gateway"):
            device_type = _gateway_clean_text(
                member.get("device-type")
                if member.get("device-type") is not None
                else member.get("device_type")
            )
            if (device_type or "").upper() != "IQ_ENERGY_ROUTER":
                continue
            router_members.append(dict(member))
    elif not router_members:
        payload = getattr(coord, "_devices_inventory_payload", None)
        buckets = _gateway_iq_energy_router_inventory_buckets(payload)
        for bucket in buckets:
            raw_type = (
                bucket.get("type")
                if bucket.get("type") is not None
                else (
                    bucket.get("deviceType")
                    if bucket.get("deviceType") is not None
                    else bucket.get("device_type")
                )
            )
            type_key = _gateway_iq_energy_router_identity(raw_type)
            if not type_key:
                continue
            if type_key.replace("_", "") != "hemsdevices":
                continue
            devices = bucket.get("devices")
            if not isinstance(devices, list):
                continue
            for grouped in devices:
                if not isinstance(grouped, dict):
                    continue
                gateways = grouped.get("gateway")
                if not isinstance(gateways, list):
                    continue
                for member in gateways:
                    if not isinstance(member, dict):
                        continue
                    if member_is_retired(member):
                        continue
                    device_type = _gateway_clean_text(
                        member.get("device-type")
                        if member.get("device-type") is not None
                        else member.get("device_type")
                    )
                    if (device_type or "").upper() != "IQ_ENERGY_ROUTER":
                        continue
                    router_members.append(dict(member))

    router_records: list[dict[str, object]] = []
    key_counts: dict[str, int] = {}
    for member in router_members:
        index = len(router_records) + 1
        base_key = _gateway_iq_energy_router_member_key(member, fallback_index=index)
        key_counts[base_key] = key_counts.get(base_key, 0) + 1
        key = base_key
        if key_counts[base_key] > 1:
            key = f"{base_key}_{key_counts[base_key]}"
        router_records.append(
            {
                "key": key,
                "index": index,
                "name": _gateway_clean_text(member.get("name"))
                or f"IQ Energy Router_{index}",
                "member": dict(member),
            }
        )
    return router_records


def _gateway_iq_energy_router_record(
    coord: EnphaseCoordinator,
    router_key: object,
) -> dict[str, object] | None:
    key = _gateway_clean_text(router_key)
    if not key:
        return None
    record_getter = getattr(coord, "gateway_iq_energy_router_record", None)
    if callable(record_getter):
        try:
            record = record_getter(key)
        except Exception:  # noqa: BLE001
            record = None
        if isinstance(record, dict):
            return record
    for record in _gateway_iq_energy_router_records(coord):
        if _gateway_clean_text(record.get("key")) == key:
            return record
    return None


def _gateway_iq_energy_router_last_reported(
    member: dict[str, object] | None,
) -> datetime | None:
    if not isinstance(member, dict):
        return None
    for key in ("last-report", *list(_GATEWAY_LAST_REPORT_KEYS)):
        parsed = _gateway_parse_timestamp(member.get(key))
        if parsed is not None:
            return parsed
    return None


def _gateway_meter_member(
    coord: EnphaseCoordinator, meter_kind: str
) -> dict[str, object] | None:
    bucket = coord.inventory_view.type_bucket("envoy") or {}
    members = bucket.get("devices")
    dashboard_detail = None
    detail_getter = getattr(coord, "system_dashboard_meter_detail", None)
    if callable(detail_getter):
        dashboard_detail = detail_getter(meter_kind)
    if not isinstance(members, list):
        return dict(dashboard_detail) if isinstance(dashboard_detail, dict) else None
    for member in members:
        if not isinstance(member, dict):
            continue
        kind = _gateway_channel_type_kind(member.get("channel_type"))
        if kind is None:
            name = _gateway_clean_text(member.get("name")) or ""
            if "production" in name.lower():
                kind = "production"
            elif "consumption" in name.lower():
                kind = "consumption"
        if kind == meter_kind:
            merged = dict(member)
            if isinstance(dashboard_detail, dict):
                for key, value in dashboard_detail.items():
                    if value is None:
                        continue
                    if merged.get(key) in (None, "") or key in (
                        "meter_state",
                        "config_type",
                        "meter_type",
                    ):
                        merged[key] = value
            return merged
    return dict(dashboard_detail) if isinstance(dashboard_detail, dict) else None


def _gateway_meter_status_text(
    member: dict[str, object] | None, hass: object | None = None
) -> str | None:
    if not isinstance(member, dict):
        return None
    status_text = _gateway_clean_text(member.get("statusText"))
    if status_text:
        return status_label(status_text, hass=hass) or status_text
    status_raw = _gateway_clean_text(member.get("status"))
    if not status_raw:
        return None
    return status_label(status_raw, hass=hass) or friendly_status_text(status_raw)


def _gateway_meter_last_reported(member: dict[str, object] | None) -> datetime | None:
    if not isinstance(member, dict):
        return None
    for key in _GATEWAY_LAST_REPORT_KEYS:
        parsed = _gateway_parse_timestamp(member.get(key))
        if parsed is not None:
            return parsed
    return None


def _gateway_system_controller_member(
    coord: EnphaseCoordinator,
) -> dict[str, object] | None:
    bucket = coord.inventory_view.type_bucket("envoy") or {}
    members = bucket.get("devices")
    if not isinstance(members, list):
        return None
    for member in members:
        if not isinstance(member, dict):
            continue
        channel_type = (_gateway_clean_text(member.get("channel_type")) or "").lower()
        if channel_type in ("enpower", "system_controller", "systemcontroller"):
            return dict(member)
        name = (_gateway_clean_text(member.get("name")) or "").lower()
        if "system controller" in name:
            return dict(member)
    return None


def _is_dry_contact_type_key(type_key: object) -> bool:
    return is_dry_contact_type_key(type_key)


def _gateway_member_is_dry_contact(member: object) -> bool:
    if not isinstance(member, dict):
        return False
    candidates = (
        member.get("channel_type"),
        member.get("channelType"),
        member.get("meter_type"),
        member.get("device_type"),
        member.get("device-type"),
        member.get("name"),
    )
    for candidate in candidates:
        if _is_dry_contact_type_key(candidate):
            return True
    return False


def _gateway_dry_contact_members(
    coord: EnphaseCoordinator,
) -> list[dict[str, object]]:
    members_out: list[dict[str, object]] = []
    seen_keys: set[str] = set()

    def _identity(member: dict[str, object]) -> str | None:
        device_uid = _gateway_clean_text(
            member.get("device_uid")
            if member.get("device_uid") is not None
            else member.get("device-uid")
        )
        uid = _gateway_clean_text(member.get("uid"))
        contact_id = _gateway_clean_text(
            member.get("contact_id")
            if member.get("contact_id") is not None
            else (
                member.get("contactId")
                if member.get("contactId") is not None
                else member.get("id")
            )
        )
        channel_type = _gateway_clean_text(
            member.get("channel_type")
            if member.get("channel_type") is not None
            else (
                member.get("channelType")
                if member.get("channelType") is not None
                else member.get("meter_type")
            )
        )
        serial_number = _gateway_clean_text(
            member.get("serial_number")
            if member.get("serial_number") is not None
            else (
                member.get("serial")
                if member.get("serial") is not None
                else member.get("serialNumber")
            )
        )

        if device_uid:
            if contact_id or channel_type:
                return "|".join(
                    part
                    for part in (
                        f"device_uid:{device_uid.lower()}",
                        (
                            f"contact_id:{contact_id.lower()}"
                            if contact_id is not None
                            else None
                        ),
                        (
                            f"channel_type:{channel_type.lower()}"
                            if channel_type is not None
                            else None
                        ),
                    )
                    if part is not None
                )
            return f"device_uid:{device_uid.lower()}"
        if uid:
            if contact_id or channel_type:
                return "|".join(
                    part
                    for part in (
                        f"uid:{uid.lower()}",
                        (
                            f"contact_id:{contact_id.lower()}"
                            if contact_id is not None
                            else None
                        ),
                        (
                            f"channel_type:{channel_type.lower()}"
                            if channel_type is not None
                            else None
                        ),
                    )
                    if part is not None
                )
            return f"uid:{uid.lower()}"
        if contact_id and channel_type:
            return (
                f"contact_id:{contact_id.lower()}|channel_type:{channel_type.lower()}"
            )
        if channel_type and serial_number:
            return f"channel_type:{channel_type.lower()}|serial_number:{serial_number.lower()}"
        if contact_id and serial_number:
            return (
                f"contact_id:{contact_id.lower()}|serial_number:{serial_number.lower()}"
            )
        if contact_id:
            return f"contact_id:{contact_id.lower()}"
        if channel_type:
            return f"channel_type:{channel_type.lower()}"
        if serial_number:
            return f"serial_number:{serial_number.lower()}"
        return None

    def _fingerprint(member: dict[str, object]) -> str | None:
        parts: list[tuple[str, str]] = []
        for raw_key in sorted(member):
            key = _gateway_attr_key(raw_key)
            if not key:
                continue
            raw_value = member.get(raw_key)
            if raw_value is None:
                continue
            if not isinstance(raw_value, (str, int, float, bool)):
                continue
            if isinstance(raw_value, str):
                value = raw_value.strip()
                if not value:
                    continue
            else:
                value = str(raw_value)
            parts.append((key, value))
        if not parts:
            return None
        return repr(tuple(parts))

    def _append_member(raw_member: object) -> None:
        if not isinstance(raw_member, dict):
            return
        if member_is_retired(raw_member):
            return
        member = dict(raw_member)
        identity = _identity(member)
        fingerprint = _fingerprint(member)
        key = (
            f"id:{identity}"
            if identity is not None
            else (
                f"fp:{fingerprint}"
                if fingerprint is not None
                else f"idx:{len(members_out)}"
            )
        )
        if key in seen_keys:
            return
        seen_keys.add(key)
        members_out.append(member)

    envoy_bucket = coord.inventory_view.type_bucket("envoy") or {}
    envoy_members = envoy_bucket.get("devices")
    if isinstance(envoy_members, list):
        for member in envoy_members:
            if _gateway_member_is_dry_contact(member):
                _append_member(member)

    buckets = getattr(coord, "_type_device_buckets", None)
    if isinstance(buckets, dict):
        for type_key, bucket in buckets.items():
            if not _is_dry_contact_type_key(type_key):
                continue
            if not isinstance(bucket, dict):
                continue
            bucket_members = bucket.get("devices")
            if not isinstance(bucket_members, list):
                continue
            for member in bucket_members:
                _append_member(member)

    members_out.sort(
        key=lambda member: (
            _identity(member) or "",
            _gateway_clean_text(
                member.get("channel_type")
                if member.get("channel_type") is not None
                else member.get("channelType")
            )
            or "",
            _gateway_clean_text(
                member.get("serial_number")
                if member.get("serial_number") is not None
                else member.get("serial")
            )
            or "",
            _gateway_clean_text(member.get("name")) or "",
        )
    )
    return members_out


class EnphaseSystemControllerInventorySensor(_SiteBaseEntity):
    _attr_translation_key = "system_controller_inventory"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "last_reported_utc",
            "last_reported",
            "last_report",
            "last_reported_at",
        }
    )

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "type_enpower_inventory",
            "System Controller",
            type_key="envoy",
        )

    def _member(self) -> dict[str, object] | None:
        return _gateway_system_controller_member(self._coord)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._member() is not None

    @property
    def native_value(self) -> Any:
        return _gateway_meter_status_text(
            self._member(), getattr(self, "hass", None) or self._coord.hass
        )

    @property
    def extra_state_attributes(self) -> Any:
        member = self._member()
        if not isinstance(member, dict):
            return {}
        last_reported = _gateway_meter_last_reported(member)
        terminal_values = _gateway_terminal_values(member)
        terminal_descriptions = _gateway_terminal_descriptions(member)
        attrs: dict[str, object] = {
            "name": _gateway_clean_text(member.get("name")) or "System Controller",
            "status_text": _gateway_meter_status_text(
                member, getattr(self, "hass", None) or self._coord.hass
            ),
            "status_raw": _gateway_clean_text(
                member.get("statusText")
                if member.get("statusText") is not None
                else member.get("status")
            ),
            "connected": _gateway_optional_bool(member.get("connected")),
            "channel_type": _gateway_clean_text(member.get("channel_type")),
            "serial_number": _gateway_clean_text(member.get("serial_number")),
            "last_reported_utc": (
                last_reported.isoformat() if last_reported is not None else None
            ),
        }
        attrs.update(terminal_values)
        if terminal_descriptions:
            attrs["terminal_descriptions"] = terminal_descriptions
        attrs.update(
            _gateway_flat_member_attributes(
                member,
                skip_keys={
                    "name",
                    "status_text",
                    "status_raw",
                    "connected",
                    "channel_type",
                    "serial_number",
                    "last_reported_utc",
                    "status",
                    "statusText",
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                },
            )
        )
        return attrs


class EnphaseDryContactsInventorySensor(_SiteBaseEntity):
    _attr_translation_key = "dry_contacts"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "members",
            "contacts",
            "unmatched_settings",
            "last_reported_utc",
        }
    )

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "dry_contacts_inventory",
            "Dry Contacts",
            type_key="envoy",
        )

    def _members(self) -> list[dict[str, object]]:
        return _gateway_dry_contact_members(self._coord)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return bool(self._members())

    @property
    def native_value(self) -> Any:
        status_values: dict[str, str] = {}
        for member in self._members():
            status_text = _gateway_meter_status_text(
                member, getattr(self, "hass", None) or self._coord.hass
            )
            if status_text:
                normalized = status_text.casefold()
                if normalized not in status_values:
                    status_values[normalized] = status_text
        if not status_values:
            return None
        unique_values = [status_values[key] for key in sorted(status_values)]
        if len(unique_values) == 1:
            return unique_values[0]
        return " | ".join(unique_values)

    @property
    def extra_state_attributes(self) -> Any:
        members = self._members()
        if not members:
            return {}
        settings_matches, unmatched_settings = self._coord.dry_contact_settings_matches(
            members
        )
        dry_contact_settings_supported = self._coord.dry_contact_settings_supported
        latest_reported: datetime | None = None
        visible_count = 0
        visible_seen = False
        enabled_count = 0
        enabled_seen = False
        in_use_count = 0
        in_use_seen = False
        contacts: list[dict[str, object]] = []
        for index, member in enumerate(members, start=1):
            member_last_reported = _gateway_meter_last_reported(member)
            if member_last_reported is None:
                pass
            elif latest_reported is None or member_last_reported > latest_reported:
                latest_reported = member_last_reported
            visible = _gateway_optional_bool(
                member.get("visible")
                if member.get("visible") is not None
                else (
                    member.get("is_visible")
                    if member.get("is_visible") is not None
                    else member.get("isVisible")
                )
            )
            if visible is not None:
                visible_seen = True
                if visible:
                    visible_count += 1
            enabled = _gateway_optional_bool(
                member.get("enabled")
                if member.get("enabled") is not None
                else (
                    member.get("is_enabled")
                    if member.get("is_enabled") is not None
                    else member.get("isEnabled")
                )
            )
            if enabled is not None:
                enabled_seen = True
                if enabled:
                    enabled_count += 1
            in_use = _gateway_optional_bool(
                member.get("in_use")
                if member.get("in_use") is not None
                else (
                    member.get("inUse")
                    if member.get("inUse") is not None
                    else (
                        member.get("used")
                        if member.get("used") is not None
                        else member.get("active")
                    )
                )
            )
            if in_use is not None:
                in_use_seen = True
                if in_use:
                    in_use_count += 1
            status_raw = _gateway_clean_text(
                member.get("statusText")
                if member.get("statusText") is not None
                else member.get("status")
            )
            terminal_values = _gateway_terminal_values(member)
            terminal_descriptions = _gateway_terminal_descriptions(member)
            contact: dict[str, object] = {
                "index": index,
                "name": _gateway_clean_text(member.get("name"))
                or f"Dry Contact {index}",
                "status_text": _gateway_meter_status_text(
                    member, getattr(self, "hass", None) or self._coord.hass
                ),
                "status_raw": status_raw,
                "connected": _gateway_optional_bool(member.get("connected")),
                "channel_type": _gateway_clean_text(
                    member.get("channel_type")
                    if member.get("channel_type") is not None
                    else member.get("channelType")
                ),
                "serial_number": _gateway_clean_text(
                    member.get("serial_number")
                    if member.get("serial_number") is not None
                    else member.get("serial")
                ),
                "visible": visible,
                "enabled": enabled,
                "in_use": in_use,
                "properties": dict(member),
                **terminal_values,
                "terminal_descriptions": terminal_descriptions,
            }
            matched_settings = (
                settings_matches[index - 1]
                if (index - 1) < len(settings_matches)
                else None
            )
            if isinstance(matched_settings, dict):
                for key in (
                    "configured_name",
                    "override_supported",
                    "override_active",
                    "control_mode",
                    "polling_interval_seconds",
                    "soc_threshold",
                    "soc_threshold_min",
                    "soc_threshold_max",
                ):
                    value = matched_settings.get(key)
                    if value is not None:
                        contact[key] = value
                schedule_windows = matched_settings.get("schedule_windows")
                if isinstance(schedule_windows, list) and schedule_windows:
                    contact["schedule_windows"] = [
                        dict(window) if isinstance(window, dict) else window
                        for window in schedule_windows
                    ]
            contacts.append(contact)

        attrs: dict[str, object] = {
            "name": "Dry Contacts",
            "member_count": len(members),
            "status_text": self.native_value,
            "last_reported_utc": (
                latest_reported.isoformat() if latest_reported is not None else None
            ),
            "contacts": contacts,
            "members": [dict(member) for member in members],
            "dry_contact_settings_supported": dry_contact_settings_supported,
            "dry_contact_settings_contact_count": len(
                self._coord.dry_contact_settings_entries()
            ),
        }
        if unmatched_settings:
            attrs["unmatched_settings"] = [
                dict(entry) if isinstance(entry, dict) else entry
                for entry in unmatched_settings
            ]
        if visible_seen:
            attrs["visible_contact_count"] = visible_count
        if enabled_seen:
            attrs["enabled_contact_count"] = enabled_count
        if in_use_seen:
            attrs["in_use_contact_count"] = in_use_count
        if len(members) == 1:
            member = members[0]
            matched_settings = settings_matches[0] if settings_matches else None
            attrs.update(
                {
                    "channel_type": _gateway_clean_text(
                        member.get("channel_type")
                        if member.get("channel_type") is not None
                        else member.get("channelType")
                    ),
                    "serial_number": _gateway_clean_text(
                        member.get("serial_number")
                        if member.get("serial_number") is not None
                        else member.get("serial")
                    ),
                    "connected": _gateway_optional_bool(member.get("connected")),
                    "status_raw": _gateway_clean_text(
                        member.get("statusText")
                        if member.get("statusText") is not None
                        else member.get("status")
                    ),
                }
            )
            terminal_descriptions = _gateway_terminal_descriptions(member)
            attrs.update(_gateway_terminal_values(member))
            if terminal_descriptions:
                attrs["terminal_descriptions"] = terminal_descriptions
            if isinstance(matched_settings, dict):
                for key in (
                    "configured_name",
                    "override_supported",
                    "override_active",
                    "control_mode",
                    "polling_interval_seconds",
                    "soc_threshold",
                    "soc_threshold_min",
                    "soc_threshold_max",
                ):
                    value = matched_settings.get(key)
                    if value is not None:
                        attrs[key] = value
                schedule_windows = matched_settings.get("schedule_windows")
                if isinstance(schedule_windows, list) and schedule_windows:
                    attrs["schedule_windows"] = [
                        dict(window) if isinstance(window, dict) else window
                        for window in schedule_windows
                    ]
            attrs.update(
                _gateway_flat_member_attributes(
                    member,
                    skip_keys={
                        "name",
                        "status",
                        "status_text",
                        "status_raw",
                        "channel_type",
                        "serial_number",
                        "connected",
                        "last_reported_utc",
                        "last_report",
                        "last_reported",
                        "last_reported_at",
                        "members",
                    },
                )
            )
        return attrs


class _EnphaseGatewayMeterSensor(_SiteBaseEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "meter_attributes",
            "last_reported_utc",
        }
    )

    def __init__(
        self,
        coord: EnphaseCoordinator,
        meter_kind: str,
        label: str,
    ) -> None:
        super().__init__(
            coord,
            f"gateway_{meter_kind}_meter",
            label,
            type_key="envoy",
        )
        self._meter_kind = meter_kind

    def _member(self) -> dict[str, object] | None:
        return _gateway_meter_member(self._coord, self._meter_kind)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._member() is not None

    @property
    def native_value(self) -> Any:
        return _gateway_meter_status_text(
            self._member(), getattr(self, "hass", None) or self._coord.hass
        )

    @property
    def extra_state_attributes(self) -> Any:
        member = self._member()
        if not isinstance(member, dict):
            return {}
        last_reported = _gateway_meter_last_reported(member)
        status_text = _gateway_meter_status_text(
            member, getattr(self, "hass", None) or self._coord.hass
        )
        attrs: dict[str, object] = {
            "meter_name": _gateway_clean_text(member.get("name")),
            "meter_type": self._meter_kind,
            "dashboard_meter_type": _gateway_clean_text(member.get("meter_type")),
            "channel_type": _gateway_clean_text(member.get("channel_type")),
            "serial_number": _gateway_clean_text(member.get("serial_number")),
            "connected": _gateway_optional_bool(member.get("connected")),
            "status_text": status_text,
            "status_raw": _gateway_clean_text(
                member.get("statusText")
                if member.get("statusText") is not None
                else member.get("status")
            ),
            "last_reported_utc": (
                last_reported.isoformat() if last_reported is not None else None
            ),
            "meter_state": _gateway_clean_text(member.get("meter_state")),
            "config_type": _gateway_clean_text(member.get("config_type")),
            "ip_address": _gateway_clean_text(
                member.get("ip")
                if member.get("ip") is not None
                else member.get("ip_address")
            ),
            "meter_attributes": dict(member),
        }
        attrs.update(
            _gateway_flat_member_attributes(
                member,
                skip_keys={
                    "name",
                    "channel_type",
                    "serial_number",
                    "connected",
                    "status_text",
                    "status_raw",
                    "meter_type",
                    "meter_state",
                    "config_type",
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                    "ip",
                    "ip_address",
                },
            )
        )
        return attrs


class EnphaseGatewayProductionMeterSensor(_EnphaseGatewayMeterSensor):
    _attr_translation_key = "gateway_production_meter"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "production", "Production Meter")


class EnphaseGatewayConsumptionMeterSensor(_EnphaseGatewayMeterSensor):
    _attr_translation_key = "gateway_consumption_meter"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "consumption", "Consumption Meter")


class EnphaseGatewayIQEnergyRouterSensor(_SiteBaseEntity):
    _attr_translation_key = "gateway_iq_energy_router"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"last_reported_utc"}
    )

    def __init__(
        self,
        coord: EnphaseCoordinator,
        router_key: str,
        index: int,
    ) -> None:
        normalized_key = _gateway_iq_energy_router_identity(router_key) or str(
            router_key
        )
        super().__init__(
            coord,
            f"gateway_iq_energy_router_{normalized_key}",
            f"IQ Energy Router_{index}",
            type_key="envoy",
        )
        self._router_key = normalized_key
        self._index = max(1, int(index))
        self._attr_translation_placeholders = {"index": str(self._index)}

    def _member(self) -> dict[str, object] | None:
        record = _gateway_iq_energy_router_record(self._coord, self._router_key)
        if not isinstance(record, dict):
            return None
        member = record.get("member")
        if not isinstance(member, dict):
            return None
        return dict(member)

    @property
    def name(self) -> str | None:
        member = self._member()
        member_name = (
            _gateway_clean_text(member.get("name"))
            if isinstance(member, dict)
            else None
        )
        if member_name:
            return member_name
        # Prefer translated fallback names when this entity is platform-attached.
        if getattr(self, "platform", None) is not None:
            try:
                translated_name = super().name
            except Exception:  # noqa: BLE001
                translated_name = None
            if translated_name:
                return translated_name  # type: ignore[no-any-return]
        return f"IQ Energy Router_{self._index}"

    @property
    def available(self) -> bool:
        if self._member() is None:
            return False
        if self._coord.last_success_utc is not None:
            return True
        return CoordinatorEntity.available.fget(self)  # type: ignore[no-any-return]

    @property
    def native_value(self) -> Any:
        return _gateway_meter_status_text(
            self._member(), getattr(self, "hass", None) or self._coord.hass
        )

    @property
    def extra_state_attributes(self) -> Any:
        member = self._member()
        if not isinstance(member, dict):
            return {}
        status_text = _gateway_meter_status_text(
            member, getattr(self, "hass", None) or self._coord.hass
        )
        last_reported = _gateway_iq_energy_router_last_reported(member)
        attrs: dict[str, object] = {
            "name": _gateway_clean_text(member.get("name"))
            or f"IQ Energy Router_{self._index}",
            "status_text": status_text,
            "status_raw": _gateway_clean_text(
                member.get("statusText")
                if member.get("statusText") is not None
                else member.get("status")
            ),
            "device_type": _gateway_clean_text(
                member.get("device-type")
                if member.get("device-type") is not None
                else member.get("device_type")
            ),
            "uid": _gateway_clean_text(member.get("uid")),
            "device_uid": _gateway_clean_text(
                member.get("device-uid")
                if member.get("device-uid") is not None
                else member.get("device_uid")
            ),
            "make": _gateway_clean_text(member.get("make")),
            "model": _gateway_clean_text(member.get("model")),
            "pairing_status": _gateway_clean_text(
                member.get("pairing-status")
                if member.get("pairing-status") is not None
                else member.get("pairing_status")
            ),
            "device_state": _gateway_clean_text(
                member.get("device-state")
                if member.get("device-state") is not None
                else member.get("device_state")
            ),
            "iqer_uid": _gateway_clean_text(
                member.get("iqer-uid")
                if member.get("iqer-uid") is not None
                else member.get("iqer_uid")
            ),
            "hems_device_id": _gateway_clean_text(
                member.get("hems-device-id")
                if member.get("hems-device-id") is not None
                else member.get("hems_device_id")
            ),
            "hems_device_facet_id": _gateway_clean_text(
                member.get("hems-device-facet-id")
                if member.get("hems-device-facet-id") is not None
                else member.get("hems_device_facet_id")
            ),
            "last_reported_utc": (
                last_reported.isoformat() if last_reported is not None else None
            ),
        }
        attrs.update(
            _gateway_flat_member_attributes(
                member,
                skip_keys={
                    "name",
                    "status",
                    "status_text",
                    "status_raw",
                    "device_type",
                    "uid",
                    "device_uid",
                    "make",
                    "model",
                    "pairing_status",
                    "device_state",
                    "iqer_uid",
                    "hems_device_id",
                    "hems_device_facet_id",
                    "last_reported_utc",
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                    "last_reported_at_utc",
                },
            )
        )
        return attrs


class EnphaseGatewayConnectivityStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "gateway_connectivity_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "latest_reported_utc",
            "latest_reported_device",
            "property_keys",
            "primary_gateway_serial",
            "default_gateway_serial",
            "preferred_gateway_serial",
        }
    )

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "gateway_connectivity_status",
            "Gateway Status",
            type_key="envoy",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = _gateway_inventory_snapshot(self._coord)
        if int(snapshot.get("total_devices", 0) or 0) > 0:  # type: ignore[call-overload]
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self) -> Any:
        return _title_case_status(
            _gateway_connectivity_state(_gateway_inventory_snapshot(self._coord)),
            getattr(self, "hass", None) or self._coord.hass,
        )

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = _gateway_inventory_snapshot(self._coord)
        attributes = {
            "total_devices": snapshot.get("total_devices"),
            "connected_devices": snapshot.get("connected_devices"),
            "disconnected_devices": snapshot.get("disconnected_devices"),
            "unknown_connection_devices": snapshot.get("unknown_connection_devices"),
            "status_counts": snapshot.get("status_counts"),
            "status_summary": snapshot.get("status_summary"),
            "model_summary": snapshot.get("model_summary"),
            "firmware_summary": snapshot.get("firmware_summary"),
            "ip_address": snapshot.get("ip_address"),
            "latest_reported_utc": snapshot.get("latest_reported_utc"),
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "property_keys": snapshot.get("property_keys"),
        }
        phase_map_keys = (
            "gateway_count",
            "multi_gateway",
            "primary_gateway_serial",
            "default_gateway_serial",
            "preferred_gateway_serial",
            "preferred_gateway_phase_count",
            "split_phase_gateway_count",
            "three_phase_gateway_count",
            "production_only_gateway_count",
            "consumption_only_gateway_count",
            "storage_gateway_count",
        )
        attributes.update(
            {key: snapshot[key] for key in phase_map_keys if key in snapshot}
        )
        return attributes


class EnphaseGatewayLastReportedSensor(_SiteBaseEntity):
    _attr_translation_key = "gateway_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "gateway_last_reported",
            "Gateway Last Reported",
            type_key="envoy",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = _gateway_inventory_snapshot(self._coord)
        return snapshot.get("latest_reported") is not None

    @property
    def native_value(self) -> Any:
        snapshot = _gateway_inventory_snapshot(self._coord)
        return snapshot.get("latest_reported")

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = _gateway_inventory_snapshot(self._coord)
        return {
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "without_last_report_count": snapshot.get("without_last_report_count"),
            "total_devices": snapshot.get("total_devices"),
            "status_summary": snapshot.get("status_summary"),
        }
