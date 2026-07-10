"""Authoritative active serial discovery helpers for registry cleanup."""

from __future__ import annotations

from .device_types import normalize_type_key


def inventory_type_available_for_cleanup(coord: object, type_key: str) -> bool | None:
    """Return inventory type availability, or None when the inventory view is unknown."""

    inventory_view = getattr(coord, "inventory_view", None)
    has_type_for_entities = getattr(inventory_view, "has_type_for_entities", None)
    if not callable(has_type_for_entities):
        return None
    try:
        return bool(has_type_for_entities(type_key))
    except Exception:  # noqa: BLE001
        return None


def inventory_type_selected_for_cleanup(coord: object, type_key: str) -> bool:
    """Return whether a device type is selected for this entry."""

    normalized = normalize_type_key(type_key)
    if not normalized:
        return False
    selected = getattr(coord, "_selected_type_keys", None)
    if selected is None or not selected:
        return True
    try:
        return normalized in {normalize_type_key(key) for key in selected}
    except Exception:  # noqa: BLE001
        return True


def inventory_type_bucket_for_cleanup(
    coord: object, type_key: str
) -> dict[str, object] | None:
    """Return the inventory bucket when the devices inventory exposed it."""

    normalized = normalize_type_key(type_key)
    if not normalized:
        return None
    inventory_view = getattr(coord, "inventory_view", None)
    type_bucket = getattr(inventory_view, "type_bucket", None)
    if callable(type_bucket):
        try:
            bucket = type_bucket(normalized)
        except Exception:  # noqa: BLE001
            bucket = None
        if isinstance(bucket, dict):
            return bucket
    buckets = getattr(coord, "_type_device_buckets", None)
    if not isinstance(buckets, dict):
        return None
    bucket = buckets.get(normalized)
    return bucket if isinstance(bucket, dict) else None


def inventory_type_bucket_empty_for_cleanup(coord: object, type_key: str) -> bool:
    """Return True when inventory explicitly exposed an empty bucket."""

    bucket = inventory_type_bucket_for_cleanup(coord, type_key)
    if not isinstance(bucket, dict):
        return False
    members = bucket.get("devices")
    if not isinstance(members, list) or members:
        return False
    if "count" not in bucket:
        return False
    try:
        count = int(str(bucket.get("count")))
    except (TypeError, ValueError):
        return False
    return count == 0


def inventory_type_known_absent_for_cleanup(coord: object, type_key: str) -> bool:
    """Return True when non-status discovery can safely prove absence."""

    return not inventory_type_selected_for_cleanup(
        coord, type_key
    ) or inventory_type_bucket_empty_for_cleanup(coord, type_key)


def serials_from_getter(getter: object) -> set[str] | None:
    """Return cleaned serials from a callable getter, or None on failure."""

    if not callable(getter):
        return None
    try:
        return {serial for sn in getter() if sn and (serial := str(sn).strip())}
    except Exception:  # noqa: BLE001
        return None


def active_charger_serials_for_cleanup(coord: object) -> set[str] | None:
    """Return active EVSE serials when EVSE inventory is authoritative."""

    if not bool(getattr(coord, "_devices_inventory_ready", False)):
        return None
    active_inventory = getattr(coord, "_active_inventory_evse_serials", None)
    if callable(active_inventory):
        try:
            serials = active_inventory()
        except Exception:  # noqa: BLE001
            return None
        if serials is None:
            return None
        return {serial for sn in serials if sn and (serial := str(sn).strip())}
    return serials_from_getter(getattr(coord, "iter_serials", None))


def active_battery_serials_for_cleanup(coord: object) -> set[str] | None:
    """Return active storage battery serials when battery status is authoritative."""

    if not bool(getattr(coord, "_devices_inventory_ready", False)):
        return None
    battery_type_available = inventory_type_available_for_cleanup(coord, "encharge")
    if getattr(coord, "_battery_has_encharge", None) is False:
        return set()
    if battery_type_available is False:
        if inventory_type_known_absent_for_cleanup(coord, "encharge"):
            return set()
        if isinstance(getattr(coord, "_battery_status_payload", None), dict):
            return serials_from_getter(getattr(coord, "iter_battery_serials", None))
        return None
    if battery_type_available is True and not isinstance(
        getattr(coord, "_battery_status_payload", None), dict
    ):
        return None
    return serials_from_getter(getattr(coord, "iter_battery_serials", None))


def active_ac_battery_serials_for_cleanup(coord: object) -> set[str] | None:
    """Return active AC Battery serials when AC Battery discovery is authoritative."""

    if not bool(getattr(coord, "_devices_inventory_ready", False)):
        return None
    ac_capability = getattr(coord, "battery_has_acb", None)
    if ac_capability is False:
        return set()
    ac_type_available = inventory_type_available_for_cleanup(coord, "ac_battery")
    if ac_type_available is False:
        if inventory_type_known_absent_for_cleanup(coord, "ac_battery"):
            return set()
        if isinstance(getattr(coord, "_ac_battery_devices_payload", None), dict):
            return serials_from_getter(getattr(coord, "iter_ac_battery_serials", None))
        return None
    if ac_type_available is True and ac_capability is not True:
        return None
    if ac_type_available is True and not isinstance(
        getattr(coord, "_ac_battery_devices_payload", None), dict
    ):
        return None
    return serials_from_getter(getattr(coord, "iter_ac_battery_serials", None))


def active_inverter_serials_for_cleanup(coord: object) -> set[str] | None:
    """Return active inverter serials when inverter inventory is authoritative."""

    if not bool(getattr(coord, "_devices_inventory_ready", False)):
        return None
    if not bool(getattr(coord, "include_inverters", True)):
        return set()
    inverter_type_available = inventory_type_available_for_cleanup(
        coord, "microinverter"
    )
    if inverter_type_available is False:
        if inventory_type_known_absent_for_cleanup(coord, "microinverter"):
            return set()
        if isinstance(getattr(coord, "_inverters_inventory_payload", None), dict):
            return serials_from_getter(getattr(coord, "iter_inverter_serials", None))
        return None
    if inverter_type_available is True and not isinstance(
        getattr(coord, "_inverters_inventory_payload", None), dict
    ):
        return None
    return serials_from_getter(getattr(coord, "iter_inverter_serials", None))


def active_serial_registry_identifiers(
    coord: object,
) -> dict[str, set[str] | None]:
    """Return active serial identifiers by serial-backed device family."""

    return {
        "charger": active_charger_serials_for_cleanup(coord),
        "battery": active_battery_serials_for_cleanup(coord),
        "ac_battery": active_ac_battery_serials_for_cleanup(coord),
        "inverter": active_inverter_serials_for_cleanup(coord),
    }


def all_active_serial_registry_identifiers(coord: object) -> set[str]:
    """Return active serial identifiers across supported serial device families."""

    active: set[str] = set()
    for serials in active_serial_registry_identifiers(coord).values():
        if serials is None:
            continue
        active.update(serials)
    return active
