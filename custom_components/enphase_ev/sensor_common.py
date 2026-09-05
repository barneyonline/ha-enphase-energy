"""Shared sensor presentation and energy normalization contracts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar, cast

from homeassistant.core import callback as ha_callback

from . import sensor_battery_helpers as _battery_helpers
from .coordinator import EnphaseCoordinator
from .labels import friendly_status_text, status_label

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])


callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)


_battery_parse_timestamp = _battery_helpers.battery_parse_timestamp


def _type_label(coord: EnphaseCoordinator, type_key: str) -> str | None:
    return coord.inventory_view.type_label(type_key)


def _has_type(coord: EnphaseCoordinator, type_key: str) -> bool:
    return bool(coord.inventory_view.has_type(type_key))


def _lifetime_energy_delta(
    *,
    current_kwh: float,
    previous_kwh: float | None,
    reset_drop_kwh: float,
) -> tuple[float | None, bool]:
    """Return delta kWh and whether the cumulative meter appears to have reset."""

    if previous_kwh is None:
        return None, False
    delta_kwh = current_kwh - previous_kwh
    return delta_kwh, delta_kwh < -reset_drop_kwh


def _resolve_lifetime_power_window(
    *,
    sample_ts: float,
    previous_energy_ts: float | None,
    default_window_s: float,
) -> float:
    """Return the elapsed sampling window used for dE/dt calculations."""

    if previous_energy_ts is not None and sample_ts > previous_energy_ts:
        window_s = sample_ts - previous_energy_ts
    else:
        window_s = default_window_s
    return window_s if window_s > 0 else default_window_s


def _energy_delta_to_power_w(
    delta_kwh: float,
    *,
    window_s: float,
    floor_zero: bool = False,
    max_watts: float | None = None,
) -> int:
    """Convert an energy delta over a window into watts."""

    watts = (delta_kwh * 3_600_000.0) / window_s
    if floor_zero and watts < 0:
        watts = 0
    if max_watts is not None and watts > max_watts:
        watts = max_watts
    return int(round(watts))


def _restore_optional_float_attribute(
    attrs: dict[str, object],
    key: str,
) -> float | None:
    """Best-effort restore of a float-like state attribute."""

    raw_value = attrs.get(key)
    if raw_value is None:
        return None
    try:
        return float(raw_value)  # type: ignore[arg-type]
    except Exception:
        return None


def _restore_optional_int_value(raw_value: object) -> int | None:
    """Best-effort restore of an int-like state value."""

    if raw_value is None:
        return None
    try:
        return int(round(float(raw_value)))  # type: ignore[arg-type]
    except Exception:
        return None


def _normalize_utc_datetime(value: object) -> datetime | None:
    """Return a timezone-aware UTC datetime when value is datetime-like."""

    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _title_case_status(value: object, hass: object | None = None) -> str | None:
    return status_label(value, hass=hass) or friendly_status_text(value)
