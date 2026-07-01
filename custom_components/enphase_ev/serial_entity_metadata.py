"""Shared unique-ID metadata for serial-backed Enphase entities."""

from __future__ import annotations

from .const import DOMAIN

CHARGER_BINARY_SENSOR_UNIQUE_SUFFIXES: tuple[str, ...] = (
    "_plugged",
    "_charging",
    "_connected",
)
HISTORICAL_CHARGER_BINARY_SENSOR_UNIQUE_SUFFIXES: tuple[str, ...] = (
    "_commissioned",
    "_charger_problem",
)
CHARGER_SENSOR_UNIQUE_SUFFIXES: tuple[str, ...] = (
    "_energy_today",
    "_connector_status",
    "_electrical_phase",
    "_power",
    "_charge_level",
    "_charging_amps",
    "_last_reported",
    "_last_rpt",
    "_charge_mode",
    "_authentication",
    "_charger_authentication",
    "_status",
    "_lifetime_energy",
    "_lifetime_kwh",
    "_storm_guard_state",
)
HISTORICAL_CHARGER_SENSOR_UNIQUE_SUFFIXES: tuple[str, ...] = (
    "_connector_reason",
    "_session_miles",
    "_plg_in_at",
    "_plg_out_at",
    "_schedule_type",
    "_schedule_start",
    "_schedule_end",
    "_session_kwh",
    "_charging_level",
    "_session_duration",
    "_phase_mode",
    "_max_current",
    "_min_amp",
    "_max_amp",
    "_connection",
    "_reporting_interval",
    "_ip_address",
)
BATTERY_ENTITY_UNIQUE_SUFFIXES: tuple[str, ...] = (
    "_charge_level",
    "_status",
    "_health",
    "_cycle_count",
)
BATTERY_RETIRED_UNIQUE_SUFFIXES: tuple[str, ...] = (
    "_last_reported",
    "_last_reported_at",
)
AC_BATTERY_ENTITY_UNIQUE_SUFFIXES: tuple[str, ...] = (
    "_charge_level",
    "_status",
    "_power",
    "_operating_mode",
    "_cycle_count",
    "_last_reported",
)
AC_BATTERY_RETIRED_UNIQUE_SUFFIXES: tuple[str, ...] = ("_last_reported_at",)
INVERTER_ENTITY_UNIQUE_SUFFIXES: tuple[str, ...] = ("_lifetime_energy",)


def charger_entity_unique_id(serial: str, suffix: str) -> str:
    """Return a per-charger unique ID."""

    return f"{DOMAIN}_{serial}{suffix}"


def charger_entity_unique_ids(
    serial: str,
    suffixes: tuple[str, ...] = CHARGER_SENSOR_UNIQUE_SUFFIXES,
) -> tuple[str, ...]:
    """Return per-charger unique IDs for suffixes."""

    return tuple(charger_entity_unique_id(serial, suffix) for suffix in suffixes)


def site_battery_entity_unique_id(site_id: str, serial: str, suffix: str) -> str:
    """Return a per-storage-battery unique ID."""

    return f"{DOMAIN}_site_{site_id}_battery_{serial}{suffix}"


def site_battery_entity_unique_ids(
    site_id: str,
    serial: str,
    suffixes: tuple[str, ...] = BATTERY_ENTITY_UNIQUE_SUFFIXES,
) -> tuple[str, ...]:
    """Return per-storage-battery unique IDs for suffixes."""

    return tuple(
        site_battery_entity_unique_id(site_id, serial, suffix) for suffix in suffixes
    )


def site_ac_battery_entity_unique_id(site_id: str, serial: str, suffix: str) -> str:
    """Return a per-AC-battery unique ID."""

    return f"{DOMAIN}_site_{site_id}_ac_battery_{serial}{suffix}"


def site_ac_battery_entity_unique_ids(
    site_id: str,
    serial: str,
    suffixes: tuple[str, ...] = AC_BATTERY_ENTITY_UNIQUE_SUFFIXES,
) -> tuple[str, ...]:
    """Return per-AC-battery unique IDs for suffixes."""

    return tuple(
        site_ac_battery_entity_unique_id(site_id, serial, suffix) for suffix in suffixes
    )


def inverter_entity_unique_id(serial: str) -> str:
    """Return a microinverter unique ID."""

    return f"{DOMAIN}_inverter_{serial}_lifetime_energy"


def charger_entity_serial_from_unique_id(
    unique_id: object,
    suffixes: tuple[str, ...],
) -> str | None:
    """Return a bare charger serial from a managed per-charger unique ID."""

    if not isinstance(unique_id, str):
        return None
    unique_prefix = f"{DOMAIN}_"
    if not unique_id.startswith(unique_prefix):
        return None
    if unique_id.startswith(f"{DOMAIN}_site_") or unique_id.startswith(
        f"{DOMAIN}_inverter_"
    ):
        return None
    for suffix in sorted(suffixes, key=len, reverse=True):
        if not unique_id.endswith(suffix):
            continue
        serial = unique_id[len(unique_prefix) : -len(suffix)]
        return serial or None
    return None


def prefixed_serial_from_unique_id(
    unique_id: object,
    *,
    prefix: str,
    suffixes: tuple[str, ...],
    blocked_unique_ids: set[str] | None = None,
) -> str | None:
    """Return a serial from a namespaced unique ID."""

    if not isinstance(unique_id, str) or not unique_id.startswith(prefix):
        return None
    if blocked_unique_ids and unique_id in blocked_unique_ids:
        return None
    for suffix in sorted(suffixes, key=len, reverse=True):
        if not unique_id.endswith(suffix):
            continue
        serial = unique_id[len(prefix) : -len(suffix)]
        return serial or None
    return None


def battery_entity_serial_from_unique_id(
    unique_id: object,
    *,
    site_id: str,
    suffixes: tuple[str, ...],
) -> str | None:
    """Return a storage battery serial parsed from a managed unique ID."""

    prefix = f"{DOMAIN}_site_{site_id}_battery_"
    return prefixed_serial_from_unique_id(
        unique_id,
        prefix=prefix,
        suffixes=suffixes,
        blocked_unique_ids={
            f"{DOMAIN}_site_{site_id}_battery_overall_status",
            f"{DOMAIN}_site_{site_id}_battery_last_reported",
        },
    )


def ac_battery_entity_serial_from_unique_id(
    unique_id: object,
    *,
    site_id: str,
    suffixes: tuple[str, ...],
) -> str | None:
    """Return an AC Battery serial parsed from a managed unique ID."""

    prefix = f"{DOMAIN}_site_{site_id}_ac_battery_"
    return prefixed_serial_from_unique_id(
        unique_id,
        prefix=prefix,
        suffixes=suffixes,
        blocked_unique_ids={
            f"{DOMAIN}_site_{site_id}_ac_battery_overall_status",
            f"{DOMAIN}_site_{site_id}_ac_battery_last_reported",
            f"{DOMAIN}_site_{site_id}_ac_battery_power",
        },
    )


def inverter_entity_serial_from_unique_id(unique_id: object) -> str | None:
    """Return an inverter serial parsed from a managed unique ID."""

    return prefixed_serial_from_unique_id(
        unique_id,
        prefix=f"{DOMAIN}_inverter_",
        suffixes=INVERTER_ENTITY_UNIQUE_SUFFIXES,
    )
