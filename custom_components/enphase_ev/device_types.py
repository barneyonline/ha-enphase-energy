"""Normalize Enphase inventory type names and device members."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .const import DOMAIN

_TYPE_ALIAS_TOKEN_MAP: dict[str, str] = {
    # Inventory labels vary between Enlighten pages, API families, and legacy
    # product names; normalize aliases before entity/device decisions.
    "envoy": "envoy",
    "gateway": "envoy",
    "iqgateway": "envoy",
    "iqgateways": "envoy",
    "meter": "envoy",
    "meters": "envoy",
    "encharge": "encharge",
    "storage": "encharge",
    "storages": "encharge",
    "battery": "encharge",
    "batteries": "encharge",
    "enpower": "envoy",
    "systemcontroller": "envoy",
    "systemcontrollers": "envoy",
    "iqevse": "iqevse",
    "evse": "iqevse",
    "evcharger": "iqevse",
    "evchargers": "iqevse",
    "heatpump": "heatpump",
    "heat_pump": "heatpump",
    "heat-pump": "heatpump",
    "drycontact": "dry_contact",
    "drycontacts": "dry_contact",
    "drycontactload": "dry_contact",
    "drycontactloads": "dry_contact",
    "nc1": "dry_contact",
    "nc2": "dry_contact",
    "no1": "dry_contact",
    "no2": "dry_contact",
    "inverter": "microinverter",
    "inverters": "microinverter",
    "microinverter": "microinverter",
    "microinverters": "microinverter",
    "generator": "generator",
    "generators": "generator",
}

KNOWN_TYPE_LABELS: dict[str, str] = {
    "envoy": "Gateway",
    "encharge": "Battery",
    "enpower": "System Controller",
    "iqevse": "EV Chargers",
    "heatpump": "Heat Pump",
    "dry_contact": "Dry Contacts",
    "microinverter": "Microinverters",
    "generator": "Generator",
}

KNOWN_TYPE_ORDER: tuple[str, ...] = (
    "envoy",
    "encharge",
    "enpower",
    "iqevse",
    "heatpump",
    "microinverter",
    "generator",
)

ONBOARDING_SUPPORTED_TYPE_KEYS: tuple[str, ...] = (
    "envoy",
    "encharge",
    "iqevse",
    "heatpump",
    "microinverter",
)

_PREFERRED_MEMBER_KEYS: tuple[str, ...] = (
    "name",
    "serial_number",
    "sku_id",
    "channel_type",
    "model",
    "status",
    "statusText",
    "connected",
    "last_report",
    "sw_version",
    "envoy_sw_version",
    "warranty_end_date",
    "ip",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DRY_CONTACT_RELAY_KEYS: frozenset[str] = frozenset({"nc1", "nc2", "no1", "no2"})


def normalize_type_key(raw_type: object) -> str | None:
    if raw_type is None:
        return None
    try:
        text = str(raw_type).strip().lower()
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    slug = _SLUG_RE.sub("_", text).strip("_")
    if not slug:
        return None
    alias_token = slug.replace("_", "")
    # This legacy family is no longer supported, including cached selections.
    if alias_token in {"acbattery", "acbatteries"}:
        return None
    canonical = _TYPE_ALIAS_TOKEN_MAP.get(alias_token)
    if canonical:
        return canonical
    if "drycontact" in alias_token:
        return "dry_contact"
    return slug


def type_display_label(type_key: object) -> str | None:
    normalized = normalize_type_key(type_key)
    if not normalized:
        return None
    if normalized in KNOWN_TYPE_LABELS:
        return KNOWN_TYPE_LABELS[normalized]
    words = [part for part in normalized.split("_") if part]
    if not words:
        return None
    return " ".join(word.capitalize() for word in words)


def is_dry_contact_type_key(type_key: object) -> bool:
    if type_key is None:
        return False
    try:
        raw_text = str(type_key).strip().lower()
    except Exception:  # noqa: BLE001
        return False
    if not raw_text:
        return False
    raw_compact = _SLUG_RE.sub("", raw_text)
    if "drycontact" in raw_compact:
        return True
    normalized = normalize_type_key(type_key)
    if not normalized:
        return False
    compact = normalized.replace("_", "")
    if compact in ("drycontact", "drycontacts"):
        return True
    tokens = set(normalized.split("_"))
    if "dry" in tokens and ("contact" in tokens or "contacts" in tokens):
        return True
    return bool(tokens & _DRY_CONTACT_RELAY_KEYS)


def type_identifier(site_id: object, type_key: object) -> tuple[str, str] | None:
    normalized = normalize_type_key(type_key)
    if not normalized:
        return None
    try:
        site_text = str(site_id).strip()
    except Exception:  # noqa: BLE001
        return None
    if not site_text:
        return None
    return DOMAIN, f"type:{site_text}:{normalized}"


def parse_type_identifier(identifier: object) -> tuple[str, str] | None:
    if identifier is None:
        return None
    try:
        ident_text = str(identifier).strip()
    except Exception:  # noqa: BLE001
        return None
    if not ident_text.startswith("type:"):
        return None
    parts = ident_text.split(":", 2)
    if len(parts) != 3:
        return None
    site_id = parts[1].strip()
    key = normalize_type_key(parts[2])
    if not site_id or not key:
        return None
    return site_id, key


def member_is_retired(member: object) -> bool:
    if not isinstance(member, dict):
        return False
    retired_flag = member.get("isRetired")
    if retired_flag is True:
        return True
    # Some inventory payloads omit ``isRetired`` and only expose lifecycle text.
    for key in ("status", "statusText", "status_text"):
        value = member.get(key)
        if value is None:
            continue
        try:
            normalized = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            continue
        if normalized == "retired":
            return True
    return False


def sanitize_member(member: object) -> dict[str, str | int | float | bool | None]:
    if not isinstance(member, dict):
        return {}
    out: dict[str, str | int | float | bool | None] = {}
    seen: set[str] = set()

    def _clean_scalar(value: Any) -> str | int | float | bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            return value.strip()
        return None

    for key in _PREFERRED_MEMBER_KEYS:
        if key not in member:
            continue
        value = _clean_scalar(member.get(key))
        if value is None and member.get(key) is not None:
            continue
        out[key] = value
        seen.add(key)

    extra_keys = sorted(str(key) for key in member.keys() if str(key) not in seen)
    for key in extra_keys:
        value = _clean_scalar(member.get(key))
        if value is None and member.get(key) is not None:
            continue
        out[key] = value
    return out


def active_type_keys_from_inventory(
    payload: object,
    *,
    allowed_type_keys: Iterable[str] | None = None,
) -> list[str]:
    """Extract active canonical type keys from a devices inventory payload."""
    if isinstance(payload, dict):
        result = payload.get("result")
    elif isinstance(payload, list):
        result = payload
    else:
        return []
    if not isinstance(result, list):
        return []

    allowed: set[str] | None = None
    if allowed_type_keys is not None:
        allowed = {
            key
            for raw in allowed_type_keys
            if raw is not None
            for key in [normalize_type_key(raw)]
            if key
        }

    active: set[str] = set()
    for bucket in result:
        if not isinstance(bucket, dict):
            continue
        raw_type = bucket.get("type")
        if raw_type is None:
            raw_type = bucket.get("deviceType")
        if raw_type is None:
            raw_type = bucket.get("device_type")
        type_key = normalize_type_key(raw_type)
        members = bucket.get("devices")
        if not isinstance(members, list):
            members = bucket.get("items")
        if not isinstance(members, list):
            members = bucket.get("members")
        if not type_key or not isinstance(members, list):
            continue
        if type_key in {"hemsdevices", "hems_devices"}:
            heatpump_members: list[dict[str, Any]] = []
            for group in members:
                if not isinstance(group, dict):
                    continue
                for key in ("heat-pump", "heat_pump", "heatpump"):
                    raw_group_members = group.get(key)
                    if not isinstance(raw_group_members, list):
                        continue
                    for member in raw_group_members:
                        if isinstance(member, dict) and not member_is_retired(member):
                            heatpump_members.append(member)
            if heatpump_members and (allowed is None or "heatpump" in allowed):
                active.add("heatpump")
            continue

        if allowed is not None and type_key not in allowed:
            continue
        if any(
            isinstance(member, dict) and not member_is_retired(member)
            for member in members
        ):
            active.add(type_key)

    ordered: list[str] = []
    for key in KNOWN_TYPE_ORDER:
        normalized = normalize_type_key(key)
        if normalized and normalized in active and normalized not in ordered:
            ordered.append(normalized)
    for key in sorted(active):
        if key not in ordered:
            ordered.append(key)
    return ordered


def active_type_serials_from_inventory(
    payload: object,
    *,
    type_key: object,
) -> list[str]:
    """Extract active device serials for a canonical type from inventory payload."""
    normalized_type = normalize_type_key(type_key)
    if not normalized_type:
        return []

    if isinstance(payload, dict):
        result = payload.get("result")
    elif isinstance(payload, list):
        result = payload
    else:
        return []
    if not isinstance(result, list):
        return []

    serials: list[str] = []
    serial_field_candidates = (
        "serial_number",
        "serial",
        "serialNumber",
        "device_sn",
    )
    for bucket in result:
        if not isinstance(bucket, dict):
            continue
        raw_type = bucket.get("type")
        if raw_type is None:
            raw_type = bucket.get("deviceType")
        if raw_type is None:
            raw_type = bucket.get("device_type")
        bucket_type = normalize_type_key(raw_type)
        members = bucket.get("devices")
        if not isinstance(members, list):
            members = bucket.get("items")
        if not isinstance(members, list):
            members = bucket.get("members")
        if bucket_type != normalized_type or not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict) or member_is_retired(member):
                continue
            for field in serial_field_candidates:
                raw_serial = member.get(field)
                if raw_serial is None:
                    continue
                try:
                    serial = str(raw_serial).strip()
                except Exception:  # noqa: BLE001
                    serial = ""
                if serial and serial not in serials:
                    serials.append(serial)
                break
    return serials
