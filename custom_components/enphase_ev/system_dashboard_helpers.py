from __future__ import annotations

from collections.abc import Iterable

from .device_types import normalize_type_key
from .parsing_helpers import coerce_optional_text
from .runtime_helpers import coerce_optional_int

SYSTEM_DASHBOARD_TYPE_KEY_MAP: dict[str, str] = {
    "envoys": "envoy",
    "meters": "envoy",
    "enpowers": "envoy",
    "encharges": "encharge",
    "inverters": "microinverter",
    "modems": "modem",
}


def _format_inverter_model_summary(model_counts: dict[str, int]) -> str | None:
    clean: dict[str, int] = {}
    for model, count in (model_counts or {}).items():
        name = str(model).strip()
        if not name:
            continue
        try:
            count_int = int(count)
        except (TypeError, ValueError):
            continue
        if count_int <= 0:
            continue
        clean[name] = count_int
    if not clean:
        return None
    ordered = sorted(clean.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{name} x{count}" for name, count in ordered)


def dashboard_key_token(key: object) -> str:
    text = coerce_optional_text(key)
    if not text:
        return ""
    return "".join(ch if ch.isalnum() else "_" for ch in text.lower()).strip("_")


def dashboard_key_matches(key: object, *candidates: str) -> bool:
    token = dashboard_key_token(key)
    if not token:
        return False
    candidate_tokens = {
        dashboard_key_token(candidate) for candidate in candidates if candidate
    }
    if token in candidate_tokens:
        return True
    return any(
        token.startswith(candidate) or token.endswith(candidate) or candidate in token
        for candidate in candidate_tokens
        if candidate and len(candidate) >= 3
    )


def dashboard_simple_value(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            simplified = dashboard_simple_value(item)
            if simplified is not None:
                out[str(key)] = simplified
        return out or None
    if isinstance(value, list):
        items = [
            simplified
            for item in value
            if (simplified := dashboard_simple_value(item)) is not None
        ]
        return items or None
    return coerce_optional_text(value)


def iter_dashboard_mappings(value: object) -> Iterable[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_dashboard_mappings(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_dashboard_mappings(item)


def dashboard_first_value(payload: object, *keys: str) -> object | None:
    for mapping in iter_dashboard_mappings(payload):
        for key, value in mapping.items():
            if dashboard_key_matches(key, *keys):
                return value
    return None


def dashboard_first_mapping(payload: object, *keys: str) -> dict[str, object] | None:
    value = dashboard_first_value(payload, *keys)
    if isinstance(value, dict):
        return dict(value)
    return None


def dashboard_field(
    payload: object,
    *keys: str,
    default: object | None = None,
) -> object | None:
    value = dashboard_first_value(payload, *keys)
    simplified = dashboard_simple_value(value)
    if simplified is None:
        return default
    return simplified


def dashboard_field_map(
    payload: object,
    fields: dict[str, tuple[str, ...]],
) -> dict[str, object]:
    out: dict[str, object] = {}
    for output_key, candidate_keys in fields.items():
        value = dashboard_field(payload, *candidate_keys)
        if value is not None:
            out[output_key] = value
    return out


def dashboard_aliases(payload: dict[str, object]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for key in (
        "device_uid",
        "device-uid",
        "uid",
        "iqer_uid",
        "iqer-uid",
        "hems_device_id",
        "hems-device-id",
        "serial_number",
        "serialNumber",
        "serial",
        "device_id",
        "deviceId",
        "id",
    ):
        value = coerce_optional_text(payload.get(key))
        if not value or value in seen:
            continue
        seen.add(value)
        aliases.append(value)
    return aliases


def dashboard_primary_id(payload: dict[str, object]) -> str | None:
    for key in (
        "device_uid",
        "device-uid",
        "uid",
        "iqer_uid",
        "iqer-uid",
        "hems_device_id",
        "hems-device-id",
        "serial_number",
        "serialNumber",
        "serial",
        "device_id",
        "deviceId",
        "id",
    ):
        value = coerce_optional_text(payload.get(key))
        if value:
            return value
    return None


def dashboard_parent_id(payload: dict[str, object]) -> str | None:
    for key in (
        "parent_uid",
        "parentUid",
        "parent_device_uid",
        "parentDeviceUid",
        "parent_id",
        "parentId",
        "parent",
    ):
        value = coerce_optional_text(payload.get(key))
        if value:
            return value
    return None


def dashboard_raw_type(
    payload: dict[str, object],
    fallback_type: str | None = None,
) -> str | None:
    for key in (
        "type",
        "device_type",
        "deviceType",
        "channel_type",
        "channelType",
        "meter_type",
    ):
        value = coerce_optional_text(payload.get(key))
        if value:
            return value
    return fallback_type


def system_dashboard_type_key(raw_type: object) -> str | None:
    text = coerce_optional_text(raw_type)
    if text:
        token = "".join(ch if ch.isalnum() else "_" for ch in text.lower()).strip("_")
        if token in SYSTEM_DASHBOARD_TYPE_KEY_MAP:
            return SYSTEM_DASHBOARD_TYPE_KEY_MAP[token]
    normalized = normalize_type_key(raw_type)
    return str(normalized) if normalized else None


def system_dashboard_detail_records(
    payloads: dict[str, object],
    *source_types: str,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for source_type in source_types:
        payload = payloads.get(source_type)
        if not isinstance(payload, dict):
            continue
        items = payload.get(source_type)
        if isinstance(items, list):
            source_items = items
        elif isinstance(items, dict):
            nested_items = (
                items.get("devices")
                if isinstance(items.get("devices"), list)
                else (
                    items.get("members")
                    if isinstance(items.get("members"), list)
                    else (
                        items.get("items")
                        if isinstance(items.get("items"), list)
                        else None
                    )
                )
            )
            source_items = nested_items if isinstance(nested_items, list) else [items]
        else:
            nested_items = (
                payload.get("devices")
                if isinstance(payload.get("devices"), list)
                else (
                    payload.get("members")
                    if isinstance(payload.get("members"), list)
                    else (
                        payload.get("items")
                        if isinstance(payload.get("items"), list)
                        else None
                    )
                )
            )
            source_items = nested_items if isinstance(nested_items, list) else [payload]
        for item in source_items:
            if not isinstance(item, dict):
                continue
            record = dict(item)
            dedupe_key = (
                coerce_optional_text(record.get("serial_number")),
                coerce_optional_text(record.get("device_uid")),
                coerce_optional_text(record.get("id")),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            records.append(record)
    return records


def system_dashboard_meter_kind(payload: dict[str, object]) -> str | None:
    for value in (
        payload.get("meter_type"),
        payload.get("config_type"),
        payload.get("channel_type"),
        payload.get("name"),
    ):
        text = coerce_optional_text(value)
        if not text:
            continue
        normalized = "".join(ch if ch.isalnum() else "_" for ch in text.lower())
        if "production" in normalized or normalized in ("prod", "pv", "solar"):
            return "production"
        if "consumption" in normalized or normalized in ("cons", "load", "site_load"):
            return "consumption"
    return None


def system_dashboard_battery_detail_subset(
    payload: dict[str, object] | None,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    allowed = (
        "phase",
        "operation_mode",
        "app_version",
        "sw_version",
        "rssi_subghz",
        "rssi_24ghz",
        "rssi_dbm",
        "led_status",
        "alarm_id",
    )
    out: dict[str, object] = {}
    for key in allowed:
        value = payload.get(key)
        if value is not None:
            out[key] = value
    return out


def dashboard_node_entry(
    payload: dict[str, object],
    *,
    fallback_type: str | None = None,
    parent_uid: str | None = None,
) -> dict[str, object] | None:
    device_uid = dashboard_primary_id(payload)
    if not device_uid:
        return None
    raw_type = dashboard_raw_type(payload, fallback_type)
    type_key = system_dashboard_type_key(raw_type)
    entry: dict[str, object] = {"device_uid": device_uid}
    if type_key:
        entry["type_key"] = type_key
    if raw_type:
        entry["source_type"] = raw_type
    parent = dashboard_parent_id(payload) or parent_uid
    if parent:
        entry["parent_uid"] = parent
    name = coerce_optional_text(
        payload.get("name")
        if payload.get("name") is not None
        else payload.get("display_name")
    )
    if name:
        entry["name"] = name
    serial = coerce_optional_text(
        payload.get("serial_number")
        if payload.get("serial_number") is not None
        else (
            payload.get("serialNumber")
            if payload.get("serialNumber") is not None
            else payload.get("serial")
        )
    )
    if serial:
        entry["serial_number"] = serial
    return entry


def dashboard_child_containers(
    payload: dict[str, object],
) -> list[tuple[object, str | None]]:
    out: list[tuple[object, str | None]] = []
    next_type = dashboard_raw_type(payload)
    for key, value in payload.items():
        if dashboard_key_matches(
            key,
            "children",
            "child_nodes",
            "devices",
            "members",
            "items",
            "nodes",
            "result",
            "data",
            "envoy",
            "envoys",
            "meter",
            "meters",
            "enpower",
            "enpowers",
            "encharge",
            "encharges",
            "modem",
            "modems",
            "inverter",
            "inverters",
        ) and isinstance(value, (dict, list)):
            out.append((value, next_type))
    return out


def index_dashboard_nodes(
    payload: object,
    *,
    fallback_type: str | None = None,
    parent_uid: str | None = None,
    index: dict[str, dict[str, object]] | None = None,
    alias_index: dict[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    out = index if isinstance(index, dict) else {}
    aliases = alias_index if isinstance(alias_index, dict) else {}
    if isinstance(payload, list):
        for item in payload:
            index_dashboard_nodes(
                item,
                fallback_type=fallback_type,
                parent_uid=parent_uid,
                index=out,
                alias_index=aliases,
            )
        return out
    if not isinstance(payload, dict):
        return out

    entry = dashboard_node_entry(
        payload,
        fallback_type=fallback_type,
        parent_uid=parent_uid,
    )
    next_parent = parent_uid
    next_type = fallback_type
    if entry is not None:
        entry_aliases = dashboard_aliases(payload)
        device_uid = next(
            (
                canonical_uid
                for alias in entry_aliases
                if (canonical_uid := aliases.get(alias)) is not None
            ),
            str(entry["device_uid"]),
        )
        existing = out.get(device_uid, {"device_uid": device_uid})
        for key, value in entry.items():
            if value is None:
                continue
            existing[key] = value
        existing["device_uid"] = device_uid
        out[device_uid] = existing
        for alias in entry_aliases:
            aliases[alias] = device_uid
        next_parent = device_uid
        next_type = coerce_optional_text(entry.get("source_type")) or next_type

    for child_payload, child_type in dashboard_child_containers(payload):
        index_dashboard_nodes(
            child_payload,
            fallback_type=child_type or next_type,
            parent_uid=next_parent,
            index=out,
            alias_index=aliases,
        )
    return out


def system_dashboard_hierarchy_summary_from_index(
    index: dict[str, dict[str, object]],
    alias_index: dict[str, str] | None = None,
) -> dict[str, object]:
    counts_by_type: dict[str, int] = {}
    child_counts: dict[str, int] = {}
    relationships: list[dict[str, object]] = []
    aliases = alias_index if isinstance(alias_index, dict) else {}
    for device_uid, entry in index.items():
        type_key = coerce_optional_text(entry.get("type_key"))
        if type_key:
            counts_by_type[type_key] = counts_by_type.get(type_key, 0) + 1
        parent_uid = coerce_optional_text(entry.get("parent_uid"))
        if parent_uid:
            parent_uid = aliases.get(parent_uid, parent_uid)
        if parent_uid:
            child_counts[parent_uid] = child_counts.get(parent_uid, 0) + 1
        relationships.append(
            {
                "device_uid": device_uid,
                "parent_uid": parent_uid,
                "type_key": type_key,
                "source_type": coerce_optional_text(entry.get("source_type")),
                "name": coerce_optional_text(entry.get("name")),
                "serial_number": coerce_optional_text(entry.get("serial_number")),
            }
        )
    for relationship in relationships:
        relationship["child_count"] = child_counts.get(
            str(relationship.get("device_uid")), 0
        )
    relationships.sort(
        key=lambda item: (
            str(item.get("type_key") or ""),
            str(item.get("name") or ""),
            str(item.get("device_uid") or ""),
        )
    )
    return {
        "total_nodes": len(index),
        "counts_by_type": counts_by_type,
        "relationships": relationships,
    }


def system_dashboard_type_hierarchy(
    type_key: str,
    index: dict[str, dict[str, object]],
    alias_index: dict[str, str] | None = None,
) -> dict[str, object]:
    aliases = alias_index if isinstance(alias_index, dict) else {}
    relationships = [
        {
            "device_uid": device_uid,
            "parent_uid": (
                aliases.get(parent_uid, parent_uid)
                if (parent_uid := coerce_optional_text(entry.get("parent_uid")))
                else None
            ),
            "name": coerce_optional_text(entry.get("name")),
            "serial_number": coerce_optional_text(entry.get("serial_number")),
            "source_type": coerce_optional_text(entry.get("source_type")),
            "child_count": sum(
                1
                for candidate in index.values()
                if coerce_optional_text(candidate.get("parent_uid")) == device_uid
            ),
        }
        for device_uid, entry in index.items()
        if coerce_optional_text(entry.get("type_key")) == type_key
    ]
    relationships.sort(
        key=lambda item: (
            str(item.get("name") or ""),
            str(item.get("device_uid") or ""),
        )
    )
    return {"count": len(relationships), "relationships": relationships}


def system_dashboard_meter_summaries(
    payloads: dict[str, object],
) -> list[dict[str, object]]:
    meters: list[dict[str, object]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for record in system_dashboard_detail_records(payloads, "meters", "meter"):
        name = coerce_optional_text(record.get("name"))
        meter_kind = system_dashboard_meter_kind(record)
        meter_type = coerce_optional_text(record.get("meter_type")) or meter_kind
        dedupe_key = (name, meter_type)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        meter_summary = {
            "name": name,
            "meter_type": meter_type,
            "status": dashboard_field(record, "status", "status_text"),
            "meter_state": coerce_optional_text(record.get("meter_state")),
            "config_type": coerce_optional_text(record.get("config_type")),
        }
        config_payload = dashboard_first_mapping(
            record,
            "configuration",
            "meter_config",
            "meter_configuration",
        )
        if isinstance(config_payload, dict):
            config = dashboard_field_map(
                config_payload,
                {
                    "phase": ("phase", "phase_mode", "phase_configuration"),
                    "wiring": ("wiring", "wiring_type"),
                    "mode": ("mode", "config_mode", "meter_mode"),
                    "role": ("role", "measurement", "measurement_type"),
                    "enabled": ("enabled", "is_enabled"),
                },
            )
            if config:
                meter_summary["config"] = config
        cleaned = {
            key: value for key, value in meter_summary.items() if value is not None
        }
        if cleaned:
            meters.append(cleaned)
    meters.sort(
        key=lambda item: (
            str(item.get("name") or ""),
            str(item.get("meter_type") or ""),
        )
    )
    return meters


def system_dashboard_envoy_summary(
    payloads: dict[str, object],
    index: dict[str, dict[str, object]],
    alias_index: dict[str, str] | None = None,
) -> dict[str, object]:
    modem_records = system_dashboard_detail_records(payloads, "modems", "modem")
    modem_source = (
        modem_records[0]
        if modem_records
        else dashboard_first_mapping(payloads, "modem", "cellular", "sim")
    )
    envoy_records = system_dashboard_detail_records(payloads, "envoys", "envoy")
    envoy_source = envoy_records[0] if envoy_records else payloads
    network_source = dashboard_first_mapping(
        envoy_source,
        "network",
        "network_config",
        "gateway_network",
        "gateway_config",
    )
    tunnel_source = dashboard_first_mapping(envoy_source, "tunnel", "vpn")
    controller_records = system_dashboard_detail_records(
        payloads, "enpowers", "enpower"
    )
    controller_source = (
        controller_records[0]
        if controller_records
        else dashboard_first_mapping(
            payloads, "controller", "system_controller", "enpower"
        )
    )
    summary = {
        "modem": dashboard_field_map(
            modem_source or payloads,
            {
                "signal": ("signal", "signal_strength", "signal_level", "sig_str"),
                "rssi": ("rssi",),
                "sim_plan_expiry": (
                    "sim_plan_expiry",
                    "plan_expiry",
                    "plan_expiry_date",
                    "plan_end",
                    "sim_expiry",
                ),
            },
        ),
        "network": dashboard_field_map(
            network_source or envoy_source,
            {
                "status": ("status", "state", "link_state"),
                "mode": ("mode", "network_mode", "config_mode"),
                "type": ("type", "network_type", "connection_type"),
                "dhcp": ("dhcp", "is_dhcp"),
                "enabled": ("enabled", "is_enabled"),
            },
        ),
        "tunnel": dashboard_field_map(
            tunnel_source or payloads,
            {
                "status": ("status", "state"),
                "type": ("type", "tunnel_type"),
                "enabled": ("enabled", "is_enabled"),
                "connected": ("connected", "is_connected"),
                "healthy": ("healthy", "is_healthy"),
            },
        ),
        "controller": dashboard_field_map(
            controller_source or payloads,
            {
                "earth_type": ("earth_type", "earthType", "system_earth_type"),
                "status": ("status", "state"),
                "operation_mode": ("operation_mode", "mode"),
            },
        ),
        "meters": system_dashboard_meter_summaries(payloads),
        "hierarchy": system_dashboard_type_hierarchy("envoy", index, alias_index),
    }
    return {key: value for key, value in summary.items() if value not in ({}, [], None)}


def system_dashboard_encharge_summary(
    payloads: dict[str, object],
    index: dict[str, dict[str, object]],
    alias_index: dict[str, str] | None = None,
) -> dict[str, object]:
    records = system_dashboard_detail_records(payloads, "encharges", "encharge")
    first_record = records[0] if records else payloads
    connectivity_source = dashboard_first_mapping(
        first_record, "connectivity", "network", "wireless"
    )
    software_source = dashboard_first_mapping(
        first_record, "software", "app", "application"
    )
    operation_source = dashboard_first_mapping(
        first_record, "operation_mode", "operation", "mode"
    )
    summary = {
        "connectivity": dashboard_field_map(
            connectivity_source or first_record,
            {
                "signal": ("signal", "signal_strength", "signal_level", "sig_str"),
                "rssi": ("rssi",),
                "rssi_subghz": ("rssi_subghz",),
                "rssi_24ghz": ("rssi_24ghz",),
                "rssi_dbm": ("rssi_dbm",),
                "status": ("status", "state"),
            },
        ),
        "software": dashboard_field_map(
            software_source or first_record,
            {
                "app_version": ("app_version", "appVersion", "version"),
                "firmware": ("firmware", "fw_version", "sw_version"),
                "sw_version": ("sw_version",),
            },
        ),
        "operation_mode": dashboard_field_map(
            operation_source or first_record,
            {
                "mode": ("operation_mode", "operationMode", "mode"),
                "state": ("status", "state"),
            },
        ),
        "batteries": [
            system_dashboard_battery_detail_subset(record)
            | {
                key: value
                for key, value in {
                    "name": coerce_optional_text(record.get("name")),
                    "serial_number": coerce_optional_text(record.get("serial_number")),
                    "status": coerce_optional_text(record.get("status")),
                    "status_text": coerce_optional_text(record.get("statusText")),
                    "soc": coerce_optional_text(record.get("soc")),
                }.items()
                if value is not None
            }
            for record in records
            if system_dashboard_battery_detail_subset(record)
            or coerce_optional_text(record.get("serial_number"))
            or coerce_optional_text(record.get("name"))
        ],
        "hierarchy": system_dashboard_type_hierarchy("encharge", index, alias_index),
    }
    return {key: value for key, value in summary.items() if value not in ({}, [], None)}


def system_dashboard_microinverter_summary(
    payloads: dict[str, object],
    index: dict[str, dict[str, object]],
    alias_index: dict[str, str] | None = None,
) -> dict[str, object]:
    summary_payload = dashboard_first_mapping(payloads, "inverters", "inverter") or {}
    if not isinstance(summary_payload, dict):
        return {}
    nested_payload = summary_payload.get("inverters")
    if isinstance(nested_payload, dict):
        summary_payload = nested_payload
    total = coerce_optional_int(summary_payload.get("total"))
    not_reporting = coerce_optional_int(summary_payload.get("not_reporting"))
    plc_comm = coerce_optional_int(summary_payload.get("plc_comm"))
    items = summary_payload.get("items")
    if isinstance(items, list):
        model_counts = {
            coerce_optional_text(item.get("name"))
            or f"item_{index}": (coerce_optional_int(item.get("count")) or 0)
            for index, item in enumerate(items, start=1)
            if isinstance(item, dict)
        }
    else:
        model_counts = {}
    reporting = None
    if total is not None:
        reporting = max(0, total - int(not_reporting or 0))
    connectivity = None
    if total is not None:
        if int(total) <= 0:
            connectivity = None
        elif int(not_reporting or 0) <= 0:
            connectivity = "online"
        elif int(not_reporting or 0) >= int(total):
            connectivity = "offline"
        else:
            connectivity = "degraded"
    summary = {
        "total_inverters": total,
        "reporting_inverters": reporting,
        "not_reporting_inverters": not_reporting,
        "plc_comm_inverters": plc_comm,
        "model_counts": model_counts or None,
        "model_summary": _format_inverter_model_summary(model_counts),
        "connectivity": connectivity,
        "hierarchy": system_dashboard_type_hierarchy(
            "microinverter", index, alias_index
        ),
    }
    return {key: value for key, value in summary.items() if value is not None}


def build_system_dashboard_summaries(
    tree_payload: dict[str, object] | None,
    details_payloads: dict[str, dict[str, object]],
) -> tuple[
    dict[str, dict[str, object]], dict[str, object], dict[str, dict[str, object]]
]:
    hierarchy_index: dict[str, dict[str, object]] = {}
    hierarchy_aliases: dict[str, str] = {}
    if isinstance(tree_payload, dict):
        hierarchy_index = index_dashboard_nodes(
            tree_payload, alias_index=hierarchy_aliases
        )
    for type_key, payloads_by_source in details_payloads.items():
        for source_type, payload in payloads_by_source.items():
            index_dashboard_nodes(
                payload,
                fallback_type=source_type or type_key,
                index=hierarchy_index,
                alias_index=hierarchy_aliases,
            )
    hierarchy_summary = system_dashboard_hierarchy_summary_from_index(
        hierarchy_index,
        hierarchy_aliases,
    )
    type_summaries: dict[str, dict[str, object]] = {}
    envoy_payloads: dict[str, object] = {}
    for key in ("envoy", "modem"):
        payloads = details_payloads.get(key, {})
        if isinstance(payloads, dict):
            envoy_payloads.update(payloads)
    if envoy_summary := system_dashboard_envoy_summary(
        envoy_payloads,
        hierarchy_index,
        hierarchy_aliases,
    ):
        type_summaries["envoy"] = envoy_summary
    if encharge_summary := system_dashboard_encharge_summary(
        details_payloads.get("encharge", {}),
        hierarchy_index,
        hierarchy_aliases,
    ):
        type_summaries["encharge"] = encharge_summary
    if microinverter_summary := system_dashboard_microinverter_summary(
        details_payloads.get("microinverter", {}),
        hierarchy_index,
        hierarchy_aliases,
    ):
        type_summaries["microinverter"] = microinverter_summary
    return type_summaries, hierarchy_summary, hierarchy_index
