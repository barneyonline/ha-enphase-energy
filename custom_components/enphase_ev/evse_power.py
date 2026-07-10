"""Pure EVSE power derivation helpers."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone as _tz
from typing import Mapping

from .const import DEFAULT_NOMINAL_VOLTAGE
from .evse_runtime import evse_power_is_actively_charging

_MAX_THROUGHPUT_W = 19200
_THREE_PHASE_LINE_TO_LINE_MULTIPLIER = 1.7320508075688772


def _power_parse_timestamp(raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            value = float(raw)
        except Exception:
            return None
        if value > 10**12:
            value = value / 1000.0
        if value <= 0:
            return None
        try:
            datetime.fromtimestamp(value, tz=_tz.utc)
        except Exception:
            return None
        return value
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        normalized = stripped.replace("[UTC]", "").replace("Z", "+00:00")
        try:
            dt_obj = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=_tz.utc)
        return dt_obj.astimezone(_tz.utc).timestamp()
    return None


def _power_as_float(raw: object) -> float | None:
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return None


def _power_as_int(raw: object) -> int | None:
    try:
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return None


def _power_topology(entry: Mapping[str, object]) -> str:
    phase_mode = entry.get("phase_mode")
    if phase_mode is not None:
        try:
            normalized = (
                str(phase_mode).strip().lower().replace("-", "_").replace(" ", "_")
            )
        except Exception:  # noqa: BLE001
            normalized = ""
        if normalized:
            if normalized in {"3", "3_phase", "three", "three_phase"}:
                return "three_phase"
            if normalized in {"split", "split_phase"}:
                return "split_phase"
            if normalized in {"1", "single", "single_phase"}:
                return "single_phase"
    phase_count = _power_as_int(entry.get("phase_count"))
    if phase_count is not None:
        if phase_count >= 3:
            return "three_phase"
        if phase_count == 1:
            return "single_phase"
    return "unknown"


def _three_phase_multiplier(entry: Mapping[str, object]) -> float:
    wiring = entry.get("wiring_configuration")
    explicit_neutral = False
    if isinstance(wiring, dict):
        for raw in (*wiring.keys(), *wiring.values()):
            try:
                token = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
            except Exception:  # noqa: BLE001
                continue
            if token in {"n", "neutral", "l1n", "l2n", "l3n", "ln"}:
                explicit_neutral = True
                break
    return 3.0 if explicit_neutral else _THREE_PHASE_LINE_TO_LINE_MULTIPLIER


def _resolve_max_throughput(
    entry: Mapping[str, object],
    nominal_voltage: object = DEFAULT_NOMINAL_VOLTAGE,
) -> tuple[int, str, float | None, float, int, str, float]:
    voltage = _power_as_float(entry.get("operating_v"))
    if voltage is None or voltage <= 0:
        voltage = _power_as_float(entry.get("nominal_v"))
    if voltage is None or voltage <= 0:
        voltage = float(str(nominal_voltage))
    topology = _power_topology(entry)
    phase_multiplier = 1.0
    for source, raw in (
        ("session_charge_level", entry.get("session_charge_level")),
        ("charging_level", entry.get("charging_level")),
        ("max_amp", entry.get("max_amp")),
        ("max_current", entry.get("max_current")),
    ):
        amps = _power_as_float(raw)
        if amps is None or amps <= 0:
            continue
        if topology == "three_phase":
            phase_multiplier = _three_phase_multiplier(entry)
        unbounded = int(round(voltage * amps * phase_multiplier))
        if unbounded <= 0:
            continue
        bounded = min(unbounded, _MAX_THROUGHPUT_W)
        return (
            bounded,
            source,
            amps,
            voltage,
            unbounded,
            topology,
            phase_multiplier,
        )
    return (
        _MAX_THROUGHPUT_W,
        "static_default",
        None,
        voltage,
        _MAX_THROUGHPUT_W,
        topology,
        1.0,
    )


def _is_actually_charging(entry: Mapping[str, object]) -> bool:
    if "actual_charging" in entry:
        return bool(entry.get("actual_charging"))
    return bool(
        evse_power_is_actively_charging(
            entry.get("connector_status"),
            entry.get("charging"),
            suspended_by_evse=entry.get("suspended_by_evse"),
        )
    )


def _known_previous_charging_state(entry: Mapping[str, object] | None) -> bool | None:
    if not isinstance(entry, Mapping):
        return None
    if not any(
        key in entry
        for key in (
            "connector_status",
            "charging",
            "actual_charging",
            "suspended_by_evse",
        )
    ):
        return None
    return _is_actually_charging(entry)


def build_evse_power_snapshot(
    current_entry: Mapping[str, object],
    previous_entry: Mapping[str, object] | None,
    previous_power_snapshot: Mapping[str, object] | None,
    nominal_voltage: object = DEFAULT_NOMINAL_VOLTAGE,
) -> dict[str, object]:
    """Build the derived EVSE power/status snapshot for one charger entry."""

    previous = previous_power_snapshot or {}
    (
        max_watts,
        max_source,
        max_amps,
        max_voltage,
        max_unbounded,
        max_topology,
        max_phase_multiplier,
    ) = _resolve_max_throughput(current_entry, nominal_voltage)
    snapshot: dict[str, object] = {
        "derived_power_max_throughput_w": max_watts,
        "derived_power_max_throughput_unbounded_w": max_unbounded,
        "derived_power_max_throughput_source": max_source,
        "derived_power_max_throughput_amps": max_amps,
        "derived_power_max_throughput_voltage": max_voltage,
        "derived_power_max_throughput_topology": max_topology,
        "derived_power_max_throughput_phase_multiplier": max_phase_multiplier,
    }

    sample_ts = _power_parse_timestamp(current_entry.get("sampled_at_ts"))
    if sample_ts is None:
        sample_ts = _power_parse_timestamp(current_entry.get("sampled_at_utc"))
    if sample_ts is None:
        sample_ts = _power_parse_timestamp(current_entry.get("last_reported_at"))
    sample_iso = (
        datetime.fromtimestamp(sample_ts, tz=_tz.utc).isoformat()
        if sample_ts is not None
        else None
    )
    lifetime = _power_as_float(current_entry.get("lifetime_kwh"))
    is_charging = _is_actually_charging(current_entry)
    previous_is_charging = _known_previous_charging_state(previous_entry)

    last_power_w = _power_as_int(previous.get("derived_power_w"))
    if last_power_w is None:
        last_power_w = 0
    last_method = previous.get("derived_power_method")
    if not isinstance(last_method, str):
        last_method = "seeded"
    last_window_s = _power_as_float(previous.get("derived_power_window_seconds"))
    last_lifetime_kwh = _power_as_float(previous.get("derived_last_lifetime_kwh"))
    last_energy_ts = _power_parse_timestamp(previous.get("derived_last_energy_ts"))
    last_reset_at = _power_parse_timestamp(previous.get("derived_last_reset_at"))
    prior_sample_ts = _power_parse_timestamp(previous.get("derived_last_sample_ts"))
    lifetime_changed = (lifetime is None) != (last_lifetime_kwh is None) or (
        lifetime is not None
        and last_lifetime_kwh is not None
        and abs(lifetime - last_lifetime_kwh) > 1e-9
    )
    if (
        sample_ts is not None
        and prior_sample_ts is not None
        and sample_ts == prior_sample_ts
        and not lifetime_changed
    ):
        snapshot.update(previous)
        snapshot.update(
            {
                "derived_sampled_at_utc": sample_iso,
                "derived_last_sample_ts": sample_ts,
                "derived_power_max_throughput_w": max_watts,
                "derived_power_max_throughput_unbounded_w": max_unbounded,
                "derived_power_max_throughput_source": max_source,
                "derived_power_max_throughput_amps": max_amps,
                "derived_power_max_throughput_voltage": max_voltage,
                "derived_power_max_throughput_topology": max_topology,
                "derived_power_max_throughput_phase_multiplier": max_phase_multiplier,
            }
        )
        if not is_charging:
            snapshot["derived_power_w"] = 0
            snapshot["derived_power_method"] = "idle"
            snapshot["derived_power_window_seconds"] = None
        return snapshot

    snapshot.update(
        {
            "derived_sampled_at_utc": sample_iso,
            "derived_last_sample_ts": sample_ts,
            "derived_last_lifetime_kwh": last_lifetime_kwh,
            "derived_last_energy_ts": last_energy_ts,
            "derived_last_reset_at": last_reset_at,
            "derived_power_w": last_power_w,
            "derived_power_window_seconds": last_window_s,
            "derived_power_method": last_method,
        }
    )

    if previous_is_charging is False and is_charging:
        snapshot["derived_power_w"] = 0
        snapshot["derived_power_method"] = "seeded"
        snapshot["derived_power_window_seconds"] = None
        if lifetime is not None:
            snapshot["derived_last_lifetime_kwh"] = lifetime
        if sample_ts is not None:
            snapshot["derived_last_energy_ts"] = sample_ts
        return snapshot

    if lifetime is None:
        if not is_charging:
            snapshot["derived_power_w"] = 0
            snapshot["derived_power_method"] = "idle"
            snapshot["derived_power_window_seconds"] = None
        return snapshot

    if sample_ts is None:
        snapshot["derived_last_lifetime_kwh"] = lifetime
        return snapshot

    if last_lifetime_kwh is None:
        snapshot["derived_last_lifetime_kwh"] = lifetime
        snapshot["derived_last_energy_ts"] = sample_ts
        snapshot["derived_power_w"] = 0
        snapshot["derived_power_method"] = "seeded"
        snapshot["derived_power_window_seconds"] = None
        return snapshot

    delta_kwh = lifetime - last_lifetime_kwh
    if delta_kwh < -0.25:
        snapshot["derived_last_lifetime_kwh"] = lifetime
        snapshot["derived_last_energy_ts"] = sample_ts
        snapshot["derived_power_w"] = 0
        snapshot["derived_power_method"] = "lifetime_reset"
        snapshot["derived_power_window_seconds"] = None
        snapshot["derived_last_reset_at"] = sample_ts
        return snapshot

    if not is_charging:
        snapshot["derived_last_lifetime_kwh"] = lifetime
        snapshot["derived_last_energy_ts"] = sample_ts
        snapshot["derived_power_w"] = 0
        snapshot["derived_power_method"] = "idle"
        snapshot["derived_power_window_seconds"] = None
        return snapshot

    if delta_kwh <= 0.0005:
        return snapshot

    window_s = (
        sample_ts - last_energy_ts
        if last_energy_ts is not None and sample_ts > last_energy_ts
        else 300.0
    )
    watts = int(round((delta_kwh * 3_600_000.0) / window_s))
    if watts > max_watts:
        watts = max_watts
    snapshot["derived_power_w"] = watts
    snapshot["derived_power_method"] = "lifetime_energy_window"
    snapshot["derived_power_window_seconds"] = window_s
    snapshot["derived_last_lifetime_kwh"] = lifetime
    snapshot["derived_last_energy_ts"] = sample_ts
    return snapshot
