"""Payload parser and normalizer helpers for Enphase API endpoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TypedDict

from .api_models import ChargerInfo, SiteInfo
from .runtime_helpers import coerce_optional_text

type PayloadMap = dict[str, object]
type DayEnergyValues = dict[str, float]
type DateKeyParser = Callable[[object], str | None]
type EnergyCoercer = Callable[..., float | None]


class LatestPowerPayload(TypedDict, total=False):
    """Payload shape returned by latest-power normalizers."""

    value: float
    units: str
    precision: int
    time: int


class HeatpumpSGReadyModeDetails(TypedDict):
    """Normalized SG Ready details derived from raw HEMS labels."""

    sg_ready_mode_label: str | None
    sg_ready_active: bool | None
    sg_ready_contact_state: str | None


@dataclass(slots=True, frozen=True)
class LatestPowerSample:
    """Normalized latest power sample."""

    value: float
    units: str | None = None
    precision: int | None = None
    time: int | None = None

    def to_dict(self) -> LatestPowerPayload:
        """Return the payload shape used by existing API consumers."""

        payload: LatestPowerPayload = {"value": self.value}
        if self.units is not None:
            payload["units"] = self.units
        if self.precision is not None:
            payload["precision"] = self.precision
        if self.time is not None:
            payload["time"] = self.time
        return payload


@dataclass(slots=True, frozen=True)
class EVSEDailyEnergyEntry:
    """Normalized EVSE daily energy timeseries for one charger."""

    serial: str
    day_values_kwh: DayEnergyValues
    energy_kwh: float | None
    current_value_kwh: float | None
    metadata: PayloadMap

    def to_dict(self) -> PayloadMap:
        """Return the payload shape used by existing API consumers."""

        return {
            "serial": self.serial,
            "day_values_kwh": self.day_values_kwh,
            "energy_kwh": self.energy_kwh,
            "current_value_kwh": self.current_value_kwh,
            **self.metadata,
        }


@dataclass(slots=True, frozen=True)
class EVSELifetimeEnergyEntry:
    """Normalized EVSE lifetime energy sample for one charger."""

    serial: str
    energy_kwh: float
    metadata: PayloadMap

    def to_dict(self) -> PayloadMap:
        """Return the payload shape used by existing API consumers."""

        return {"serial": self.serial, "energy_kwh": self.energy_kwh, **self.metadata}


@dataclass(slots=True, frozen=True)
class HEMSHeatpumpState:
    """Normalized HEMS heat pump runtime state."""

    payload: PayloadMap

    def to_dict(self) -> PayloadMap:
        """Return the payload shape used by existing API consumers."""

        return dict(self.payload)


@dataclass(slots=True, frozen=True)
class HEMSDailyConsumptionEntry:
    """Normalized HEMS daily consumption entry for one device."""

    device_uid: str | None
    device_name: str | None
    consumption: list[PayloadMap]

    def to_dict(self) -> PayloadMap:
        """Return the payload shape used by existing API consumers."""

        return {
            "device_uid": self.device_uid,
            "device_name": self.device_name,
            "consumption": self.consumption,
        }


def normalize_sites(payload: object) -> list[SiteInfo]:
    """Normalize Enlighten site search payload variants."""

    data = payload
    if isinstance(data, dict):
        # Enlighten account search has returned each of these wrapper keys in
        # different browser and API flows.
        for key in ("sites", "data", "items", "systems"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        return []

    sites: dict[str, SiteInfo] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        site_id = (
            item.get("site_id")
            or item.get("siteId")
            or item.get("id")
            or item.get("system_id")
        )
        if site_id is None:
            continue
        site_id_text = str(site_id)
        name = (
            item.get("name")
            or item.get("site_name")
            or item.get("siteName")
            or item.get("title")
            or item.get("displayName")
            or item.get("display_name")
        )
        name_text = str(name) if name else None
        if site_id_text in sites:
            if not sites[site_id_text].name and name_text:
                sites[site_id_text].name = name_text
            continue
        sites[site_id_text] = SiteInfo(site_id=site_id_text, name=name_text or None)
    return list(sites.values())


def normalize_chargers(payload: object) -> list[ChargerInfo]:
    """Normalize charger inventory payload variants."""

    data = payload
    if isinstance(data, dict):
        nested = data.get("data")
        if nested:
            data = nested

    if isinstance(data, dict):
        # Charger discovery may come from EVSE-specific or generic inventory
        # routes, so accept both naming families.
        for key in ("chargers", "evChargerData", "evses", "devices", "items"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        return []

    chargers: list[ChargerInfo] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        serial = (
            item.get("serial")
            or item.get("serialNumber")
            or item.get("sn")
            or item.get("id")
        )
        if not serial:
            continue
        serial_text = str(serial)
        name = (
            item.get("name")
            or item.get("displayName")
            or item.get("display_name")
            or item.get("title")
        )
        name_text = str(name) if name else None
        chargers.append(ChargerInfo(serial=serial_text, name=name_text or None))
    return chargers


def coerce_lifetime_energy_value(value: object) -> float | None:
    """Normalize numeric lifetime-energy values into float samples."""

    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except Exception:  # noqa: BLE001
            return None
    return None


def coerce_non_boolean_number(value: object) -> float | None:
    """Normalize numeric values while rejecting JSON booleans."""

    if isinstance(value, bool):
        return None
    return coerce_lifetime_energy_value(value)


def parse_latest_power_payload(payload: object) -> LatestPowerSample | None:
    """Parse app-api latest power payloads into a typed sample."""

    data = payload
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data.get("data")
    if not isinstance(data, dict):
        return None

    latest = data.get("latest_power")
    if not isinstance(latest, dict):
        latest = data

    value = coerce_non_boolean_number(latest.get("value"))
    if value is None:
        return None

    units_text: str | None = None
    units = latest.get("units")
    if units is not None:
        try:
            normalized_units = str(units).strip()
        except Exception:  # noqa: BLE001
            normalized_units = ""
        if normalized_units:
            units_text = normalized_units

    precision_int: int | None = None
    precision = coerce_non_boolean_number(latest.get("precision"))
    if precision is not None:
        try:
            precision_int = int(precision)
        except Exception:  # noqa: BLE001
            precision_int = None

    sample_time_int: int | None = None
    sample_time = latest.get("time")
    if sample_time is not None:
        sample_time_val = coerce_non_boolean_number(sample_time)
        if sample_time_val is not None:
            if sample_time_val > 10**12:
                # Some app-api payloads report milliseconds instead of seconds.
                sample_time_val /= 1000.0
            try:
                sample_time_int = int(sample_time_val)
            except Exception:  # noqa: BLE001
                sample_time_int = None

    return LatestPowerSample(
        value=value,
        units=units_text,
        precision=precision_int,
        time=sample_time_int,
    )


def normalize_latest_power_payload(payload: object) -> LatestPowerPayload | None:
    """Normalize app-api latest power payloads into a common shape."""

    sample = parse_latest_power_payload(payload)
    return sample.to_dict() if sample is not None else None


def normalize_lifetime_energy_payload(payload: object) -> PayloadMap | None:
    """Normalize site/HEMS lifetime-energy payloads into a common shape."""

    data = payload
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data.get("data")
    if not isinstance(data, dict):
        return None

    array_fields = {
        "production",
        "consumption",
        "solar_home",
        "solar_grid",
        "generator_grid",
        "grid_home",
        "import",
        "export",
        "charge",
        "discharge",
        "solar_battery",
        "battery_home",
        "battery_grid",
        "grid_battery",
        "evse",
        "heatpump",
        "water_heater",
    }
    array_field_aliases = {
        # HEMS and site-energy payloads use slightly different names for the
        # same flows depending on endpoint family.
        "evse_charging": "evse",
        "heat_pump": "heatpump",
        "heat-pump": "heatpump",
        "waterheater": "water_heater",
        "water-heater": "water_heater",
        "water_heater_consumption": "water_heater",
    }
    metadata_fields = {
        "start_date",
        "last_report_date",
        "update_pending",
        "system_id",
    }
    metadata_aliases = {
        "startDate": "start_date",
        "lastReportDate": "last_report_date",
        "updatePending": "update_pending",
        "systemId": "system_id",
    }

    normalized: PayloadMap = {}
    for key, value in data.items():
        canonical_key = array_field_aliases.get(key, key)
        if canonical_key in array_fields:
            if (
                canonical_key != key
                and canonical_key in data
                and canonical_key in normalized
            ):
                continue
            if isinstance(value, list):
                normalized[canonical_key] = [
                    coerce_lifetime_energy_value(v) for v in value
                ]
            else:
                normalized[canonical_key] = []
            continue
        if key in metadata_fields:
            normalized[key] = value
            continue
        canonical_meta = metadata_aliases.get(key)
        if canonical_meta and canonical_meta not in normalized:
            normalized[canonical_meta] = value

    interval_minutes = coerce_lifetime_energy_value(
        data.get("interval_minutes")
        or data.get("interval")
        or data.get("interval_min")
        or data.get("intervalMinutes")
    )
    if interval_minutes is not None and interval_minutes > 0:
        normalized["interval_minutes"] = interval_minutes
    return normalized


def normalize_evse_timeseries_serial(value: object) -> str | None:
    """Return a normalized EVSE serial string."""

    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None


def parse_evse_timeseries_date_key(value: object) -> str | None:
    """Normalize a date-like EVSE timeseries key to YYYY-MM-DD."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        try:
            ts_val = float(value)
            if ts_val > 10**12:
                # Timeseries keys have appeared as Unix milliseconds.
                ts_val /= 1000.0
            return datetime.fromtimestamp(ts_val, tz=timezone.utc).date().isoformat()
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) >= 10:
        try:
            return (
                datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
                .date()
                .isoformat()
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            datetime.strptime(cleaned[:10], "%Y-%m-%d")
            return cleaned[:10]
        except Exception:  # noqa: BLE001
            pass
    return None


def coerce_evse_timeseries_energy(
    value: object,
    *,
    key_hint: str | None = None,
    unit_hint: object | None = None,
) -> float | None:
    """Normalize EVSE timeseries energy to kWh."""

    numeric = coerce_lifetime_energy_value(value)
    if numeric is None:
        return None
    try:
        unit_text = str(unit_hint).strip().lower() if unit_hint is not None else ""
    except Exception:  # noqa: BLE001
        unit_text = ""
    hint = (key_hint or "").lower()
    if "wh" in hint and "kwh" not in hint:
        return round(numeric / 1000.0, 6)
    if unit_text in {"wh", "watt_hour", "watt-hours", "watt_hours"}:
        return round(numeric / 1000.0, 6)
    return round(numeric, 6)


def normalize_evse_timeseries_metadata(payload: object) -> PayloadMap:
    """Normalize shared EVSE timeseries metadata."""

    if not isinstance(payload, dict):
        return {}
    interval = coerce_lifetime_energy_value(
        payload.get("interval_minutes")
        or payload.get("interval")
        or payload.get("interval_min")
        or payload.get("intervalMinutes")
    )
    metadata: PayloadMap = {}
    if interval is not None and interval > 0:
        metadata["interval_minutes"] = interval
    last_report = (
        payload.get("last_report_date")
        or payload.get("lastReportDate")
        or payload.get("last_reported_at")
        or payload.get("lastReportedAt")
    )
    if last_report is not None:
        metadata["last_report_date"] = last_report
    return metadata


def daily_values_from_mapping(
    payload: PayloadMap,
    *,
    parse_date_key: DateKeyParser = parse_evse_timeseries_date_key,
    coerce_energy: EnergyCoercer = coerce_evse_timeseries_energy,
) -> tuple[DayEnergyValues, float | None]:
    """Extract per-day EVSE values from a mapping payload."""

    day_values: DayEnergyValues = {}
    current_value: float | None = None
    unit_hint = payload.get("unit") or payload.get("source_unit")
    for key, raw in payload.items():
        day_key = parse_date_key(key)
        if day_key is None:
            continue
        numeric = coerce_energy(raw, key_hint=str(key), unit_hint=unit_hint)
        if numeric is None:
            continue
        day_values[day_key] = numeric
    for key in (
        "energy_kwh",
        "value_kwh",
        "daily_energy_kwh",
        "daily_kwh",
        "energy",
        "value",
        "energy_wh",
        "daily_energy_wh",
    ):
        if key not in payload:
            continue
        current_value = coerce_energy(
            payload.get(key), key_hint=key, unit_hint=unit_hint
        )
        break
    return day_values, current_value


def daily_values_from_sequence(
    values: list[object],
    *,
    start_date_value: object | None = None,
    unit_hint: object | None = None,
    parse_date_key: DateKeyParser = parse_evse_timeseries_date_key,
    coerce_energy: EnergyCoercer = coerce_evse_timeseries_energy,
) -> tuple[DayEnergyValues, float | None]:
    """Extract per-day EVSE values from a sequence payload."""

    day_values: DayEnergyValues = {}
    current_value: float | None = None
    start_day = parse_date_key(start_date_value)
    start_dt = None
    if start_day is not None:
        try:
            start_dt = datetime.fromisoformat(start_day)
        except Exception:  # noqa: BLE001
            start_dt = None
    for idx, item in enumerate(values):
        if isinstance(item, dict):
            day_key = parse_date_key(
                item.get("date")
                or item.get("day")
                or item.get("start_date")
                or item.get("startDate")
                or item.get("timestamp")
                or item.get("time")
            )
            item_unit = item.get("unit") or unit_hint
            for key in (
                "energy_kwh",
                "value_kwh",
                "daily_energy_kwh",
                "energy",
                "value",
                "energy_wh",
                "daily_energy_wh",
            ):
                if key not in item:
                    continue
                numeric = coerce_energy(
                    item.get(key), key_hint=key, unit_hint=item_unit
                )
                if numeric is None:
                    continue
                if day_key is not None:
                    day_values[day_key] = numeric
                else:
                    current_value = numeric
                break
            continue
        numeric = coerce_energy(item, unit_hint=unit_hint)
        if numeric is None:
            continue
        if start_dt is not None:
            day_values[(start_dt + timedelta(days=idx)).date().isoformat()] = numeric
        else:
            current_value = numeric
    return day_values, current_value


def parse_evse_daily_entry(
    serial: str,
    payload: object,
    *,
    base_metadata: PayloadMap | None = None,
    parse_date_key: DateKeyParser = parse_evse_timeseries_date_key,
    coerce_energy: EnergyCoercer = coerce_evse_timeseries_energy,
) -> EVSEDailyEnergyEntry | None:
    """Parse one EVSE daily timeseries entry."""

    metadata = dict(base_metadata or {})
    day_values: DayEnergyValues = {}
    current_value: float | None = None
    if isinstance(payload, dict):
        metadata.update(normalize_evse_timeseries_metadata(payload))
        record_serial = normalize_evse_timeseries_serial(
            payload.get("serial")
            or payload.get("serial_number")
            or payload.get("device_serial")
            or payload.get("charger_serial")
            or payload.get("sn")
        )
        if record_serial and record_serial != serial:
            serial = record_serial
        nested = (
            payload.get("days")
            or payload.get("daily")
            or payload.get("values")
            or payload.get("series")
            or payload.get("data")
        )
        if isinstance(nested, list):
            day_values, current_value = daily_values_from_sequence(
                nested,
                start_date_value=payload.get("start_date") or payload.get("startDate"),
                unit_hint=payload.get("unit") or payload.get("source_unit"),
                parse_date_key=parse_date_key,
                coerce_energy=coerce_energy,
            )
        elif isinstance(nested, dict):
            day_values, current_value = daily_values_from_mapping(
                nested,
                parse_date_key=parse_date_key,
                coerce_energy=coerce_energy,
            )
        else:
            day_values, current_value = daily_values_from_mapping(
                payload,
                parse_date_key=parse_date_key,
                coerce_energy=coerce_energy,
            )
    elif isinstance(payload, list):
        day_values, current_value = daily_values_from_sequence(
            payload,
            parse_date_key=parse_date_key,
            coerce_energy=coerce_energy,
        )
    else:
        current_value = coerce_energy(payload)
    if not day_values and current_value is None:
        return None
    current_day = max(day_values) if day_values else None
    return EVSEDailyEnergyEntry(
        serial=serial,
        day_values_kwh=day_values,
        energy_kwh=(
            day_values.get(current_day) if current_day is not None else current_value
        ),
        current_value_kwh=current_value,
        metadata=metadata,
    )


def normalize_evse_daily_entry(
    serial: str,
    payload: object,
    *,
    base_metadata: PayloadMap | None = None,
    parse_date_key: DateKeyParser = parse_evse_timeseries_date_key,
    coerce_energy: EnergyCoercer = coerce_evse_timeseries_energy,
) -> PayloadMap | None:
    """Normalize one EVSE daily timeseries entry."""

    entry = parse_evse_daily_entry(
        serial,
        payload,
        base_metadata=base_metadata,
        parse_date_key=parse_date_key,
        coerce_energy=coerce_energy,
    )
    return entry.to_dict() if entry is not None else None


def parse_evse_lifetime_entry(
    serial: str,
    payload: object,
    *,
    base_metadata: PayloadMap | None = None,
    coerce_energy: EnergyCoercer = coerce_evse_timeseries_energy,
) -> EVSELifetimeEnergyEntry | None:
    """Parse one EVSE lifetime timeseries entry."""

    metadata = dict(base_metadata or {})
    energy_kwh: float | None = None
    if isinstance(payload, dict):
        metadata.update(normalize_evse_timeseries_metadata(payload))
        record_serial = normalize_evse_timeseries_serial(
            payload.get("serial")
            or payload.get("serial_number")
            or payload.get("device_serial")
            or payload.get("charger_serial")
            or payload.get("sn")
        )
        if record_serial and record_serial != serial:
            serial = record_serial
        unit_hint = payload.get("unit") or payload.get("source_unit")
        for key in (
            "energy_kwh",
            "value_kwh",
            "lifetime_energy_kwh",
            "lifetime_kwh",
            "total_kwh",
            "energy_wh",
            "lifetime_energy_wh",
            "value_wh",
            "energy",
            "value",
        ):
            if key not in payload:
                continue
            energy_kwh = coerce_energy(
                payload.get(key), key_hint=key, unit_hint=unit_hint
            )
            if energy_kwh is not None:
                break
        if energy_kwh is None:
            values = (
                payload.get("values") or payload.get("series") or payload.get("data")
            )
            if isinstance(values, list):
                for item in reversed(values):
                    if isinstance(item, dict):
                        for key in (
                            "energy_kwh",
                            "value_kwh",
                            "lifetime_energy_kwh",
                            "energy_wh",
                            "value_wh",
                            "value",
                        ):
                            if key not in item:
                                continue
                            energy_kwh = coerce_energy(
                                item.get(key),
                                key_hint=key,
                                unit_hint=item.get("unit") or unit_hint,
                            )
                            if energy_kwh is not None:
                                break
                        if energy_kwh is not None:
                            break
                    else:
                        energy_kwh = coerce_energy(item, unit_hint=unit_hint)
                        if energy_kwh is not None:
                            break
    else:
        energy_kwh = coerce_energy(payload)
    if energy_kwh is None:
        return None
    return EVSELifetimeEnergyEntry(
        serial=serial,
        energy_kwh=energy_kwh,
        metadata=metadata,
    )


def normalize_evse_lifetime_entry(
    serial: str,
    payload: object,
    *,
    base_metadata: PayloadMap | None = None,
    coerce_energy: EnergyCoercer = coerce_evse_timeseries_energy,
) -> PayloadMap | None:
    """Normalize one EVSE lifetime timeseries entry."""

    entry = parse_evse_lifetime_entry(
        serial,
        payload,
        base_metadata=base_metadata,
        coerce_energy=coerce_energy,
    )
    return entry.to_dict() if entry is not None else None


def normalize_evse_timeseries_payload(
    payload: object,
    *,
    daily: bool,
    parse_date_key: DateKeyParser = parse_evse_timeseries_date_key,
    coerce_energy: EnergyCoercer = coerce_evse_timeseries_energy,
) -> dict[str, PayloadMap] | None:
    """Normalize EVSE timeseries payloads keyed by charger serial."""

    data = payload
    if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
        data = data.get("data")
    base_metadata = normalize_evse_timeseries_metadata(
        payload if isinstance(payload, dict) else {}
    )
    if isinstance(data, dict):
        candidates = (
            data.get("results")
            or data.get("chargers")
            or data.get("devices")
            or data.get("timeseries")
        )
        if isinstance(candidates, list):
            data = candidates
    normalized: dict[str, PayloadMap] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            serial = normalize_evse_timeseries_serial(
                item.get("serial")
                or item.get("serial_number")
                or item.get("device_serial")
                or item.get("charger_serial")
                or item.get("sn")
            )
            if not serial:
                continue
            entry = (
                normalize_evse_daily_entry(
                    serial,
                    item,
                    base_metadata=base_metadata,
                    parse_date_key=parse_date_key,
                    coerce_energy=coerce_energy,
                )
                if daily
                else normalize_evse_lifetime_entry(
                    serial,
                    item,
                    base_metadata=base_metadata,
                    coerce_energy=coerce_energy,
                )
            )
            if entry is not None:
                normalized[serial] = entry
        return normalized
    if not isinstance(data, dict):
        return None
    for key, value in data.items():
        serial = normalize_evse_timeseries_serial(key)
        if not serial:
            continue
        entry = (
            normalize_evse_daily_entry(
                serial,
                value,
                base_metadata=base_metadata,
                parse_date_key=parse_date_key,
                coerce_energy=coerce_energy,
            )
            if daily
            else normalize_evse_lifetime_entry(
                serial,
                value,
                base_metadata=base_metadata,
                coerce_energy=coerce_energy,
            )
        )
        if entry is None:
            continue
        normalized[serial] = entry
    return normalized


def clean_optional_text(value: object) -> str | None:
    """Return a trimmed string value when present."""

    return coerce_optional_text(value)


def heatpump_sg_ready_mode_details(value: object) -> HeatpumpSGReadyModeDetails:
    """Map raw HEMS SG Ready mode labels to app-facing semantics."""

    text = clean_optional_text(value)
    if text is None:
        return {
            "sg_ready_mode_label": None,
            "sg_ready_active": None,
            "sg_ready_contact_state": None,
        }
    normalized = text.upper()
    if normalized == "MODE_2":
        return {
            "sg_ready_mode_label": "Normal",
            "sg_ready_active": False,
            "sg_ready_contact_state": "open",
        }
    if normalized == "MODE_3":
        return {
            "sg_ready_mode_label": "Recommended",
            "sg_ready_active": True,
            "sg_ready_contact_state": "closed",
        }
    return {
        "sg_ready_mode_label": None,
        "sg_ready_active": None,
        "sg_ready_contact_state": None,
    }


def parse_hems_heatpump_state_payload(payload: object) -> HEMSHeatpumpState | None:
    """Parse HEMS heat-pump runtime state payloads."""

    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    device_uid = clean_optional_text(
        data.get("device_uid")
        if data.get("device_uid") is not None
        else data.get("device-uid")
    )
    heatpump_status = clean_optional_text(
        data.get("heatpump_status")
        if data.get("heatpump_status") is not None
        else data.get("heatpump-status")
    )
    sg_ready_mode_raw = clean_optional_text(
        data.get("sg_ready_mode")
        if data.get("sg_ready_mode") is not None
        else data.get("sg-ready-mode")
    )
    details = heatpump_sg_ready_mode_details(sg_ready_mode_raw)
    endpoint_type = clean_optional_text(payload.get("type"))
    endpoint_timestamp = payload.get("timestamp")
    return HEMSHeatpumpState(
        {
            "type": endpoint_type,
            "timestamp": endpoint_timestamp,
            "endpoint_type": endpoint_type,
            "endpoint_timestamp": endpoint_timestamp,
            "device_uid": device_uid,
            "heatpump_status": heatpump_status,
            "sg_ready_mode_raw": sg_ready_mode_raw,
            "sg_ready_mode_label": details.get("sg_ready_mode_label"),
            "sg_ready_active": details.get("sg_ready_active"),
            "sg_ready_contact_state": details.get("sg_ready_contact_state"),
            "vpp_sgready_mode_override": clean_optional_text(
                data.get("vpp_sgready_mode_override")
                if data.get("vpp_sgready_mode_override") is not None
                else data.get("vpp-sgready-mode-override")
            ),
            "last_report_at": (
                data.get("last_report_at")
                if data.get("last_report_at") is not None
                else data.get("last-report-at")
            ),
        }
    )


def normalize_hems_heatpump_state_payload(payload: object) -> PayloadMap | None:
    """Normalize HEMS heat-pump runtime state payloads."""

    state = parse_hems_heatpump_state_payload(payload)
    return state.to_dict() if state is not None else None


def parse_hems_daily_consumption_entry(
    payload: object,
) -> HEMSDailyConsumptionEntry | None:
    """Parse a HEMS daily-consumption device entry."""

    if not isinstance(payload, dict):
        return None
    device_uid = clean_optional_text(
        payload.get("device_uid")
        if payload.get("device_uid") is not None
        else payload.get("device-uid")
    )
    device_name = clean_optional_text(
        payload.get("device_name")
        if payload.get("device_name") is not None
        else payload.get("device-name")
    )
    buckets: list[PayloadMap] = []
    raw_buckets = payload.get("consumption")
    if isinstance(raw_buckets, list):
        for item in raw_buckets:
            if not isinstance(item, dict):
                continue
            bucket: PayloadMap = {
                "solar": coerce_lifetime_energy_value(item.get("solar")),
                "battery": coerce_lifetime_energy_value(item.get("battery")),
                "grid": coerce_lifetime_energy_value(item.get("grid")),
                "details": [],
            }
            details = item.get("details")
            if isinstance(details, list):
                bucket["details"] = [
                    coerce_lifetime_energy_value(detail) for detail in details
                ]
            buckets.append(bucket)
    return HEMSDailyConsumptionEntry(
        device_uid=device_uid,
        device_name=device_name,
        consumption=buckets,
    )


def normalize_hems_daily_consumption_entry(
    payload: object,
) -> PayloadMap | None:
    """Normalize a HEMS daily-consumption device entry."""

    entry = parse_hems_daily_consumption_entry(payload)
    return entry.to_dict() if entry is not None else None


def normalize_hems_energy_consumption_payload(payload: object) -> PayloadMap | None:
    """Normalize HEMS daily energy-consumption payloads."""

    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload

    endpoint_type = clean_optional_text(payload.get("type"))
    endpoint_timestamp = payload.get("timestamp")
    families: PayloadMap = {
        "heat-pump": [],
        "evse": [],
        "water-heater": [],
    }
    normalized: PayloadMap = {
        "type": endpoint_type,
        "timestamp": endpoint_timestamp,
        "endpoint_type": endpoint_type,
        "endpoint_timestamp": endpoint_timestamp,
        "data": families,
    }
    for family_key in ("heat-pump", "evse", "water-heater"):
        raw_family = data.get(family_key)
        if raw_family is None:
            raw_family = data.get(family_key.replace("-", "_"))
        if not isinstance(raw_family, list):
            continue
        entries: list[PayloadMap] = []
        for item in raw_family:
            normalized_entry = normalize_hems_daily_consumption_entry(item)
            if normalized_entry is not None:
                entries.append(normalized_entry)
        families[family_key] = entries
    return normalized


def normalize_pv_system_today_payload(payload: object) -> PayloadMap | None:
    """Normalize site-today payloads used by heat-pump daily totals."""

    if not isinstance(payload, dict):
        return None
    stats = payload.get("stats")
    normalized_stats: list[PayloadMap] = []
    if isinstance(stats, list):
        for item in stats:
            if not isinstance(item, dict):
                continue
            normalized_stat = dict(item)
            for key in ("heatpump", "heat_pump", "heat-pump"):
                value = item.get(key)
                if value is None:
                    continue
                normalized_stat["heatpump"] = value
                break
            normalized_stats.append(normalized_stat)
    normalized = dict(payload)
    normalized["stats"] = normalized_stats
    return normalized
