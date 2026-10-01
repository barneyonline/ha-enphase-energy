"""Pure normalization of BatteryConfig grid relay observations."""

from __future__ import annotations

from .parsing_helpers import coerce_optional_text


def normalize_grid_mode_status_value(value: object) -> str | None:
    """Normalize cloud relay tokens without depending on coordinator state."""

    text = coerce_optional_text(value)
    if text is None:
        return None
    token = text.strip().upper()
    if token in {
        "OPER_RELAY_OPEN",
        "OPER_RELAY_OFFGRID_AC_GRID_PRESENT",
        "OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD",
    }:
        return "off_grid"
    if token in {
        "OPER_RELAY_CLOSED",
        "OPER_RELAY_WAITING_TO_INITIALIZE_ON_GRID",
    }:
        return "on_grid"
    return None


def grid_relay_candidates(payload: object) -> list[object]:
    """Read supported wrapper shapes, preserving candidate priority and order."""

    if isinstance(payload, dict):
        candidates: list[object] = []
        for key in ("gridRelay", "grid_relay"):
            if key in payload:
                candidates.append(payload.get(key))
        meters = payload.get("meters")
        if isinstance(meters, dict):
            candidates.extend(grid_relay_candidates(meters))
        elif isinstance(meters, list):
            for item in meters:
                candidates.extend(grid_relay_candidates(item))
        for key in ("data", "payload", "message"):
            nested = payload.get(key)
            if isinstance(nested, (dict, list)):
                candidates.extend(grid_relay_candidates(nested))
        return candidates
    if isinstance(payload, list):
        candidates = []
        for item in payload:
            candidates.extend(grid_relay_candidates(item))
        return candidates
    return []
