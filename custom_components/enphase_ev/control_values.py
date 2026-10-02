"""Read-facing control contracts; requested intent is never a confirmed value."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import time as dt_time
from typing import Any

from .const import DEFAULT_CHARGE_LEVEL_SETTING, SAVINGS_OPERATION_MODE_SUBTYPE
from .schedule import normalize_slot_payload
from .schedule_editor_helpers import normalize_days
from .runtime_helpers import coerce_optional_int
from .scalar_helpers import coerce_optional_bool

# Attribute names deliberately use configured/readback values, not selected or
# optimistic projections. Several controls share the profile transaction.
SCALAR_CONTROLS = {
    "system_profile": ("profile_key", "_battery_profile"),
    "battery_reserve": ("reserve", "_battery_backup_percentage"),
    "savings_use_battery_after_peak": ("enabled", "savings_use_battery_after_peak"),
    "charge_from_grid": ("enabled", "_battery_charge_from_grid"),
    "power_match": ("enabled", "battery_power_match_enabled"),
    "battery_shutdown_level": ("level", "battery_shutdown_level"),
    "storm_guard": ("enabled", "storm_guard_state"),
    "storm_evse": ("enabled", "_storm_evse_enabled"),
    "grid_mode": ("mode", "grid_mode"),
}
BATTERY_PROFILE_CONTROLS = (
    "system_profile",
    "battery_reserve",
    "savings_use_battery_after_peak",
)
FAMILY_CONTROLS = {
    "battery_settings": (
        *BATTERY_PROFILE_CONTROLS,
        "charge_from_grid",
        "power_match",
        "battery_shutdown_level",
        "cfg_schedule",
        "dtg_schedule",
        "rbd_schedule",
        "battery_schedule_create",
        "battery_schedule_update",
        "battery_schedule_delete",
    ),
    "storm_guard": (*BATTERY_PROFILE_CONTROLS, "storm_guard", "storm_evse"),
    "battery_schedules": (
        "battery_schedule_create",
        "battery_schedule_update",
        "battery_schedule_delete",
        "cfg_schedule",
        "dtg_schedule",
        "rbd_schedule",
    ),
    "grid_mode_status": ("grid_mode",),
    "tariff": ("tariff",),
    "battery_status": ("system_profile",),
}
RELATED_CONTROLS = {
    "battery_profile": BATTERY_PROFILE_CONTROLS,
    "battery_settings": (
        "charge_from_grid",
        "power_match",
        "battery_shutdown_level",
        "cfg_schedule",
        "dtg_schedule",
        "rbd_schedule",
        "battery_schedule_create",
        "battery_schedule_update",
        "battery_schedule_delete",
    ),
    "storm_guard": ("storm_guard", "storm_evse"),
    "evse_schedule": (
        "evse_schedule_enabled",
        "evse_schedule_replace",
        "evse_schedule_save",
        "evse_schedule_delete",
    ),
}
EVSE_CACHES = {
    "charge_mode": "_charge_mode_cache",
    "green_battery": "_green_battery_cache",
    "app_authentication": "_auth_settings_cache",
    "default_charge_level": "_charger_config_cache",
}


def confirmed_control_values(
    coord: Any, control: str, serial: str | None = None
) -> dict[str, object]:
    if control in SCALAR_CONTROLS:
        field, attr = SCALAR_CONTROLS[control]
        value = getattr(coord, attr, None)
        if control == "system_profile":
            value = getattr(coord, "_battery_live_profile", None) or value
        if control == "storm_guard":
            value = None if value is None else value == "enabled"
        if control == "savings_use_battery_after_peak":
            value = (
                getattr(coord, "_battery_operation_mode_sub_type", None)
                == SAVINGS_OPERATION_MODE_SUBTYPE
            )
        result = {field: value}
        if control == "system_profile":
            result["configured_profile"] = getattr(coord, "_battery_profile", None)
            result["operation_mode_sub_type"] = getattr(
                coord, "_battery_operation_mode_sub_type", None
            )
        if control in BATTERY_PROFILE_CONTROLS:
            result["profile_ready"] = not bool(
                getattr(coord, "battery_profile_pending", False)
            )
        return result
    if control in EVSE_CACHES:
        state = getattr(coord, "evse_state", None)
        cache = getattr(state, EVSE_CACHES[control], {}).get(serial)
        data = _serial_data(coord, serial)
        if control == "charge_mode":
            return {
                "mode": (
                    cache[0]
                    if cache
                    else data.get("charge_mode_pref") or data.get("charge_mode")
                )
            }
        if control == "green_battery":
            return {"enabled": cache[0] if cache else data.get("green_battery_enabled")}
        if control == "app_authentication":
            return {"enabled": cache[0] if cache else data.get("app_auth_enabled")}
        value = (
            cache[0].get(DEFAULT_CHARGE_LEVEL_SETTING)
            if cache
            else data.get("default_charge_level")
        )
        try:
            value = int(str(value))
        except (ValueError, TypeError):
            value = None
        return {"amps": value}
    if control == "charging":
        actual = getattr(coord, "_last_actual_charging", {}).get(serial)
        if actual is None:
            actual = _serial_data(coord, serial).get("charging")
        return {"enabled": actual}
    if control.endswith("_schedule"):
        prefix = control[:3]
        schedule_name = {
            "cfg": "charge_from_grid",
            "dtg": "discharge_to_grid",
            "rbd": "restrict_battery_discharge",
        }[prefix]
        return {
            "enabled": getattr(
                coord, f"battery_{schedule_name}_schedule_enabled", None
            ),
            "start_time": _time_value(
                getattr(coord, f"battery_{schedule_name}_start_time", None)
            ),
            "end_time": _time_value(
                getattr(coord, f"battery_{schedule_name}_end_time", None)
            ),
            "limit": getattr(coord, f"battery_{prefix}_schedule_limit", None),
            "schedule_ready": not bool(
                getattr(coord, f"battery_{prefix}_schedule_pending", False)
            ),
        }
    if control.startswith("battery_schedule_"):
        from .battery_schedule_editor import battery_schedule_inventory

        records = [record.as_dict() for record in battery_schedule_inventory(coord)]
        payload = getattr(coord, "_battery_schedules_payload", None)
        if isinstance(payload, dict):
            enabled_by_id: dict[str, bool] = {}
            for family in payload.values():
                if not isinstance(family, dict):
                    continue
                details = family.get("details")
                if not isinstance(details, list):
                    continue
                for detail in details:
                    if isinstance(detail, dict) and isinstance(
                        detail.get("isEnabled"), bool
                    ):
                        enabled_by_id[str(detail.get("scheduleId"))] = detail[
                            "isEnabled"
                        ]
            for record in records:
                schedule_id = str(record["schedule_id"])
                if schedule_id in enabled_by_id:
                    record["enabled"] = enabled_by_id[schedule_id]
        return {"schedules": records}
    if control.startswith("evse_schedule_"):
        sync = getattr(coord, "schedule_sync", None)
        return {"slots": deepcopy(getattr(sync, "_slot_cache", {}).get(serial, {}))}
    if control == "grid_profile":
        runtime = coord.grid_profile_runtime
        update = coord.control_updates.updates.get((control, None))
        serial = update.requested.get("gateway_serial") if update else None
        target = runtime.gateway_targets.get(serial)
        return {
            "profile_id": (
                target.current_profile_id if target else runtime.current_profile_id
            ),
            "gateway_serial": serial,
        }
    if control == "storm_alert_opt_out":
        return {"active_alerts": getattr(coord, "storm_alert_active", None)}
    if control == "tariff":
        billing = getattr(coord, "tariff_billing", None)
        return {
            "rates": coord.tariff_runtime._rate_signature(),
            "billing": dict(billing.attributes) if billing else None,
        }
    return {}


def _serial_data(coord: Any, serial: str | None) -> Mapping[str, Any]:
    data = getattr(coord, "data", None)
    record = data.get(serial) if isinstance(data, Mapping) else None
    return record if isinstance(record, Mapping) else {}


def _time_value(value: object) -> object:
    return value.strftime("%H:%M") if isinstance(value, dt_time) else value


def requested_control_values(
    control: str, arguments: dict[str, Any], coord: Any
) -> dict[str, object]:
    result: dict[str, object] = {
        key: _time_value(value) for key, value in arguments.items() if value is not None
    }
    if control == "system_profile":
        result["profile_key"] = coord.battery_runtime.normalize_battery_profile_key(
            arguments["profile_key"]
        )
    elif control == "charge_mode":
        result["mode"] = coord.evse_runtime.normalize_charge_mode_preference(
            arguments["mode"]
        )
    elif control == "grid_mode":
        result["mode"] = coord._normalize_grid_mode_value(arguments["mode"])
    if control in BATTERY_PROFILE_CONTROLS:
        result["profile_ready"] = True
    if control.endswith("_schedule"):
        for key in ("start", "end"):
            if key in result:
                result[key + "_time"] = result.pop(key)
        result["schedule_ready"] = True
    if control.startswith("battery_schedule_"):
        if "schedules" in arguments:
            return {
                "deleted_schedules": [
                    {
                        "deleted_schedule": str(schedule_id),
                        "deleted_schedule_type": str(schedule_type).lower(),
                    }
                    for schedule_id, schedule_type in arguments["schedules"]
                ]
            }
        if control.endswith("delete") or result.pop("is_deleted", False):
            return {
                "deleted_schedule": str(arguments["schedule_id"]),
                "deleted_schedule_type": str(
                    arguments.get("schedule_type", "cfg")
                ).lower(),
            }
        if "schedule_type" in result:
            result["schedule_type"] = str(result["schedule_type"]).lower()
        if "schedule_id" in result:
            result["schedule_id"] = str(result["schedule_id"])
        for field in ("start_time", "end_time"):
            if field in result:
                result[field] = str(result[field])[:5]
        if "days" in result:
            result["days"] = normalize_days(result["days"])
        result["schedule_ready"] = True
        enabled = result.pop("is_enabled", None)
        return {
            "schedule": result,
            **(
                {"family_enabled": {str(result.get("schedule_type", "cfg")): enabled}}
                if enabled is not None
                else {}
            ),
        }
    if control.startswith("evse_schedule_"):
        if control.endswith("delete"):
            return {"deleted_slot": str(arguments["slot_id"])}
        if control.endswith("enabled"):
            return {
                "slot": {
                    "id": str(arguments["slot_id"]),
                    "enabled": arguments["enabled"],
                }
            }
        return (
            {"slot": _requested_slot(arguments["slot"])}
            if "slot" in arguments
            else {
                "slots": [
                    _requested_slot(slot)
                    for slot in arguments["slots"]
                    if isinstance(slot, dict)
                ]
            }
        )
    if control == "storm_alert_opt_out":
        return {"active_alerts": False}
    if control == "tariff":
        # The tariff runtime replaces this with normalized target rates before
        # submitting. No raw provider payload enters the entity state machine.
        return {"write_requested": True}
    return result


def _requested_slot(slot: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_slot_payload(slot)
    normalized["scheduleType"] = str(normalized["scheduleType"]).strip().upper()
    if not normalized.get("id"):
        normalized.pop("id", None)
    for field in ("startTime", "endTime", "reminderTimeUtc"):
        value = normalized.get(field)
        if isinstance(value, str):
            normalized[field] = value[:5]
    if isinstance(normalized.get("days"), list):
        normalized["days"] = normalize_days(normalized["days"])
    return normalized


def _observed_slot(slot: object) -> object:
    if not isinstance(slot, dict):
        return slot
    normalized = _requested_slot(slot)
    for field in ("scheduleType", "days"):
        if field in slot and (
            slot[field] is None
            or (field == "days" and not isinstance(slot[field], list))
        ):
            normalized[field] = slot[field]
    # Write defaults are not evidence that a read returned those fields.
    return {key: value for key, value in normalized.items() if key in slot}


def _collection_matches(expected: list[Any], actual: list[Any]) -> bool:
    remaining = list(actual)
    for item in expected:
        match = next(
            (
                index
                for index, candidate in enumerate(remaining)
                if _subset(item, candidate)
            ),
            None,
        )
        if match is None:
            return False
        remaining.pop(match)
    return not remaining


def matches_requested(
    requested: dict[str, object], observed: dict[str, object]
) -> bool:
    records = observed.get("schedules")
    slots = observed.get("slots")
    if "deleted_schedules" in requested:
        deletions = requested["deleted_schedules"]
        return (
            isinstance(deletions, list)
            and bool(deletions)
            and all(
                isinstance(deletion, dict) and matches_requested(deletion, observed)
                for deletion in deletions
            )
        )
    if "deleted_schedule" in requested:
        family = requested.get("deleted_schedule_type")
        families = observed.get("schedule_families")
        if family is not None and (
            not isinstance(families, list) or family not in families
        ):
            return False
        return isinstance(records, list) and all(
            isinstance(record, dict)
            and record.get("schedule_id") != requested["deleted_schedule"]
            for record in records
        )
    if "schedule" in requested:
        companions = {
            key: value for key, value in requested.items() if key != "schedule"
        }
        return (
            isinstance(records, list)
            and _subset(companions, observed)
            and any(_subset(requested["schedule"], record) for record in records)
        )
    if "deleted_slot" in requested:
        return isinstance(slots, dict) and requested["deleted_slot"] not in slots
    if "slot" in requested:
        return isinstance(slots, dict) and any(
            _subset(requested["slot"], _observed_slot(slot)) for slot in slots.values()
        )
    if "slots" in requested:
        expected = requested["slots"]
        return (
            isinstance(slots, dict)
            and isinstance(expected, list)
            and _collection_matches(
                expected, [_observed_slot(slot) for slot in slots.values()]
            )
        )
    return bool(requested) and _subset(requested, observed)


def _subset(expected: object, actual: object) -> bool:
    if isinstance(expected, dict) and isinstance(actual, dict):
        return all(
            key in actual and _subset(value, actual[key])
            for key, value in expected.items()
        )
    return expected == actual


def _profile_feedback(
    coord: Any, control: str, data: dict[str, Any]
) -> dict[str, object]:
    """Read only fields present in this response, including explicit null values."""
    runtime = coord.battery_runtime
    ready = not bool(coord.battery_profile_pending)
    if control == "system_profile" and "profile" in data:
        profile = runtime.normalize_battery_profile_key(data["profile"])
        result: dict[str, object] = {
            "profile_key": getattr(coord, "battery_live_profile", None) or profile,
            "configured_profile": profile,
            "profile_ready": ready,
        }
        if "operationModeSubType" in data:
            result["operation_mode_sub_type"] = data["operationModeSubType"]
        return result
    if control == "battery_reserve" and "batteryBackupPercentage" in data:
        return {
            "reserve": coerce_optional_int(data["batteryBackupPercentage"]),
            "profile_ready": ready,
        }
    if control == "savings_use_battery_after_peak" and "operationModeSubType" in data:
        return {
            "enabled": coord._normalize_battery_sub_type(data["operationModeSubType"])
            == SAVINGS_OPERATION_MODE_SUBTYPE,
            "profile_ready": ready,
        }
    return {}


def _schedule_feedback(
    coord: Any, control: str, family: str, data: dict[str, Any]
) -> dict[str, object]:
    prefix = control[:3]
    result: dict[str, object] = {}
    if family == "battery_schedules":
        payload = data.get(prefix)
        if not isinstance(payload, dict):
            return {}
        details = payload.get("details")
        if "details" not in payload and payload.get("count") == 0:
            details = []
        if not isinstance(details, list):
            return {}
        schedule_id = getattr(coord, f"_battery_{prefix}_schedule_id", None)
        detail = next(
            (
                item
                for item in details
                if isinstance(item, dict) and str(item.get("scheduleId")) == schedule_id
            ),
            {},
        )
        for source, field in (
            ("startTime", "start_time"),
            ("endTime", "end_time"),
            ("limit", "limit"),
        ):
            if source in detail:
                value = detail[source]
                result[field] = (
                    coerce_optional_int(value) if field == "limit" else str(value)[:5]
                )
        status = detail.get("scheduleStatus") or payload.get("scheduleStatus")
        result["schedule_ready"] = str(status).lower() != "pending"
        return result
    # Settings carries control windows and enable flags, but no schedule limits
    # or schedule synchronization status. Those require the schedules endpoint.
    if prefix == "cfg":
        keys = (
            ("chargeFromGridScheduleEnabled", "enabled"),
            ("chargeBeginTime", "start_time"),
            ("chargeEndTime", "end_time"),
        )
    else:
        raw_control = data.get(prefix + "Control")
        if not isinstance(raw_control, dict):
            return {}
        data = raw_control
        keys = (
            ("enabled", "enabled"),
            ("startTime", "start_time"),
            ("endTime", "end_time"),
        )
    for source, field in keys:
        if source in data:
            value = data[source]
            result[field] = (
                coerce_optional_bool(value)
                if field == "enabled"
                else _time_value(
                    coord._minutes_of_day_to_time(coord.normalize_minutes_of_day(value))
                )
            )
    return result


def _schedule_inventory_feedback(data: dict[str, Any]) -> dict[str, object]:
    """Read only explicit family inventories and fields in this response."""
    records: list[dict[str, object]] = []
    families: list[str] = []
    valid_inventory = False
    for schedule_type in ("cfg", "dtg", "rbd"):
        family = data.get(schedule_type)
        if not isinstance(family, dict):
            continue
        details = family.get("details")
        if "details" not in family and family.get("count") == 0:
            details = []
        if not isinstance(details, list) or not all(
            isinstance(item, dict) and item.get("scheduleId") is not None
            for item in details
        ):
            continue
        valid_inventory = True
        family_status = str(family.get("scheduleStatus", "")).strip().lower()
        if family_status != "pending":
            families.append(schedule_type)
        for detail in details:
            record: dict[str, object] = {
                "schedule_id": str(detail["scheduleId"]),
                "schedule_type": schedule_type,
                "schedule_ready": str(detail.get("scheduleStatus") or family_status)
                .strip()
                .lower()
                != "pending",
            }
            for source, field in (
                ("startTime", "start_time"),
                ("endTime", "end_time"),
                ("limit", "limit"),
                ("days", "days"),
                ("timezone", "timezone"),
                ("isEnabled", "enabled"),
            ):
                if source not in detail:
                    continue
                value = detail[source]
                if field in {"start_time", "end_time"}:
                    value = str(value)[:5]
                elif field == "limit":
                    value = coerce_optional_int(value)
                elif field == "days":
                    value = normalize_days(value)
                elif field == "enabled":
                    value = coerce_optional_bool(value)
                record[field] = value
            records.append(record)
    return (
        {"schedules": records, "schedule_families": families} if valid_inventory else {}
    )


def fresh_control_values(coord: Any, control: str, family: str) -> dict[str, object]:
    """Exclude cached companion fields and values promoted by write bookkeeping."""
    if family == "battery_status":
        return {
            "profile_key": coord.battery_live_profile,
            "profile_ready": not coord.battery_profile_pending,
        }
    payload_attr = {
        "battery_settings": "_battery_settings_payload",
        "storm_guard": "_battery_profile_payload",
        "battery_schedules": "_battery_schedules_payload",
    }.get(family)
    if payload_attr is None:
        return confirmed_control_values(coord, control)
    payload = getattr(coord, payload_attr, None)
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    if control in BATTERY_PROFILE_CONTROLS:
        return _profile_feedback(coord, control, data)
    if control.startswith("battery_schedule_"):
        if family == "battery_settings":
            enabled = {}
            for schedule_type in ("cfg", "dtg", "rbd"):
                raw = (
                    data.get("chargeFromGridScheduleEnabled")
                    if schedule_type == "cfg"
                    else data.get(schedule_type + "Control", {})
                )
                if isinstance(raw, dict):
                    raw = raw.get("enabled")
                value = coerce_optional_bool(raw)
                if value is not None:
                    enabled[schedule_type] = value
            return {"family_enabled": enabled} if enabled else {}
        return _schedule_inventory_feedback(data)
    if control.endswith("_schedule"):
        return _schedule_feedback(coord, control, family, data)
    raw_fields = {
        "charge_from_grid": "chargeFromGrid",
        "power_match": "powerMatchControl",
        "battery_shutdown_level": "veryLowSoc",
        "storm_guard": "stormGuardState",
        "storm_evse": "evseStormEnabled",
    }
    if control in raw_fields:
        source = raw_fields[control]
        if source not in data:
            return {}
        field = SCALAR_CONTROLS[control][0]
        value = data[source]
        if control == "power_match":
            return confirmed_control_values(coord, control)
        if control == "storm_guard":
            state = coord.battery_runtime.normalize_storm_guard_state(value)
            return {field: None if state is None else state == "enabled"}
        return {
            field: (
                coerce_optional_int(value)
                if field == "level"
                else coerce_optional_bool(value)
            )
        }
    return {}


def observe_control_family(
    coord: Any, family: str, tokens: Mapping[tuple[str, str | None], object]
) -> None:
    for control in FAMILY_CONTROLS.get(family, ()):
        values = fresh_control_values(coord, control, family)
        if values:
            coord.control_updates.observe(control, None, values, tokens)
