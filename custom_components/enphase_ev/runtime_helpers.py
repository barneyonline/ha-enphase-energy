"""Provide shared coercion, diagnostics, and time helpers for runtimes."""

from __future__ import annotations

import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone as _tz
from zoneinfo import ZoneInfo

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_SLOW_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MIN_FAST_POLL_INTERVAL,
    MIN_SLOW_POLL_INTERVAL,
)


def coerce_int(value: object, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def coerce_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except Exception:  # noqa: BLE001
        return None


def coerce_optional_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None


def _text_contains_iq_evse(value: object) -> bool:
    try:
        text = str(value).strip().upper()
    except Exception:  # noqa: BLE001
        return False
    return "IQ-EVSE" in text


def evse_session_energy_uses_wh(*sources: object) -> bool:
    """Return true when EVSE session energy is known to be reported as Wh."""

    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in (
            "modelId",
            "model_id",
            "sku",
            "model",
            "modelName",
            "model_name",
            "partNumber",
            "part_number",
        ):
            value = source.get(key)
            if value is not None and _text_contains_iq_evse(value):
                return True
        session = source.get("session_d")
        if isinstance(session, dict) and evse_session_energy_uses_wh(session):
            return True
        if any(
            key in source
            for key in (
                "strt_chrg",
                "plg_in_at",
                "plg_out_at",
                "auth_status",
                "auth_type",
                "auth_id",
                "charge_level",
            )
        ):
            return True
    return False


def normalize_evse_session_energy(
    value: object,
    *,
    wh_hint: bool = False,
) -> tuple[float | None, float | None, str | None]:
    """Normalize EVSE session energy to kWh, Wh, and the inferred source unit."""

    if value is None:
        return None, None, None
    try:
        numeric_value = float(value)
    except Exception:  # noqa: BLE001
        return None, None, None

    if wh_hint or numeric_value > 200:
        try:
            return (
                round(numeric_value / 1000.0, 2),
                round(numeric_value, 3),
                "Wh",
            )
        except Exception:  # noqa: BLE001
            return None, None, "Wh"

    try:
        return (
            round(numeric_value, 2),
            round(numeric_value * 1000.0, 3),
            "kWh",
        )
    except Exception:  # noqa: BLE001
        return None, None, "kWh"


def iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def monotonic_deadline_to_utc_iso(target_mono: float) -> str | None:
    now_mono = time.monotonic()
    if target_mono <= 0 or target_mono <= now_mono:
        return None
    delta_seconds = target_mono - now_mono
    return iso_or_none(dt_util.utcnow() + timedelta(seconds=delta_seconds))


def inventory_type_available(coord: object, type_key: str) -> bool:
    inventory_view = getattr(coord, "inventory_view", None)
    has_type_for_entities = getattr(inventory_view, "has_type_for_entities", None)
    return bool(callable(has_type_for_entities) and has_type_for_entities(type_key))


def inventory_type_device_info(coord: object, type_key: str) -> DeviceInfo | None:
    inventory_view = getattr(coord, "inventory_view", None)
    type_device_info = getattr(inventory_view, "type_device_info", None)
    if not callable(type_device_info):
        return None
    return type_device_info(type_key)


def normalize_poll_intervals(
    fast_value: object,
    slow_value: object,
    *,
    fast_default: int = DEFAULT_FAST_POLL_INTERVAL,
    slow_default: int = DEFAULT_SLOW_POLL_INTERVAL,
) -> tuple[int, int]:
    """Return sanitized fast/slow polling intervals."""

    fast = min(
        MAX_POLL_INTERVAL,
        max(MIN_FAST_POLL_INTERVAL, coerce_int(fast_value, default=fast_default)),
    )
    slow_floor = max(MIN_SLOW_POLL_INTERVAL, fast)
    slow = min(
        MAX_POLL_INTERVAL,
        max(slow_floor, coerce_int(slow_value, default=slow_default)),
    )
    return fast, slow


def copy_diagnostics_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: copy_diagnostics_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_diagnostics_value(item) for item in value]
    return value


def normalize_iso_date(value: object) -> str | None:
    if value is None:
        return None
    try:
        cleaned = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    if not cleaned:
        return None
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").date().isoformat()
    except Exception:  # noqa: BLE001
        return None


def resolve_inverter_start_date(
    site_energy_meta: object,
    inverter_data: object,
) -> str | None:
    start_date: str | None = None
    if isinstance(site_energy_meta, dict):
        start_date = normalize_iso_date(site_energy_meta.get("start_date"))
    if start_date:
        return start_date

    existing_starts: list[str] = []
    if isinstance(inverter_data, dict):
        for payload in inverter_data.values():
            if not isinstance(payload, dict):
                continue
            normalized = normalize_iso_date(payload.get("lifetime_query_start_date"))
            if normalized:
                existing_starts.append(normalized)
    if existing_starts:
        return min(existing_starts)
    return None


def resolve_site_timezone_name(battery_timezone: object) -> str:
    tz_name = battery_timezone
    if isinstance(tz_name, str) and tz_name.strip():
        try:
            ZoneInfo(tz_name.strip())
        except Exception:  # noqa: BLE001
            pass
        else:
            return tz_name.strip()
    return "UTC"


def resolve_site_local_current_date(
    devices_inventory_payload: object,
    battery_timezone: object,
) -> str:
    inventory_payload = (
        devices_inventory_payload
        if isinstance(devices_inventory_payload, dict)
        else None
    )
    if inventory_payload is not None:
        # Enphase inventory payloads sometimes include the site's current date
        # directly.
        direct = normalize_iso_date(inventory_payload.get("curr_date_site"))
        if direct:
            return direct
        result = inventory_payload.get("result")
        if isinstance(result, list):
            for item in result:
                if not isinstance(item, dict):
                    continue
                candidate = normalize_iso_date(item.get("curr_date_site"))
                if candidate:
                    return candidate

    tz_name = battery_timezone
    if isinstance(tz_name, str) and tz_name.strip():
        try:
            return datetime.now(ZoneInfo(tz_name.strip())).date().isoformat()
        except Exception:  # noqa: BLE001
            pass

    try:
        return dt_util.now().date().isoformat()
    except Exception:  # noqa: BLE001
        return datetime.now(tz=_tz.utc).date().isoformat()


def redact_battery_payload(value: object) -> object:
    """Return a diagnostics-safe copy of nested Enphase payload data."""

    sensitive = {
        "email",
        "authorization",
        "cookie",
        "token",
        "access_token",
        "refresh_token",
        "xsrf_token",
        "x_xsrf_token",
        "session_id",
        "userid",
        "user_id",
        "username",
        "device_link",
        "device_url",
        "href",
        "url",
        "location",
        "redirect_target",
        "interface_ip",
        "ip_addr",
        "gateway_ip_addr",
        "default_route",
        "mac_addr",
        "site",
        "site_id",
        "battery_id",
        "battery_ids",
        "serial",
        "serials",
        "serial_number",
        "data_site_id",
        "data_battery_id",
    }
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.strip().lower().replace("-", "_") in sensitive:
                out[key_text] = "[redacted]"
            else:
                out[key_text] = redact_battery_payload(item)
        return out
    if isinstance(value, list):
        return [redact_battery_payload(item) for item in value]
    return value
