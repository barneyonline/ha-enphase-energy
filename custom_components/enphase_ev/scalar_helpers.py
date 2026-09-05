"""Normalize scalar values using the integration's explicit payload policies."""

from __future__ import annotations

from datetime import datetime


def coerce_snapshot_bool(value: object) -> bool | None:
    """Parse telemetry booleans without interpreting imperative enable/disable."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "y", "enabled", "on"):
            return True
        if normalized in ("false", "0", "no", "n", "disabled", "off"):
            return False
    return None


def coerce_optional_bool(value: object) -> bool | None:
    """Parse capability booleans, including imperative enable/disable values."""

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "enable":
            return True
        if normalized == "disable":
            return False
    return coerce_snapshot_bool(value)


def sum_optional_values(values: object) -> float | None:
    """Sum valid finite samples, preserving missing versus a measured zero."""

    if not isinstance(values, list):
        return None
    total = 0.0
    found = False
    for item in values:
        if item is None:
            continue
        try:
            numeric = float(item)
        except Exception:  # noqa: BLE001 - tolerate malformed cloud scalars
            continue
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            continue
        total += numeric
        found = True
    return total if found else None


def snapshot_compatible_value(value: object) -> object:
    """Detach discovery values into JSON-compatible containers and scalars."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            try:
                key_text = str(key)
            except Exception:  # noqa: BLE001 - best-effort discovery metadata
                continue
            out[key_text] = snapshot_compatible_value(item)
        return out
    if isinstance(value, (list, tuple, set)):
        return [snapshot_compatible_value(item) for item in value]
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - best-effort discovery metadata
        return None
