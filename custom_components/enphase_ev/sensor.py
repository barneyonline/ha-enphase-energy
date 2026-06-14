"""Sensor entities for Enphase charger, battery, gateway, and site telemetry.

The module maps normalized coordinator snapshots into Home Assistant sensors,
including restore-state fallbacks for cumulative energy and cloud diagnostic
entities that surface optional endpoint health without exposing credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
import time

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import DistanceConverter

from .ac_battery_support import (
    ac_battery_device_info,
    ac_battery_entities_available,
    ac_battery_last_reported_snapshot,
    ac_battery_snapshot_last_reported,
    ac_battery_storage_snapshot,
)
from .battery_schedule_editor import BatteryScheduleRecord, battery_schedule_inventory
from .const import (
    DEFAULT_NOMINAL_VOLTAGE,
    DOMAIN,
    PHASE_SWITCH_CONFIG_SETTING,
    SAFE_LIMIT_AMPS,
)
from .coordinator import EnphaseCoordinator
from .device_types import is_dry_contact_type_key, member_is_retired
from .device_info_helpers import _cloud_device_info
from .energy import SiteEnergyFlow
from .entity import (
    EnphaseBaseEntity,
    evse_amp_control_applicable,
    evse_resolved_charge_mode,
)
from .labels import friendly_status_text, status_label
from .parsing_helpers import heatpump_status_text
from .runtime_data import EnphaseConfigEntry, get_runtime_data
from .runtime_helpers import (
    coerce_optional_text as _gateway_clean_text,
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
    normalize_evse_session_energy,
)
from .sensor_registry import (
    AC_BATTERY_ENTITY_UNIQUE_SUFFIXES as AC_BATTERY_ENTITY_UNIQUE_SUFFIXES,
    AC_BATTERY_RETIRED_UNIQUE_SUFFIXES as AC_BATTERY_RETIRED_UNIQUE_SUFFIXES,
    BATTERY_ENTITY_UNIQUE_SUFFIXES as BATTERY_ENTITY_UNIQUE_SUFFIXES,
    BATTERY_RETIRED_UNIQUE_SUFFIXES as BATTERY_RETIRED_UNIQUE_SUFFIXES,
    HISTORICAL_CHARGER_SENSOR_UNIQUE_SUFFIXES as HISTORICAL_CHARGER_SENSOR_UNIQUE_SUFFIXES,
    EnphaseSensorRegistrySetup,
)
from .tariff import (
    current_tariff_rate_sensor_spec,
    next_billing_date,
    next_tariff_rate_change,
    tariff_rate_sensor_specs,
)
from . import sensor_battery_helpers as _battery_helpers
from .evse_runtime import evse_power_is_actively_charging

PARALLEL_UPDATES = 0

STATE_NONE = "none"
CLOUD_ERROR_CODE_STATES: tuple[str, ...] = (
    STATE_NONE,
    "rate_limited",
    "auth_blocked",
    "authentication_error",
    "request_error",
    "service_unavailable",
    "invalid_payload",
    "dns_error",
    "network_error",
)
SITE_SERVICE_STATUS_STATES: tuple[str, ...] = ("ok", "degraded", "unknown")
CURRENT_POWER_CACHE_TTL_MULTIPLIER = 2
SITE_LIFETIME_FLOW_BUCKET_LENGTH_KEYS: dict[str, tuple[str, ...]] = {
    "grid_import": ("import", "grid_home", "grid_battery"),
    "grid_export": ("solar_grid",),
    "battery_charge": ("charge", "solar_battery", "grid_battery"),
    "battery_discharge": ("discharge", "battery_home", "battery_grid"),
}
BATTERY_LED_STATUS_STATE_MAP: dict[int, str] = {
    12: "charging",
    13: "discharging",
    14: "idle",
    15: "idle",
    16: "idle",
    17: "idle",
}
_battery_last_reported_members = _battery_helpers.battery_last_reported_members
_battery_last_reported_snapshot = _battery_helpers.battery_last_reported_snapshot
_battery_optional_bool = _battery_helpers.battery_optional_bool
_battery_parse_timestamp = _battery_helpers.battery_parse_timestamp
_battery_snapshot_last_reported = _battery_helpers.battery_snapshot_last_reported


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
        return float(raw_value)
    except Exception:
        return None


def _restore_optional_int_value(raw_value: object) -> int | None:
    """Best-effort restore of an int-like state value."""

    if raw_value is None:
        return None
    try:
        return int(round(float(raw_value)))
    except Exception:
        return None


def _normalize_utc_datetime(value: object) -> datetime | None:
    """Return a timezone-aware UTC datetime when value is datetime-like."""

    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _site_has_battery(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    return has_encharge is not False


def _grid_control_site_applicable(coord: EnphaseCoordinator) -> bool:
    has_encharge = getattr(coord, "battery_has_encharge", None)
    has_enpower = getattr(coord, "battery_has_enpower", None)
    if has_encharge is True or has_enpower is True:
        return True
    if has_encharge is False and has_enpower is False:
        return False
    return _type_available(coord, "encharge")


def _battery_schedule_inventory_supported(coord: EnphaseCoordinator) -> bool:
    client = getattr(coord, "client", None)
    if not (_site_has_battery(coord) and _type_available(coord, "encharge")):
        return False
    if callable(getattr(client, "battery_schedules", None)):
        return True
    if isinstance(getattr(coord, "_battery_schedules_payload", None), dict):
        return True
    return any(
        getattr(coord, attr, None) is not None
        for attr in (
            "_battery_cfg_schedule_id",
            "_battery_dtg_schedule_id",
            "_battery_rbd_schedule_id",
        )
    )


def _tariff_data_available(coord: EnphaseCoordinator) -> bool:
    return any(
        getattr(coord, attr, None) is not None
        for attr in (
            "tariff_billing",
            "tariff_import_rate",
            "tariff_export_rate",
        )
    )


def _tariff_now(coord: EnphaseCoordinator, hass: HomeAssistant | None) -> datetime:
    tz_name = None
    site_tz = getattr(coord, "_site_timezone_name", None)
    if callable(site_tz):
        tz_name = site_tz()
    if not isinstance(tz_name, str) or not tz_name.strip():
        tz_name = getattr(getattr(hass, "config", None), "time_zone", None)
    tzinfo = dt_util.get_time_zone(tz_name) if isinstance(tz_name, str) else None
    return dt_util.now(tzinfo or dt_util.DEFAULT_TIME_ZONE)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    ent_reg = er.async_get(hass)
    registry_setup = EnphaseSensorRegistrySetup(
        ent_reg,
        config_entry_id=entry.entry_id,
        site_id=str(coord.site_id),
    )
    known_site_entity_keys = registry_setup.known_site_entity_keys
    known_type_keys = registry_setup.known_type_keys
    known_gateway_iq_router_keys = registry_setup.known_gateway_iq_router_keys
    _gateway_iq_router_entity_key = registry_setup.gateway_iq_router_entity_key
    _async_prune_removed_gateway_iq_router_entities = (
        registry_setup.prune_removed_gateway_iq_router_entities
    )
    _async_remove_site_sensor_entity = registry_setup.remove_site_sensor_entity
    _async_remove_type_sensor_entity = registry_setup.remove_type_sensor_entity
    _site_sensor_entity_registered = registry_setup.site_sensor_entity_registered
    _async_remove_site_sensor_entities_with_prefix = (
        registry_setup.remove_site_sensor_entities_with_prefix
    )
    _async_prune_dry_contact_type_inventory_entities = (
        registry_setup.prune_dry_contact_type_inventory_entities
    )
    _async_prune_blocked_type_inventory_entities = (
        registry_setup.prune_blocked_type_inventory_entities
    )
    known_serials: set[str] = set()
    known_storm_guard_serials: set[str] = set()
    last_type_key_set: set[str] | None = None
    last_battery_serial_set: set[str] | None = None
    last_ac_battery_serial_set: set[str] | None = None
    last_charger_serial_set: set[str] | None = None
    last_inverter_serial_set: set[str] | None = None

    @callback
    def _async_sync_site_entities() -> None:
        site_entities: list[SensorEntity] = []
        site_has_battery = _site_has_battery(coord)
        gateway_available = _type_available(coord, "envoy")
        battery_device_available = _type_available(coord, "encharge")
        ac_battery_device_available = ac_battery_entities_available(coord)
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        current_router_keys: set[str] = set()
        router_records = _gateway_iq_energy_router_records(coord)
        heatpump_type_present = _has_type(coord, "heatpump")
        energy = getattr(coord, "energy", None)
        site_energy = (
            getattr(energy, "site_energy", None)
            if energy is not None
            else getattr(coord, "site_energy", None)
        )
        if not isinstance(site_energy, dict):
            site_energy = {}
        site_energy_meta = (
            getattr(energy, "site_energy_meta", None)
            if energy is not None
            else getattr(coord, "site_energy_meta", None)
        )
        site_energy_bucket_lengths = (
            site_energy_meta.get("bucket_lengths")
            if isinstance(site_energy_meta, dict)
            else None
        )
        if not isinstance(site_energy_bucket_lengths, dict):
            site_energy_bucket_lengths = {}

        def _gateway_meter_present(meter_kind: str) -> bool | None:
            try:
                return _gateway_meter_member(coord, meter_kind) is not None
            except Exception:  # noqa: BLE001
                return None

        def _gateway_dry_contact_present() -> bool | None:
            try:
                return bool(_gateway_dry_contact_members(coord))
            except Exception:  # noqa: BLE001
                return None

        microinverter_available = bool(getattr(coord, "include_inverters", True)) and (
            _type_available(coord, "microinverter")
        )
        heatpump_available = _type_available(coord, "heatpump")
        heatpump_runtime_available = _heatpump_runtime_device_uid(coord) is not None
        heatpump_site_entity_keys: tuple[str, ...] = (
            "heat_pump_status",
            "heat_pump_connectivity_status",
            "heat_pump_sg_ready_mode",
            "heat_pump_energy_meter",
            "heat_pump_daily_energy",
            "heat_pump_daily_grid_energy",
            "heat_pump_daily_solar_energy",
            "heat_pump_daily_battery_energy",
            "heat_pump_last_reported",
            "heat_pump_power",
            "heat_pump_sg_ready_gateway",
        )
        site_energy_specs: dict[str, tuple[str, str]] = {
            "solar_production": ("site_solar_production", "Site Solar Production"),
            "consumption": ("site_consumption", "Site Consumption"),
            "evse_charging": ("site_evse_charging", "Site EVSE Charging"),
            "heat_pump": ("site_heat_pump_consumption", "Site Heat Pump Consumption"),
            "water_heater": (
                "site_water_heater_consumption",
                "Site Water Heater Consumption",
            ),
            "grid_import": ("site_grid_import", "Site Grid Import"),
            "grid_export": ("site_grid_export", "Site Grid Export"),
            "battery_charge": ("site_battery_charge", "Site Battery Charge"),
            "battery_discharge": ("site_battery_discharge", "Site Battery Discharge"),
        }

        def _add_site_entity(key: str, entity: SensorEntity) -> None:
            if key in known_site_entity_keys:
                return
            site_entities.append(entity)
            known_site_entity_keys.add(key)

        def _site_energy_channel_present(
            flow_key: str, payload_keys: str | tuple[str, ...]
        ) -> bool:
            if flow_key in site_energy:
                return True
            known_channel = getattr(
                getattr(coord, "discovery_snapshot", None),
                "site_energy_channel_known",
                None,
            )
            if callable(known_channel):
                try:
                    if known_channel(flow_key):
                        return True
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(payload_keys, str):
                payload_keys = (payload_keys,)
            for payload_key in payload_keys:
                bucket_length = site_energy_bucket_lengths.get(payload_key)
                try:
                    if int(bucket_length) > 0:
                        return True
                except (TypeError, ValueError):
                    if bucket_length:
                        return True
            return False

        def _site_lifetime_power_channel_present(flow_key: str) -> bool:
            return _site_energy_channel_present(
                flow_key,
                SITE_LIFETIME_FLOW_BUCKET_LENGTH_KEYS.get(flow_key, (flow_key,)),
            )

        _add_site_entity("site_last_update", EnphaseSiteLastUpdateSensor(coord))
        _add_site_entity("site_cloud_latency", EnphaseCloudLatencySensor(coord))
        _add_site_entity(
            "current_production_power",
            EnphaseCurrentPowerConsumptionSensor(coord),
        )
        _async_remove_site_sensor_entity("current_power_consumption")
        if _site_lifetime_power_channel_present(
            "grid_import"
        ) or _site_lifetime_power_channel_present("grid_export"):
            _add_site_entity("grid_power", EnphaseGridPowerSensor(coord))
        else:
            _async_remove_site_sensor_entity("grid_power")
        _async_remove_site_sensor_entity("grid_import_power")
        _async_remove_site_sensor_entity("grid_export_power")
        _add_site_entity("site_last_error_code", EnphaseSiteLastErrorCodeSensor(coord))
        _add_site_entity(
            "site_service_status",
            EnphaseSiteServiceStatusSensor(coord),
        )
        _add_site_entity("site_backoff_ends", EnphaseSiteBackoffEndsSensor(coord))

        if gateway_available:
            _add_site_entity(
                "system_controller_inventory",
                EnphaseSystemControllerInventorySensor(coord),
            )
            dry_contacts_present = _gateway_dry_contact_present()
            if (
                dry_contacts_present is True
                or dry_contacts_present is None
                or not inventory_ready
            ):
                _add_site_entity(
                    "dry_contacts_inventory",
                    EnphaseDryContactsInventorySensor(coord),
                )
            elif inventory_ready:
                _async_remove_site_sensor_entity("dry_contacts_inventory")
            production_meter_present = _gateway_meter_present("production")
            if (
                production_meter_present is True
                or production_meter_present is None
                or not inventory_ready
            ):
                _add_site_entity(
                    "gateway_production_meter",
                    EnphaseGatewayProductionMeterSensor(coord),
                )
            elif inventory_ready:
                _async_remove_site_sensor_entity("gateway_production_meter")
            consumption_meter_present = _gateway_meter_present("consumption")
            if (
                consumption_meter_present is True
                or consumption_meter_present is None
                or not inventory_ready
            ):
                _add_site_entity(
                    "gateway_consumption_meter",
                    EnphaseGatewayConsumptionMeterSensor(coord),
                )
            elif inventory_ready:
                _async_remove_site_sensor_entity("gateway_consumption_meter")
            _add_site_entity(
                "gateway_connectivity_status",
                EnphaseGatewayConnectivityStatusSensor(coord),
            )
            _add_site_entity(
                "gateway_last_reported",
                EnphaseGatewayLastReportedSensor(coord),
            )
            if site_has_battery:
                _add_site_entity("storm_alert", EnphaseStormAlertSensor(coord))
                _add_site_entity(
                    "system_profile_status", EnphaseSystemProfileStatusSensor(coord)
                )
        tariff_billing = getattr(coord, "tariff_billing", None)
        if (
            tariff_billing is not None
            or "tariff_billing_cycle" in known_site_entity_keys
            or _site_sensor_entity_registered("tariff_billing_cycle")
        ):
            _add_site_entity("tariff_billing_cycle", EnphaseTariffBillingSensor(coord))
        tariff_import_rate = getattr(coord, "tariff_import_rate", None)
        tariff_export_rate = getattr(coord, "tariff_export_rate", None)
        tariff_rates_refresh_seen = (
            getattr(coord, "tariff_rates_last_refresh_utc", None) is not None
        )
        current_import_rate_key = "tariff_current_import_rate"
        if tariff_import_rate is not None:
            _add_site_entity(
                current_import_rate_key,
                EnphaseCurrentTariffRateSensor(coord, is_import=True),
            )
        elif tariff_rates_refresh_seen:
            _async_remove_site_sensor_entity(current_import_rate_key)
        elif current_import_rate_key in known_site_entity_keys or (
            _site_sensor_entity_registered(current_import_rate_key)
        ):
            _add_site_entity(
                current_import_rate_key,
                EnphaseCurrentTariffRateSensor(coord, is_import=True),
            )
        _async_remove_site_sensor_entity("tariff_import_rate")
        _async_remove_site_sensor_entities_with_prefix(
            "tariff_import_rate_",
        )
        current_export_rate_key = "tariff_current_export_rate"
        if tariff_export_rate is not None:
            _add_site_entity(
                current_export_rate_key,
                EnphaseCurrentTariffRateSensor(coord, is_import=False),
            )
        elif tariff_rates_refresh_seen:
            _async_remove_site_sensor_entity(current_export_rate_key)
        elif current_export_rate_key in known_site_entity_keys or (
            _site_sensor_entity_registered(current_export_rate_key)
        ):
            _add_site_entity(
                current_export_rate_key,
                EnphaseCurrentTariffRateSensor(coord, is_import=False),
            )
        _async_remove_site_sensor_entity("tariff_export_rate")
        _async_remove_site_sensor_entities_with_prefix(
            "tariff_export_rate_",
        )

        for record in router_records:
            router_key = str(record.get("key", "")).strip()
            if not router_key:
                continue
            current_router_keys.add(router_key)
            entity_key = _gateway_iq_router_entity_key(router_key)
            if entity_key in known_site_entity_keys:
                continue
            try:
                index = int(record.get("index", 0))
            except Exception:  # noqa: BLE001
                index = 0
            if index <= 0:
                index = len(current_router_keys)
            site_entities.append(
                EnphaseGatewayIQEnergyRouterSensor(coord, router_key, index)
            )
            known_site_entity_keys.add(entity_key)
            known_gateway_iq_router_keys.add(router_key)

        if inventory_ready:
            stale_router_keys = known_gateway_iq_router_keys - current_router_keys
            for stale_router_key in list(stale_router_keys):
                _async_remove_site_sensor_entity(
                    _gateway_iq_router_entity_key(stale_router_key)
                )
            _async_prune_removed_gateway_iq_router_entities(current_router_keys)
        else:
            known_gateway_iq_router_keys.update(current_router_keys)
        for flow_key, (translation_key, name) in site_energy_specs.items():
            entity_key = f"site_energy_{flow_key}"
            if flow_key == "heat_pump":
                supported = (
                    heatpump_available
                    if inventory_ready
                    else (
                        heatpump_type_present
                        or bool(getattr(coord, "_heatpump_known_present", False))
                        or _site_energy_channel_present(flow_key, "heatpump")
                    )
                )
                if not supported:
                    _async_remove_site_sensor_entity(flow_key)
                    continue
            elif flow_key == "water_heater" and not _site_energy_channel_present(
                flow_key, "water_heater"
            ):
                _async_remove_site_sensor_entity(flow_key)
                continue
            _add_site_entity(
                entity_key,
                EnphaseSiteEnergySensor(coord, flow_key, translation_key, name),
            )
        if microinverter_available:
            _add_site_entity(
                "microinverter_connectivity_status",
                EnphaseMicroinverterConnectivityStatusSensor(coord),
            )
            _add_site_entity(
                "microinverter_reporting_count",
                EnphaseMicroinverterReportingCountSensor(coord),
            )
            _add_site_entity(
                "microinverter_last_reported",
                EnphaseMicroinverterLastReportedSensor(coord),
            )
        if heatpump_available:
            if heatpump_runtime_available:
                _add_site_entity(
                    "heat_pump_status",
                    EnphaseHeatPumpStatusSensor(coord),
                )
                _async_remove_site_sensor_entity("heat_pump_sg_ready_gateway")
                _add_site_entity(
                    "heat_pump_sg_ready_mode",
                    EnphaseHeatPumpSgReadyModeSensor(coord),
                )
                _add_site_entity(
                    "heat_pump_last_reported",
                    EnphaseHeatPumpLastReportedSensor(coord),
                )
            elif inventory_ready:
                for entity_key in (
                    "heat_pump_status",
                    "heat_pump_sg_ready_mode",
                    "heat_pump_last_reported",
                    "heat_pump_sg_ready_gateway",
                ):
                    _async_remove_site_sensor_entity(entity_key)
            _add_site_entity(
                "heat_pump_connectivity_status",
                EnphaseHeatPumpConnectivityStatusSensor(coord),
            )
            _add_site_entity(
                "heat_pump_energy_meter",
                EnphaseHeatPumpEnergyMeterSensor(coord),
            )
            _add_site_entity(
                "heat_pump_daily_energy",
                EnphaseHeatPumpDailyEnergySensor(coord),
            )
            _add_site_entity(
                "heat_pump_daily_grid_energy",
                EnphaseHeatPumpDailyGridEnergySensor(coord),
            )
            _add_site_entity(
                "heat_pump_daily_solar_energy",
                EnphaseHeatPumpDailySolarEnergySensor(coord),
            )
            _add_site_entity(
                "heat_pump_daily_battery_energy",
                EnphaseHeatPumpDailyBatteryEnergySensor(coord),
            )
            _add_site_entity(
                "heat_pump_power",
                EnphaseHeatPumpPowerSensor(coord),
            )
            _add_site_entity(
                "heat_pump_sg_ready_gateway",
                EnphaseHeatPumpSgReadyGatewaySensor(coord),
            )
        elif inventory_ready and not bool(
            getattr(coord, "_heatpump_known_present", False)
        ):
            for entity_key in heatpump_site_entity_keys:
                _async_remove_site_sensor_entity(entity_key)
        if _grid_control_site_applicable(coord) and (
            _type_available(coord, "enpower") or _type_available(coord, "envoy")
        ):
            _add_site_entity("grid_mode", EnphaseGridModeSensor(coord))
            _add_site_entity(
                "grid_control_status", EnphaseGridControlStatusSensor(coord)
            )
        battery_power_supported = _site_lifetime_power_channel_present(
            "battery_charge"
        ) and _site_lifetime_power_channel_present("battery_discharge")
        if site_has_battery and battery_device_available:
            if battery_power_supported:
                _add_site_entity("battery_power", EnphaseBatteryPowerSensor(coord))
            else:
                _async_remove_site_sensor_entity("battery_power")
            _add_site_entity("battery_mode", EnphaseBatteryModeSensor(coord))
            _add_site_entity(
                "battery_overall_charge", EnphaseBatteryOverallChargeSensor(coord)
            )
            _add_site_entity(
                "battery_overall_status", EnphaseBatteryOverallStatusSensor(coord)
            )
            _add_site_entity(
                "battery_cfg_schedule_status",
                EnphaseBatteryCfgScheduleStatusSensor(coord),
            )
            _add_site_entity(
                "battery_available_energy", EnphaseBatteryAvailableEnergySensor(coord)
            )
            _add_site_entity(
                "battery_available_power", EnphaseBatteryAvailablePowerSensor(coord)
            )
            _add_site_entity(
                "battery_last_reported",
                EnphaseBatteryLastReportedSensor(coord),
            )
            if _battery_schedule_inventory_supported(coord):
                _async_remove_site_sensor_entity("battery_schedule_summary")
                _add_site_entity(
                    "battery_cfg_schedules",
                    EnphaseBatteryScheduleModeSensor(coord, "cfg"),
                )
                _add_site_entity(
                    "battery_dtg_schedules",
                    EnphaseBatteryScheduleModeSensor(coord, "dtg"),
                )
                _add_site_entity(
                    "battery_rbd_schedules",
                    EnphaseBatteryScheduleModeSensor(coord, "rbd"),
                )
            elif inventory_ready:
                for entity_key in (
                    "battery_schedule_summary",
                    "battery_cfg_schedules",
                    "battery_dtg_schedules",
                    "battery_rbd_schedules",
                ):
                    _async_remove_site_sensor_entity(entity_key)
        else:
            _async_remove_site_sensor_entity("battery_power")
            for entity_key in (
                "battery_schedule_summary",
                "battery_cfg_schedules",
                "battery_dtg_schedules",
                "battery_rbd_schedules",
            ):
                _async_remove_site_sensor_entity(entity_key)
        if ac_battery_device_available:
            _add_site_entity(
                "ac_battery_overall_status",
                EnphaseAcBatteryOverallStatusSensor(coord),
            )
            _add_site_entity("ac_battery_power", EnphaseAcBatteryPowerSensor(coord))
            _add_site_entity(
                "ac_battery_last_reported",
                EnphaseAcBatteryLastReportedSensor(coord),
            )
        elif inventory_ready:
            for entity_key in (
                "ac_battery_overall_status",
                "ac_battery_power",
                "ac_battery_last_reported",
            ):
                _async_remove_site_sensor_entity(entity_key)
        if site_entities:
            async_add_entities(site_entities, update_before_add=False)

    @callback
    def _async_sync_type_inventory() -> None:
        _async_prune_dry_contact_type_inventory_entities()
        _async_prune_blocked_type_inventory_entities({"encharge"})
        for type_key in list(known_type_keys):
            if not _is_dry_contact_type_key(type_key):
                continue
            _async_remove_type_sensor_entity(type_key)
        keys = [
            key
            for key in coord.inventory_view.iter_type_keys()
            if key
            and key
            not in {
                "envoy",
                "encharge",
                "ac_battery",
                "iqevse",
                "microinverter",
                "heatpump",
            }
            and not _is_dry_contact_type_key(key)
            and key not in known_type_keys
        ]
        if not keys:
            return
        type_entities = [EnphaseTypeInventorySensor(coord, key) for key in keys]
        async_add_entities(type_entities, update_before_add=False)
        known_type_keys.update(keys)

    @callback
    def _async_sync_chargers() -> None:
        serials = [sn for sn in coord.iter_serials() if sn and sn not in known_serials]
        per_serial_entities = []
        site_has_battery = _site_has_battery(coord)
        for sn in serials:
            per_serial_entities.append(EnphaseEnergyTodaySensor(coord, sn))
            per_serial_entities.append(EnphaseConnectorStatusSensor(coord, sn))
            per_serial_entities.append(EnphaseElectricalPhaseSensor(coord, sn))
            per_serial_entities.append(EnphasePowerSensor(coord, sn))
            per_serial_entities.append(EnphaseChargingLevelSensor(coord, sn))
            per_serial_entities.append(EnphaseLastReportedSensor(coord, sn))
            per_serial_entities.append(EnphaseChargeModeSensor(coord, sn))
            per_serial_entities.append(EnphaseChargerAuthenticationSensor(coord, sn))
            per_serial_entities.append(EnphaseStatusSensor(coord, sn))
            per_serial_entities.append(EnphaseLifetimeEnergySensor(coord, sn))
            if site_has_battery:
                per_serial_entities.append(EnphaseStormGuardStateSensor(coord, sn))
                known_storm_guard_serials.add(sn)
            # The following sensors were removed due to unreliable values in most deployments:
            # Connector Reason, Schedule Type/Start/End, Session Miles, Session Plug timestamps
        if site_has_battery:
            storm_guard_serials = [
                sn
                for sn in coord.iter_serials()
                if sn and sn not in known_storm_guard_serials
            ]
            if storm_guard_serials:
                per_serial_entities.extend(
                    EnphaseStormGuardStateSensor(coord, sn)
                    for sn in storm_guard_serials
                )
                known_storm_guard_serials.update(storm_guard_serials)
        if per_serial_entities:
            async_add_entities(per_serial_entities, update_before_add=False)
        if serials:
            known_serials.update(serials)

    @callback
    def _async_sync_batteries() -> None:
        site_has_battery = _site_has_battery(coord)
        if not site_has_battery or not _type_available(coord, "encharge"):
            current_serials: list[str] = []
        else:
            iter_batteries = getattr(coord, "iter_battery_serials", None)
            current_serials = (
                [sn for sn in iter_batteries() if sn]
                if callable(iter_batteries)
                else []
            )
        current_set = set(current_serials)

        registry_setup.prune_battery_registry_once(current_set)
        registry_setup.remove_missing_battery_entities(current_set)

        serials = [
            sn
            for sn in current_serials
            if sn not in registry_setup.known_battery_serials
        ]
        if serials:
            entities: list[SensorEntity] = []
            for sn in serials:
                entities.extend(
                    [
                        EnphaseBatteryStorageChargeSensor(coord, sn),
                        EnphaseBatteryStorageStatusSensor(coord, sn),
                        EnphaseBatteryStorageHealthSensor(coord, sn),
                        EnphaseBatteryStorageCycleCountSensor(coord, sn),
                    ]
                )
            async_add_entities(entities, update_before_add=False)
            registry_setup.known_battery_serials.update(serials)

    @callback
    def _async_sync_ac_batteries() -> None:
        if not ac_battery_entities_available(coord):
            current_serials: list[str] = []
        else:
            iter_ac_batteries = getattr(coord, "iter_ac_battery_serials", None)
            current_serials = (
                [sn for sn in iter_ac_batteries() if sn]
                if callable(iter_ac_batteries)
                else []
            )
        current_set = set(current_serials)

        registry_setup.prune_ac_battery_registry_once(current_set)
        registry_setup.remove_missing_ac_battery_entities(current_set)

        serials = [
            sn
            for sn in current_serials
            if sn not in registry_setup.known_ac_battery_serials
        ]
        if serials:
            entities: list[SensorEntity] = []
            for sn in serials:
                entities.extend(
                    [
                        EnphaseAcBatteryStorageChargeSensor(coord, sn),
                        EnphaseAcBatteryStorageStatusSensor(coord, sn),
                        EnphaseAcBatteryStoragePowerSensor(coord, sn),
                        EnphaseAcBatteryStorageOperatingModeSensor(coord, sn),
                        EnphaseAcBatteryStorageCycleCountSensor(coord, sn),
                        EnphaseAcBatteryStorageLastReportedSensor(coord, sn),
                    ]
                )
            async_add_entities(entities, update_before_add=False)
            registry_setup.known_ac_battery_serials.update(serials)

    @callback
    def _async_sync_inverters() -> None:
        current_serials = [
            sn for sn in getattr(coord, "iter_inverter_serials", lambda: [])() if sn
        ]
        current_set = set(current_serials)

        registry_setup.prune_inverter_registry_once(current_set)
        registry_setup.remove_missing_inverter_entities(current_set)

        serials = [
            sn
            for sn in current_serials
            if sn not in registry_setup.known_inverter_serials
        ]
        if serials:
            entities = [
                EnphaseInverterLifetimeEnergySensor(coord, sn) for sn in serials
            ]
            async_add_entities(entities, update_before_add=False)
            registry_setup.known_inverter_serials.update(serials)

    @callback
    def _async_sync_topology() -> None:
        nonlocal last_type_key_set
        nonlocal last_battery_serial_set
        nonlocal last_ac_battery_serial_set
        nonlocal last_charger_serial_set
        nonlocal last_inverter_serial_set

        current_type_keys = {
            key for key in coord.inventory_view.iter_type_keys() if key
        }
        current_battery_serials = {
            sn for sn in getattr(coord, "iter_battery_serials", lambda: [])() if sn
        }
        current_ac_battery_serials = {
            sn for sn in getattr(coord, "iter_ac_battery_serials", lambda: [])() if sn
        }
        current_charger_serials = {sn for sn in coord.iter_serials() if sn}
        current_inverter_serials = {
            sn for sn in getattr(coord, "iter_inverter_serials", lambda: [])() if sn
        }

        _async_sync_site_entities()
        _async_sync_type_inventory()
        last_type_key_set = current_type_keys
        if current_battery_serials != last_battery_serial_set:
            _async_sync_batteries()
            _async_sync_chargers()
            last_battery_serial_set = current_battery_serials
        if current_ac_battery_serials != last_ac_battery_serial_set:
            _async_sync_ac_batteries()
            last_ac_battery_serial_set = current_ac_battery_serials
        if current_charger_serials != last_charger_serial_set:
            _async_sync_chargers()
            last_charger_serial_set = current_charger_serials
        if current_inverter_serials != last_inverter_serial_set:
            _async_sync_inverters()
            last_inverter_serial_set = current_inverter_serials

    add_topology_listener = getattr(coord, "async_add_topology_listener", None)
    has_topology_listener = callable(add_topology_listener)
    if not has_topology_listener:
        add_topology_listener = getattr(coord, "async_add_listener", None)
    if callable(add_topology_listener):
        entry.async_on_unload(add_topology_listener(_async_sync_topology))
    add_coordinator_listener = getattr(coord, "async_add_listener", None)
    if has_topology_listener and callable(add_coordinator_listener):
        entry.async_on_unload(add_coordinator_listener(_async_sync_topology))
    registry_setup.prune_historical_charger_sensor_entities()
    registry_setup.prune_removed_site_entities()
    _async_sync_topology()


class _BaseEVSensor(EnphaseBaseEntity, SensorEntity):
    def __init__(self, coord: EnphaseCoordinator, sn: str, name_suffix: str, key: str):
        super().__init__(coord, sn)
        self._key = key
        self._attr_name = name_suffix
        self._attr_unique_id = f"{DOMAIN}_{sn}_{key}"

    @property
    def native_value(self):
        return self.data.get(self._key)


class EnphaseElectricalPhaseSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "electrical_phase"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_electrical_phase"

    @staticmethod
    def _friendly_phase_mode(raw) -> tuple[str | None, object | None]:
        if raw is None:
            return None, None
        try:
            normalized = str(raw).strip()
        except Exception:  # noqa: BLE001
            return None, raw
        if not normalized:
            return None, None
        friendly: str | None = None
        try:
            n = int(normalized)
        except Exception:  # noqa: BLE001
            n = None
        if n == 1:
            friendly = "Single Phase"
        elif n == 3:
            friendly = "Three Phase"
        if friendly is None:
            friendly = normalized
        raw_out: object | None = normalized if isinstance(raw, str) else raw
        return friendly, raw_out

    @staticmethod
    def _as_bool(value) -> bool | None:
        if value is None:
            return None
        try:
            return bool(value)
        except Exception:  # noqa: BLE001
            return None

    @property
    def native_value(self):
        friendly, _ = self._friendly_phase_mode(self.data.get("phase_mode"))
        return friendly

    @property
    def extra_state_attributes(self):
        _, phase_raw = self._friendly_phase_mode(self.data.get("phase_mode"))
        return {
            "phase_mode_raw": phase_raw,
            PHASE_SWITCH_CONFIG_SETTING: self.data.get(PHASE_SWITCH_CONFIG_SETTING),
            "dlb_enabled": self._as_bool(self.data.get("dlb_enabled")),
            "dlb_active": self._as_bool(self.data.get("dlb_active")),
        }


@dataclass
class _LastSessionRestoreData(ExtraStoredData):
    """Persist last session metrics across restarts."""

    last_session_kwh: float | None
    last_session_wh: float | None
    last_session_start: float | None
    last_session_end: float | None
    session_key: str | None
    last_duration_min: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "last_session_kwh": self.last_session_kwh,
            "last_session_wh": self.last_session_wh,
            "last_session_start": self.last_session_start,
            "last_session_end": self.last_session_end,
            "session_key": self.session_key,
            "last_duration_min": self.last_duration_min,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "_LastSessionRestoreData":
        if not isinstance(data, dict):
            return cls(None, None, None, None, None, None)

        def _as_float(val):
            try:
                return float(val) if val is not None else None
            except Exception:  # noqa: BLE001
                return None

        def _as_int(val):
            try:
                return int(val) if val is not None else None
            except Exception:  # noqa: BLE001
                return None

        session_key = data.get("session_key")
        return cls(
            _as_float(data.get("last_session_kwh")),
            _as_float(data.get("last_session_wh")),
            _as_float(data.get("last_session_start")),
            _as_float(data.get("last_session_end")),
            str(session_key) if session_key is not None else None,
            _as_int(data.get("last_duration_min")),
        )


@dataclass
class _SiteLifetimePowerRestoreData(ExtraStoredData):
    """Persist the last two live lifetime-energy samples across restarts."""

    previous_live_flow_kwh: dict[str, float]
    previous_live_energy_ts: float | None
    previous_live_sample_ts: float | None
    last_live_interval_minutes: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "previous_live_flow_kwh": dict(self.previous_live_flow_kwh),
            "previous_live_energy_ts": self.previous_live_energy_ts,
            "previous_live_sample_ts": self.previous_live_sample_ts,
            "last_live_interval_minutes": self.last_live_interval_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "_SiteLifetimePowerRestoreData":
        if not isinstance(data, dict):
            return cls({}, None, None, None)

        previous_live_flow_kwh: dict[str, float] = {}
        raw_previous_live_flow_kwh = data.get("previous_live_flow_kwh")
        if isinstance(raw_previous_live_flow_kwh, dict):
            for flow_key, raw_value in raw_previous_live_flow_kwh.items():
                if not isinstance(flow_key, str):
                    continue
                try:
                    numeric = float(raw_value)
                except Exception:
                    continue
                if numeric < 0:
                    continue
                previous_live_flow_kwh[flow_key] = numeric

        def _as_float(value: object) -> float | None:
            try:
                return float(value) if value is not None else None
            except Exception:
                return None

        return cls(
            previous_live_flow_kwh=previous_live_flow_kwh,
            previous_live_energy_ts=_as_float(data.get("previous_live_energy_ts")),
            previous_live_sample_ts=_as_float(data.get("previous_live_sample_ts")),
            last_live_interval_minutes=_as_float(
                data.get("last_live_interval_minutes")
            ),
        )


class EnphaseEnergyTodaySensor(EnphaseBaseEntity, SensorEntity, RestoreEntity):
    """Expose the last charging session's energy as a sensor."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_translation_key = "last_session"
    _HISTORY_ATTR_KEYS = (
        "session_cost",
        "avg_cost_per_kwh",
        "cost_calculated",
        "session_cost_state",
        "manual_override",
        "charge_profile_stack_level",
        "start",
        "end",
        "active_charge_time_s",
        "session_miles",
        "session_charge_level",
        "session_auth_status",
        "session_auth_type",
        "session_auth_token_present",
    )

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        # Preserve unique_id for continuity even though the semantics changed
        self._attr_unique_id = f"{DOMAIN}_{sn}_energy_today"
        self._last_session_kwh: float | None = None
        self._last_session_wh: float | None = None
        self._last_session_start: float | None = None
        self._last_session_end: float | None = None
        self._last_duration_min: int | None = None
        self._session_key: str | None = None
        self._last_context: dict | None = None
        self._last_context_source: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        last_extra = await self.async_get_last_extra_data()
        extra_data = _LastSessionRestoreData.from_dict(
            last_extra.as_dict() if last_extra is not None else None
        )
        self._last_session_kwh = extra_data.last_session_kwh
        self._last_session_wh = extra_data.last_session_wh
        self._last_session_start = extra_data.last_session_start
        self._last_session_end = extra_data.last_session_end
        self._session_key = extra_data.session_key
        self._last_duration_min = extra_data.last_duration_min
        if last_state:
            try:
                restored_val = float(last_state.state)
            except Exception:
                restored_val = None
            if restored_val is not None and restored_val >= 0:
                self._last_session_kwh = restored_val
            attrs = last_state.attributes or {}
            if self._session_key is None and attrs.get("session_key") is not None:
                try:
                    self._session_key = str(attrs["session_key"])
                except Exception:
                    self._session_key = None
            if self._last_duration_min is None and attrs.get("session_duration_min"):
                try:
                    self._last_duration_min = int(attrs.get("session_duration_min"))
                except Exception:
                    self._last_duration_min = None

    @staticmethod
    def _coerce_timestamp(value) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return None
            cleaned = cleaned.replace("[UTC]", "").replace("Z", "+00:00")
            try:
                dt_val = datetime.fromisoformat(cleaned)
            except Exception:
                return None
            if dt_val.tzinfo is None:
                dt_val = dt_val.replace(tzinfo=timezone.utc)
            return dt_val.timestamp()
        return None

    @staticmethod
    def _coerce_energy(session_kwh, session_wh) -> tuple[float | None, float | None]:
        energy_kwh: float | None = None
        energy_wh: float | None = None
        if session_kwh is not None:
            try:
                energy_kwh = round(float(session_kwh), 2)
            except Exception:  # noqa: BLE001
                energy_kwh = None
        if session_wh is not None:
            wh_kwh, wh_value, _unit = normalize_evse_session_energy(
                session_wh,
                wh_hint=True,
            )
            if energy_kwh is None:
                energy_kwh = wh_kwh
            energy_wh = wh_value
        if energy_kwh is not None and energy_wh is None:
            try:
                energy_wh = round(energy_kwh * 1000.0, 3)
            except Exception:  # noqa: BLE001
                energy_wh = None
        return energy_kwh, energy_wh

    def _extract_realtime_session(self, data: dict) -> dict:
        charging = bool(data.get("charging"))
        energy_kwh, energy_wh = self._coerce_energy(
            data.get("session_kwh"), data.get("session_energy_wh")
        )
        start = self._coerce_timestamp(data.get("session_start"))
        end = self._coerce_timestamp(data.get("session_end"))
        session_key = None
        if start is not None or end is not None:
            session_key = f"{start or 'none'}:{end or 'none'}"
        elif charging:
            session_key = "charging"

        return {
            "energy_kwh": energy_kwh,
            "energy_wh": energy_wh,
            "start": start,
            "end": end,
            "charging": charging,
            "plug_in_at": data.get("session_plug_in_at"),
            "plug_out_at": data.get("session_plug_out_at"),
            "session_charge_level": data.get("session_charge_level"),
            "session_cost": data.get("session_cost"),
            "session_miles": data.get("session_miles"),
            "session_key": session_key,
            "session_id": None,
            "active_charge_time_s": None,
            "avg_cost_per_kwh": None,
            "cost_calculated": None,
            "session_cost_state": None,
            "manual_override": None,
            "charge_profile_stack_level": None,
            "session_auth_status": data.get("session_auth_status"),
            "session_auth_type": data.get("session_auth_type"),
            "session_auth_identifier": data.get("session_auth_identifier"),
            "session_auth_token_present": data.get("session_auth_token_present"),
        }

    def _extract_history_session(self, data: dict) -> dict | None:
        sessions = data.get("energy_today_sessions") or []
        if not sessions:
            return None
        latest = sessions[-1]
        energy_kwh, energy_wh = self._coerce_energy(
            (
                latest.get("energy_kwh_total")
                if latest.get("energy_kwh_total") is not None
                else latest.get("energy_kwh")
            ),
            None,
        )
        start = self._coerce_timestamp(latest.get("start"))
        end = self._coerce_timestamp(latest.get("end"))
        session_id_raw = (
            latest.get("session_id")
            if latest.get("session_id") is not None
            else (
                latest.get("sessionId")
                if latest.get("sessionId") is not None
                else latest.get("id")
            )
        )
        session_key = None
        session_id = None
        if session_id_raw is not None:
            try:
                session_id = str(session_id_raw)
            except Exception:  # noqa: BLE001
                session_id = None
        if session_id is not None:
            session_key = session_id
        elif start is not None or end is not None:
            session_key = f"{start or 'none'}:{end or 'none'}"

        return {
            "energy_kwh": energy_kwh,
            "energy_wh": energy_wh,
            "start": start,
            "end": end,
            "charging": False,
            "plug_in_at": latest.get("start"),
            "plug_out_at": latest.get("end"),
            "session_charge_level": latest.get("session_charge_level"),
            "session_cost": latest.get("session_cost"),
            "session_miles": (
                latest.get("miles_added")
                if latest.get("miles_added") is not None
                else latest.get("range_added")
            ),
            "session_key": session_key,
            "session_id": session_id,
            "active_charge_time_s": latest.get("active_charge_time_s"),
            "avg_cost_per_kwh": latest.get("avg_cost_per_kwh"),
            "cost_calculated": latest.get("cost_calculated"),
            "session_cost_state": latest.get("session_cost_state"),
            "manual_override": latest.get("manual_override"),
            "charge_profile_stack_level": latest.get("charge_profile_stack_level"),
            "session_auth_status": latest.get("auth_status"),
            "session_auth_type": latest.get("auth_type"),
            "session_auth_identifier": latest.get("auth_identifier"),
            "session_auth_token_present": (
                bool(latest.get("auth_token")) if latest.get("auth_token") else False
            ),
        }

    @staticmethod
    def _compute_duration_minutes(
        start: float | None, end: float | None, charging: bool
    ) -> int | None:
        if start is None:
            return None
        if end is None and charging:
            end_ts = dt_util.utcnow().timestamp()
        elif end is None:
            return None
        else:
            end_ts = end
        try:
            duration = int((end_ts - start) / 60)
        except Exception:  # noqa: BLE001
            return None
        return max(0, duration)

    def _pick_session_context(self, data: dict) -> dict | None:
        realtime = self._extract_realtime_session(data)
        history = self._extract_history_session(data)

        has_realtime_energy = realtime and realtime.get("energy_kwh") is not None
        realtime_nonzero = bool(
            has_realtime_energy and (realtime.get("energy_kwh") or 0) > 0
        )
        realtime_idle_zero = bool(
            realtime
            and not realtime.get("charging")
            and (realtime.get("energy_kwh") or 0) == 0
        )
        if realtime and realtime["charging"]:
            self._last_context_source = "realtime"
            return realtime
        if history and history.get("energy_kwh") is not None:
            # Session history is richer than the live status payload once the
            # charger is idle, especially for authorization and final energy
            # metadata that can arrive after charging stops.
            self._last_context_source = "history"
            return history
        if realtime and realtime_nonzero:
            self._last_context_source = "realtime"
            return realtime
        if has_realtime_energy and not realtime_idle_zero:
            self._last_context_source = "realtime"
            return realtime
        if realtime_idle_zero:
            if history:
                self._last_context_source = "history"
                return history
            self._last_context_source = None
            return None
        if history:
            self._last_context_source = "history"
            return history
        self._last_context_source = None
        return None

    def _merge_history_context(self, context: dict | None) -> dict:
        merged = dict(context or {})
        history = self._extract_history_session(self.data)
        if not history:
            return merged

        def _as_float(value):
            if value is None or isinstance(value, bool):
                return None
            try:
                return float(value)
            except Exception:  # noqa: BLE001
                return None

        should_merge = self._last_context_source == "history"
        if not should_merge:
            context_key = merged.get("session_key")
            history_key = history.get("session_key")
            should_merge = (
                context_key is not None
                and history_key is not None
                and context_key == history_key
            )
        if not should_merge:
            ctx_start = _as_float(merged.get("start"))
            ctx_end = _as_float(merged.get("end"))
            hist_start = _as_float(history.get("start"))
            hist_end = _as_float(history.get("end"))
            if ctx_start is not None and hist_start is not None:
                if abs(ctx_start - hist_start) <= 1.0:
                    if ctx_end is None or hist_end is None:
                        should_merge = True
                    elif abs(ctx_end - hist_end) <= 1.0:
                        should_merge = True
            elif ctx_end is not None and hist_end is not None:
                if abs(ctx_end - hist_end) <= 1.0:
                    should_merge = True
        if should_merge:
            for key in self._HISTORY_ATTR_KEYS:
                value = history.get(key)
                if value is not None:
                    merged[key] = value
        return merged

    @property
    def native_value(self):
        context = self._pick_session_context(self.data) or {}
        self._last_context = context

        energy_kwh = context.get("energy_kwh")
        energy_wh = context.get("energy_wh")
        start = context.get("start")
        end = context.get("end")
        charging = bool(context.get("charging"))
        session_key = context.get("session_key")
        duration_min = self._compute_duration_minutes(start, end, charging)

        if energy_kwh is not None:
            try:
                energy_kwh = max(0.0, round(float(energy_kwh), 2))
            except Exception:  # noqa: BLE001
                energy_kwh = None
        if energy_wh is not None:
            try:
                energy_wh = max(0.0, round(float(energy_wh), 3))
            except Exception:  # noqa: BLE001
                energy_wh = None
        if energy_kwh is not None and energy_wh is None:
            try:
                energy_wh = round(energy_kwh * 1000.0, 3)
            except Exception:  # noqa: BLE001
                energy_wh = None

        if session_key and session_key != self._session_key:
            self._session_key = session_key
            if energy_kwh is not None:
                self._last_session_kwh = energy_kwh
            if energy_wh is not None or energy_kwh is not None:
                self._last_session_wh = energy_wh or (
                    round(energy_kwh * 1000.0, 3) if energy_kwh is not None else None
                )
            self._last_duration_min = duration_min
            self._last_session_start = start
            self._last_session_end = end
        else:
            if energy_kwh is not None:
                self._last_session_kwh = energy_kwh
            if energy_wh is not None:
                self._last_session_wh = energy_wh
            elif energy_kwh is not None:
                try:
                    self._last_session_wh = round(energy_kwh * 1000.0, 3)
                except Exception:  # noqa: BLE001
                    pass
            if duration_min is not None:
                self._last_duration_min = duration_min
            if start is not None:
                self._last_session_start = start
            if end is not None:
                self._last_session_end = end

        return self._last_session_kwh

    @property
    def extra_state_attributes(self):
        merged_context = self._merge_history_context(self._last_context)
        return self._session_metadata_attributes(
            self.data,
            hass=self.hass,  # type: ignore[arg-type]
            context=merged_context,
            energy_kwh=self._last_session_kwh,
            energy_wh=self._last_session_wh,
            duration_min=self._last_duration_min,
            session_key=self._session_key,
        )

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        return _LastSessionRestoreData(
            last_session_kwh=self._last_session_kwh,
            last_session_wh=self._last_session_wh,
            last_session_start=self._last_session_start,
            last_session_end=self._last_session_end,
            session_key=self._session_key,
            last_duration_min=self._last_duration_min,
        )

    @staticmethod
    def _session_metadata_attributes(
        data: dict,
        hass=None,
        *,
        context: dict | None = None,
        energy_kwh: float | None = None,
        energy_wh: float | None = None,
        duration_min: int | None = None,
        session_key: str | None = None,
    ) -> dict[str, object]:
        """Derive session metadata attributes from the coordinator payload."""
        result: dict[str, object] = {}

        def _localize(value):
            if value in (None, ""):
                return None
            try:
                if isinstance(value, (int, float)):
                    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
                elif isinstance(value, str):
                    cleaned = value.strip()
                    if not cleaned:
                        return None
                    if cleaned.endswith("[UTC]"):
                        cleaned = cleaned[:-5]
                    if cleaned.endswith("Z"):
                        cleaned = cleaned[:-1] + "+00:00"
                    dt = datetime.fromisoformat(cleaned)
                else:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt_util.as_local(dt).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                return None

        def _as_bool(value):
            if value is None:
                return None
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "y")
            return None

        def _as_int(value):
            if value is None:
                return None
            try:
                return int(float(value))
            except Exception:  # noqa: BLE001
                return None

        def _as_float(value, *, precision: int | None = None):
            if value is None:
                return None
            try:
                out = float(value)
            except Exception:  # noqa: BLE001
                return None
            if precision is not None:
                try:
                    return round(out, precision)
                except Exception:  # noqa: BLE001
                    return out
            return out

        session_data = context or {}
        plug_in = _localize(
            session_data.get("plug_in_at") or data.get("session_plug_in_at")
        )
        plug_out = _localize(
            session_data.get("plug_out_at") or data.get("session_plug_out_at")
        )
        result["plugged_in_at"] = plug_in
        result["plugged_out_at"] = plug_out

        energy_kwh_val = energy_kwh
        energy_wh_val = energy_wh
        if energy_kwh_val is None or energy_wh_val is None:
            kwh_raw = session_data.get("energy_kwh")
            wh_raw = session_data.get("energy_wh")
            if energy_kwh_val is None and kwh_raw is not None:
                try:
                    energy_kwh_val = round(float(kwh_raw), 2)
                except Exception:  # noqa: BLE001
                    energy_kwh_val = None
            if energy_wh_val is None and wh_raw is not None:
                try:
                    energy_wh_val = round(float(wh_raw), 3)
                except Exception:  # noqa: BLE001
                    energy_wh_val = None
        if energy_kwh_val is None:
            session_kwh = data.get("session_kwh")
            if session_kwh is not None:
                try:
                    energy_kwh_val = round(float(session_kwh), 2)
                except Exception:  # noqa: BLE001
                    energy_kwh_val = None
        if energy_wh_val is None:
            energy_wh_raw = data.get("session_energy_wh")
            if energy_wh_raw is not None:
                try:
                    energy_wh_val = round(float(energy_wh_raw), 3)
                except Exception:  # noqa: BLE001
                    energy_wh_val = None
        if energy_kwh_val is not None and energy_wh_val is None:
            try:
                energy_wh_val = round(energy_kwh_val * 1000.0, 3)
            except Exception:  # noqa: BLE001
                energy_wh_val = None

        result["energy_consumed_wh"] = energy_wh_val
        result["energy_consumed_kwh"] = energy_kwh_val

        session_cost = session_data.get("session_cost", data.get("session_cost"))
        if session_cost is not None:
            try:
                result["session_cost"] = round(float(session_cost), 3)
            except Exception:  # noqa: BLE001
                result["session_cost"] = session_cost
        else:
            result["session_cost"] = None

        session_charge_level = session_data.get(
            "session_charge_level", data.get("session_charge_level")
        )
        if session_charge_level is not None:
            try:
                result["session_charge_level"] = int(session_charge_level)
            except Exception:  # noqa: BLE001
                result["session_charge_level"] = session_charge_level
        else:
            result["session_charge_level"] = None

        range_value = session_data.get("session_miles", data.get("session_miles"))
        preferred_unit = UnitOfLength.MILES
        try:
            if hass is not None and hasattr(hass, "config"):
                units = getattr(hass.config, "units", None)
                if units is not None and hasattr(units, "length_unit"):
                    preferred_unit = units.length_unit  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            preferred_unit = UnitOfLength.MILES
        converted_range = None
        try:
            if range_value is not None:
                range_float = float(range_value)
                target_unit = preferred_unit
                if target_unit and target_unit != UnitOfLength.MILES:
                    converted_range = DistanceConverter.convert(
                        range_float, UnitOfLength.MILES, target_unit
                    )
                else:
                    converted_range = range_float
        except Exception:  # noqa: BLE001
            converted_range = None

        result["range_added"] = (
            round(converted_range, 3) if converted_range is not None else None
        )
        result["session_duration_min"] = duration_min

        start_at = _localize(session_data.get("start") or data.get("session_start"))
        end_at = _localize(session_data.get("end") or data.get("session_end"))
        result["session_started_at"] = start_at
        result["session_ended_at"] = end_at

        result["active_charge_time_s"] = _as_int(
            session_data.get("active_charge_time_s")
        )
        result["avg_cost_per_kwh"] = _as_float(
            session_data.get("avg_cost_per_kwh"), precision=3
        )
        result["cost_calculated"] = _as_bool(session_data.get("cost_calculated"))
        result["session_cost_state"] = session_data.get("session_cost_state")
        result["manual_override"] = _as_bool(session_data.get("manual_override"))
        result["charge_profile_stack_level"] = _as_int(
            session_data.get("charge_profile_stack_level")
        )
        auth_status_raw = session_data.get("session_auth_status")
        if auth_status_raw is None:
            auth_status_raw = data.get("session_auth_status")
        result["session_auth_status"] = _as_int(auth_status_raw)
        result["session_auth_type"] = (
            session_data.get("session_auth_type")
            if session_data.get("session_auth_type") is not None
            else data.get("session_auth_type")
        )
        auth_token_flag = session_data.get(
            "session_auth_token_present", data.get("session_auth_token_present")
        )
        result["session_auth_token_present"] = _as_bool(auth_token_flag)

        return result


class EnphaseConnectorStatusSensor(_BaseEVSensor):
    _attr_translation_key = "connector_status"

    def __init__(self, coord, sn):
        super().__init__(coord, sn, "Connector Status", "connector_status")

    @property
    def icon(self) -> str | None:
        v = str(self.data.get("connector_status") or "").upper()
        # Map common connector status values to clearer icons
        mapping = {
            "AVAILABLE": "mdi:ev-station",
            "CHARGING": "mdi:ev-plug-ccs2",
            "PLUGGED": "mdi:ev-plug-type2",
            "CONNECTED": "mdi:ev-plug-type2",
            "DISCONNECTED": "mdi:power-plug-off",
            "UNPLUGGED": "mdi:power-plug-off",
            "FAULTED": "mdi:alert",
            "ERROR": "mdi:alert",
            "OCCUPIED": "mdi:car-electric",
        }
        return mapping.get(v, "mdi:ev-station")

    @property
    def extra_state_attributes(self):
        def _clean(val):
            if val in (None, ""):
                return None
            if isinstance(val, str):
                cleaned = val.strip()
                return cleaned or None
            try:
                text = str(val)
            except Exception:  # noqa: BLE001
                return val
            return text.strip() or None

        return {
            "status_reason": _clean(self.data.get("connector_reason")),
            "connector_status_info": _clean(self.data.get("connector_status_info")),
        }


class EnphasePowerSensor(EnphaseBaseEntity, SensorEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_translation_key = "power"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.POWER

    _DEFAULT_WINDOW_S = 300  # 5 minutes
    _MIN_DELTA_KWH = 0.0005  # 0.5 Wh jitter guard
    _RESET_DROP_KWH = 0.25  # minimum backward delta treated as a meter reset
    _STATIC_MAX_WATTS = 19200  # IQ EV Charger 2 max continuous throughput (~80A @ 240V)

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_power"
        self._last_lifetime_kwh: float | None = None
        self._last_energy_ts: float | None = None
        self._last_sample_ts: float | None = None
        self._last_power_w: int = 0
        self._last_window_s: float | None = None
        self._last_method: str = "seeded"
        self._max_throughput_w: int = self._STATIC_MAX_WATTS
        self._max_throughput_unbounded_w: int = self._STATIC_MAX_WATTS
        self._max_throughput_source: str = "static_default"
        self._max_throughput_amps: float | None = None
        nominal = getattr(self._coord, "nominal_voltage", DEFAULT_NOMINAL_VOLTAGE)
        self._max_throughput_voltage: float = float(nominal)
        self._max_throughput_topology: str = "unknown"
        self._max_throughput_phase_multiplier: float = 1.0
        self._last_reset_at: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if not last_state:
            return
        attrs = last_state.attributes or {}
        self._last_lifetime_kwh = _restore_optional_float_attribute(
            attrs, "last_lifetime_kwh"
        )
        self._last_energy_ts = _restore_optional_float_attribute(
            attrs, "last_energy_ts"
        )
        self._last_sample_ts = _restore_optional_float_attribute(
            attrs, "last_sample_ts"
        )
        restored_power = _restore_optional_int_value(last_state.state)
        if restored_power is None:
            restored_power = _restore_optional_int_value(attrs.get("last_power_w"))
        self._last_power_w = restored_power if restored_power is not None else 0
        self._last_window_s = _restore_optional_float_attribute(
            attrs, "last_window_seconds"
        )
        if attrs.get("method"):
            self._last_method = str(attrs.get("method"))
        self._last_reset_at = _restore_optional_float_attribute(attrs, "last_reset_at")

        # Legacy restore support (pre-0.7.9 attributes)
        if self._last_lifetime_kwh is None:
            legacy_baseline = attrs.get("baseline_kwh")
            legacy_today = attrs.get("last_energy_today_kwh")
            try:
                if legacy_baseline is not None:
                    legacy_baseline = float(legacy_baseline)
                if legacy_today is not None:
                    legacy_today = float(legacy_today)
            except Exception:
                legacy_baseline = None
                legacy_today = None
            if legacy_baseline is not None and legacy_today is not None:
                self._last_lifetime_kwh = legacy_baseline + legacy_today
                try:
                    if (
                        attrs.get("last_ts") is not None
                        and self._last_energy_ts is None
                    ):
                        self._last_energy_ts = float(attrs.get("last_ts"))
                except Exception:
                    self._last_energy_ts = None
                # Preserve previously reported power when available
                if attrs.get("method") is None:
                    self._last_method = "legacy_restore"

    @staticmethod
    def _parse_timestamp(raw: float | str | None) -> float | None:
        """Normalize Enlighten timestamps to epoch seconds."""
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            val = float(raw)
            if val > 10**12:
                val = val / 1000.0
            return val if val > 0 else None
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return None
            s = s.replace("[UTC]", "").replace("Z", "+00:00")
            try:
                dt_obj = datetime.fromisoformat(s)
            except ValueError:
                return None
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
            return dt_obj.timestamp()
        return None

    @staticmethod
    def _as_float(val) -> float | None:
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(val) -> int | None:
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _power_topology(cls, data: dict) -> str:
        phase_mode = data.get("phase_mode")
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
        phase_count = cls._as_int(data.get("phase_count"))
        if phase_count is not None:
            if phase_count >= 3:
                return "three_phase"
            if phase_count == 1:
                return "single_phase"
        return "unknown"

    @classmethod
    def _three_phase_multiplier(cls, data: dict) -> float:
        wiring = data.get("wiring_configuration")
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
        return 3.0 if explicit_neutral else math.sqrt(3)

    @staticmethod
    def _is_actually_charging(data: dict) -> bool:
        if "actual_charging" in data:
            return bool(data.get("actual_charging"))
        return evse_power_is_actively_charging(
            data.get("connector_status"),
            data.get("charging"),
            suspended_by_evse=data.get("suspended_by_evse"),
        )

    def _resolve_max_throughput(
        self, data: dict
    ) -> tuple[int, str, float | None, float, int, str, float]:
        voltage = self._as_float(data.get("operating_v"))
        if voltage is None or voltage <= 0:
            voltage = self._as_float(data.get("nominal_v"))
        if voltage is None or voltage <= 0:
            voltage = float(
                getattr(self._coord, "nominal_voltage", DEFAULT_NOMINAL_VOLTAGE)
            )
        topology = self._power_topology(data)
        phase_multiplier = 1.0
        candidates = (
            ("session_charge_level", data.get("session_charge_level")),
            ("charging_level", data.get("charging_level")),
            ("max_amp", data.get("max_amp")),
            ("max_current", data.get("max_current")),
        )
        for source, raw in candidates:
            amps = self._as_float(raw)
            if amps is None or amps <= 0:
                continue
            if topology == "three_phase":
                # Default to the conservative line-to-line formula unless the
                # payload explicitly suggests line-to-neutral wiring.
                phase_multiplier = self._three_phase_multiplier(data)
            unbounded = int(round(voltage * amps * phase_multiplier))
            if unbounded <= 0:
                continue
            bounded = min(unbounded, self._STATIC_MAX_WATTS)
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
            self._STATIC_MAX_WATTS,
            "static_default",
            None,
            voltage,
            self._STATIC_MAX_WATTS,
            topology,
            phase_multiplier,
        )

    def _apply_derived_snapshot(self, data: dict) -> bool:
        if "derived_power_w" not in data:
            return False
        self._last_lifetime_kwh = self._as_float(data.get("derived_last_lifetime_kwh"))
        self._last_energy_ts = self._parse_timestamp(data.get("derived_last_energy_ts"))
        self._last_sample_ts = self._parse_timestamp(data.get("derived_last_sample_ts"))
        derived_power = self._as_int(data.get("derived_power_w"))
        self._last_power_w = derived_power if derived_power is not None else 0
        self._last_window_s = self._as_float(data.get("derived_power_window_seconds"))
        method = data.get("derived_power_method")
        self._last_method = str(method) if method is not None else "seeded"
        self._last_reset_at = self._parse_timestamp(data.get("derived_last_reset_at"))
        self._max_throughput_w = (
            self._as_int(data.get("derived_power_max_throughput_w"))
            or self._STATIC_MAX_WATTS
        )
        self._max_throughput_unbounded_w = (
            self._as_int(data.get("derived_power_max_throughput_unbounded_w"))
            or self._STATIC_MAX_WATTS
        )
        source = data.get("derived_power_max_throughput_source")
        self._max_throughput_source = (
            str(source) if source is not None else "static_default"
        )
        self._max_throughput_amps = self._as_float(
            data.get("derived_power_max_throughput_amps")
        )
        max_voltage = self._as_float(data.get("derived_power_max_throughput_voltage"))
        if max_voltage is None or max_voltage <= 0:
            max_voltage = float(
                getattr(self._coord, "nominal_voltage", DEFAULT_NOMINAL_VOLTAGE)
            )
        self._max_throughput_voltage = max_voltage
        topology = data.get("derived_power_max_throughput_topology")
        self._max_throughput_topology = (
            str(topology) if topology is not None else "unknown"
        )
        phase_multiplier = self._as_float(
            data.get("derived_power_max_throughput_phase_multiplier")
        )
        self._max_throughput_phase_multiplier = (
            phase_multiplier if phase_multiplier is not None else 1.0
        )
        return True

    @property
    def native_value(self):
        data = self.data
        if self._apply_derived_snapshot(data):
            return self._last_power_w
        is_charging = self._is_actually_charging(data)
        (
            max_watts,
            max_source,
            max_amps,
            max_voltage,
            max_unbounded,
            max_topology,
            max_phase_multiplier,
        ) = self._resolve_max_throughput(data)
        self._max_throughput_w = max_watts
        self._max_throughput_unbounded_w = max_unbounded
        self._max_throughput_source = max_source
        self._max_throughput_amps = max_amps
        self._max_throughput_voltage = max_voltage
        self._max_throughput_topology = max_topology
        self._max_throughput_phase_multiplier = max_phase_multiplier
        lifetime = self._as_float(data.get("lifetime_kwh"))
        sample_ts = self._parse_timestamp(data.get("sampled_at_ts"))
        if sample_ts is None:
            sample_ts = self._parse_timestamp(data.get("sampled_at_utc"))
        if sample_ts is None:
            sample_ts = self._parse_timestamp(data.get("last_reported_at"))
        if sample_ts is None:
            now_dt = getattr(self._coord, "last_success_utc", None) or dt_util.now()
            if now_dt.tzinfo is None:
                now_dt = now_dt.replace(tzinfo=timezone.utc)
            sample_ts = now_dt.astimezone(timezone.utc).timestamp()
        self._last_sample_ts = sample_ts

        if lifetime is None:
            if not is_charging:
                self._last_power_w = 0
                self._last_method = "idle"
                self._last_window_s = None
            return self._last_power_w

        if self._last_lifetime_kwh is None:
            self._last_lifetime_kwh = lifetime
            self._last_energy_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "seeded"
            self._last_window_s = None
            return 0

        delta_kwh, reset_detected = _lifetime_energy_delta(
            current_kwh=lifetime,
            previous_kwh=self._last_lifetime_kwh,
            reset_drop_kwh=self._RESET_DROP_KWH,
        )
        if reset_detected:
            self._last_lifetime_kwh = lifetime
            self._last_energy_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "lifetime_reset"
            self._last_window_s = None
            self._last_reset_at = sample_ts
            return 0
        if not is_charging:
            self._last_lifetime_kwh = lifetime
            self._last_energy_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "idle"
            self._last_window_s = None
            return 0
        if delta_kwh <= self._MIN_DELTA_KWH:
            return self._last_power_w

        window_s = _resolve_lifetime_power_window(
            sample_ts=sample_ts,
            previous_energy_ts=self._last_energy_ts,
            default_window_s=self._DEFAULT_WINDOW_S,
        )
        self._last_power_w = _energy_delta_to_power_w(
            delta_kwh,
            window_s=window_s,
            floor_zero=True,
            max_watts=self._max_throughput_w,
        )
        self._last_method = "lifetime_energy_window"
        self._last_window_s = window_s
        self._last_lifetime_kwh = lifetime
        self._last_energy_ts = sample_ts
        return self._last_power_w

    @property
    def extra_state_attributes(self):
        data = self.data
        actual_charging = self._is_actually_charging(data)
        operating_v = self._as_float(data.get("operating_v"))
        if operating_v is None or operating_v <= 0:
            operating_v = self._as_float(data.get("nominal_v"))
        if operating_v is None or operating_v <= 0:
            operating_v = float(
                getattr(self._coord, "nominal_voltage", DEFAULT_NOMINAL_VOLTAGE)
            )
        return {
            "sampled_at_utc": (
                data.get("sampled_at_utc")
                if data.get("sampled_at_utc") is not None
                else (
                    datetime.fromtimestamp(
                        self._last_sample_ts, tz=timezone.utc
                    ).isoformat()
                    if self._last_sample_ts is not None
                    else None
                )
            ),
            "last_lifetime_kwh": self._last_lifetime_kwh,
            "last_energy_ts": self._last_energy_ts,
            "last_sample_ts": self._last_sample_ts,
            "last_power_w": self._last_power_w,
            "last_window_seconds": self._last_window_s,
            "method": self._last_method,
            "charging": actual_charging,
            "actual_charging": actual_charging,
            "operating_v": operating_v,
            "max_throughput_w": self._max_throughput_w,
            "max_throughput_unbounded_w": self._max_throughput_unbounded_w,
            "max_throughput_source": self._max_throughput_source,
            "max_throughput_amps": self._max_throughput_amps,
            "max_throughput_voltage": self._max_throughput_voltage,
            "max_throughput_topology": getattr(
                self, "_max_throughput_topology", "unknown"
            ),
            "max_throughput_phase_multiplier": getattr(
                self, "_max_throughput_phase_multiplier", 1.0
            ),
            "last_reset_at": self._last_reset_at,
        }


class EnphaseChargingLevelSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "set_amps"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_suggested_display_precision = 0

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charging_amps"

    @property
    def available(self) -> bool:  # type: ignore[override]
        return super().available and evse_amp_control_applicable(self._coord, self._sn)

    @staticmethod
    def _safe_limit_active(value) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return bool(value)
        try:
            return int(str(value).strip()) != 0
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _charging_active(value) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes", "y", "on"):
                return True
            if normalized in ("false", "0", "no", "n", "off"):
                return False
            return False
        return False

    @staticmethod
    def _optional_bool(value) -> bool | None:
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

    @property
    def native_value(self):
        data = self.data
        if self._safe_limit_active(
            data.get("safe_limit_state")
        ) and self._charging_active(data.get("charging")):
            return self._safe_limit_amps(data)
        lvl = data.get("charging_level")
        if lvl is None:
            # Fall back to coordinator helper which respects charger limits
            return self._coord.pick_start_amps(self._sn)
        try:
            return int(lvl)
        except Exception:
            return self._coord.pick_start_amps(self._sn)

    @staticmethod
    def _coerce_amp(value):
        if value in (None, ""):
            return None
        try:
            return int(float(str(value).strip()))
        except Exception:  # noqa: BLE001
            return None

    @classmethod
    def _safe_limit_amps(cls, data):
        min_amp = cls._coerce_amp(data.get("min_amp"))
        if min_amp is not None and min_amp > 0:
            return min_amp
        return SAFE_LIMIT_AMPS

    @property
    def extra_state_attributes(self):
        min_amp = self._coerce_amp(self.data.get("min_amp"))
        max_amp = self._coerce_amp(self.data.get("max_amp"))
        max_current = self._coerce_amp(self.data.get("max_current"))
        amp_granularity = self._coerce_amp(self.data.get("amp_granularity"))
        safe_limit_state = self.data.get("safe_limit_state")
        return {
            "min_amp": min_amp,
            "max_amp": max_amp,
            "max_current": max_current,
            "amp_granularity": amp_granularity,
            "default_charge_level": self.data.get("default_charge_level"),
            "charging_amps_supported": self._optional_bool(
                self.data.get("charging_amps_supported")
            ),
            "safe_limit_state": safe_limit_state,
            "safe_limit_active": self._safe_limit_active(safe_limit_state),
        }


class EnphaseLastReportedSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_last_rpt"

    @property
    def available(self) -> bool:
        return bool(super().available and self.native_value is not None)

    @property
    def native_value(self):
        from datetime import datetime, timezone

        s = self.data.get("last_reported_at")
        if not s:
            return None
        # Example: 2025-09-07T11:38:31Z[UTC]
        s = str(s).replace("[UTC]", "").replace("Z", "")
        try:
            dt = datetime.fromisoformat(s)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    @property
    def extra_state_attributes(self):
        def _as_int(value):
            if value is None:
                return None
            try:
                return int(str(value).strip())
            except Exception:  # noqa: BLE001
                return None

        def _as_bool(value):
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

        def _clean_text(value):
            if value in (None, ""):
                return None
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                return None
            return text or None

        def _localize(value):
            if value in (None, ""):
                return None
            try:
                if isinstance(value, (int, float)):
                    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
                elif isinstance(value, str):
                    cleaned = value.strip()
                    if not cleaned:
                        return None
                    if cleaned.endswith("[UTC]"):
                        cleaned = cleaned[:-5]
                    if cleaned.endswith("Z"):
                        cleaned = cleaned[:-1] + "+00:00"
                    dt = datetime.fromisoformat(cleaned)
                else:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt_util.as_local(dt).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                return None

        interval_raw = self.data.get("reporting_interval")
        attrs = {
            "reporting_interval": _as_int(interval_raw),
            "connection": _clean_text(self.data.get("connection")),
            "ip_address": _clean_text(self.data.get("ip_address")),
            "mac_address": _clean_text(self.data.get("mac_address")),
            "network_interface_count": _as_int(
                self.data.get("network_interface_count")
            ),
            "operating_voltage": _as_int(self.data.get("operating_v")),
            "charger_timezone": _clean_text(self.data.get("charger_timezone")),
            "firmware_version": _clean_text(self.data.get("firmware_version")),
            "system_version": _clean_text(self.data.get("system_version")),
            "application_version": _clean_text(self.data.get("application_version")),
            "software_version": _clean_text(self.data.get("sw_version")),
            "hardware_version": _clean_text(self.data.get("hw_version")),
            "processor_board_version": _clean_text(
                self.data.get("processor_board_version")
            ),
            "power_board_version": _clean_text(self.data.get("power_board_version")),
            "kernel_version": _clean_text(self.data.get("kernel_version")),
            "bootloader_version": _clean_text(self.data.get("bootloader_version")),
            "default_route": _clean_text(self.data.get("default_route")),
            "wifi_config": _clean_text(self.data.get("wifi_config")),
            "cellular_config": _clean_text(self.data.get("cellular_config")),
            "warranty_start_date": _localize(self.data.get("warranty_start_date")),
            "warranty_due_date": _localize(self.data.get("warranty_due_date")),
            "warranty_period_years": _as_int(self.data.get("warranty_period_years")),
            "created_at": _localize(self.data.get("created_at")),
            "breaker_rating": _as_int(self.data.get("breaker_rating")),
            "rated_current": _as_int(self.data.get("rated_current")),
            "grid_type": _as_int(self.data.get("grid_type")),
            "phase_count": _as_int(self.data.get("phase_count")),
            "phase_switch_config": _clean_text(self.data.get("phase_switch_config")),
            "default_charge_level": _clean_text(self.data.get("default_charge_level")),
            "commissioning_status": _as_int(self.data.get("commissioning_status")),
            "is_connected": _as_bool(self.data.get("is_connected")),
            "is_locally_connected": _as_bool(self.data.get("is_locally_connected")),
            "ho_control": _as_bool(self.data.get("ho_control")),
            "gateway_connection_count": _as_int(
                self.data.get("gateway_connection_count")
            ),
            "gateway_connected_count": _as_int(
                self.data.get("gateway_connected_count")
            ),
            "gateway_last_connection_at": _localize(
                self.data.get("gateway_last_connection_at")
            ),
            "functional_validation_state": _as_int(
                self.data.get("functional_validation_state")
            ),
            "functional_validation_updated_at": _localize(
                self.data.get("functional_validation_updated_at")
            ),
        }
        gateway_details = self.data.get("gateway_connectivity_details")
        if isinstance(gateway_details, list):
            attrs["gateway_connectivity_details"] = [
                {
                    **(
                        {"status": _as_int(detail.get("status"))}
                        if _as_int(detail.get("status")) is not None
                        else {}
                    ),
                    **(
                        {"failure_reason": _as_int(detail.get("failure_reason"))}
                        if _as_int(detail.get("failure_reason")) is not None
                        else {}
                    ),
                    **(
                        {
                            "last_connection_at": _localize(
                                detail.get("last_connection_at")
                            )
                        }
                        if _localize(detail.get("last_connection_at")) is not None
                        else {}
                    ),
                }
                for detail in gateway_details
                if isinstance(detail, dict)
            ]
        return attrs


class EnphaseChargeModeSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charge_mode"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charge_mode"

    @property
    def native_value(self):
        d = self.data
        # Prefer scheduler preference when available for consistency with selector
        return d.get("charge_mode_pref") or d.get("charge_mode")

    @property
    def icon(self) -> str | None:
        # Map charge modes to friendly icons
        mode = str(self.native_value or "").upper()
        mapping = {
            "MANUAL_CHARGING": "mdi:flash",
            "IMMEDIATE": "mdi:flash",
            "SCHEDULED_CHARGING": "mdi:calendar-clock",
            "GREEN_CHARGING": "mdi:leaf",
            "SMART_CHARGING": "mdi:leaf",
            "IDLE": "mdi:timer-sand-paused",
        }
        return mapping.get(mode, "mdi:car-electric")

    @staticmethod
    def _as_bool(value) -> bool | None:
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

    @property
    def extra_state_attributes(self):
        applicable = evse_amp_control_applicable(self._coord, self._sn)
        resolved_mode = evse_resolved_charge_mode(self._coord, self._sn)
        return {
            "preferred_mode": self.data.get("charge_mode_pref"),
            "effective_mode": self.data.get("charge_mode"),
            "charge_mode_supported": self._as_bool(
                self.data.get("charge_mode_supported")
            ),
            "amp_control_applicable": applicable,
            "amp_control_managed_by_mode": None if applicable else resolved_mode,
            "amp_control_applies_in_modes": [
                "MANUAL_CHARGING",
                "SCHEDULED_CHARGING",
                "IMMEDIATE",
            ],
            "schedule_status": self.data.get("schedule_status"),
            "schedule_type": self.data.get("schedule_type"),
            "schedule_slot_id": self.data.get("schedule_slot_id"),
            "schedule_start": self.data.get("schedule_start"),
            "schedule_end": self.data.get("schedule_end"),
            "schedule_days": self.data.get("schedule_days"),
            "schedule_reminder_enabled": self._as_bool(
                self.data.get("schedule_reminder_enabled")
            ),
            "schedule_reminder_minutes": self.data.get("schedule_reminder_min"),
            "green_battery_supported": self._as_bool(
                self.data.get("green_battery_supported")
            ),
            "green_battery_enabled": self._as_bool(
                self.data.get("green_battery_enabled")
            ),
        }


class EnphaseStormGuardStateSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "storm_guard_state"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_storm_guard_state"

    @property
    def available(self) -> bool:  # type: ignore[override]
        if not super().available:
            return False
        if bool(getattr(self._coord, "storm_guard_update_pending", False)):
            return True
        return self.data.get("storm_guard_state") is not None

    @property
    def native_value(self):
        if bool(getattr(self._coord, "storm_guard_update_pending", False)):
            return "Updating"
        raw = self.data.get("storm_guard_state")
        if raw is None:
            return None
        if isinstance(raw, bool):
            return "Enabled" if raw else "Disabled"
        if isinstance(raw, (int, float)):
            return "Enabled" if raw != 0 else "Disabled"
        try:
            normalized = str(raw).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        if normalized in ("enabled", "disabled"):
            return "Enabled" if normalized == "enabled" else "Disabled"
        if normalized in ("true", "1", "yes", "y", "on"):
            return "Enabled"
        if normalized in ("false", "0", "no", "n", "off"):
            return "Disabled"
        return None


class EnphaseChargerAuthenticationSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charger_authentication"

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charger_authentication"

    @property
    def available(self) -> bool:  # type: ignore[override]
        return super().available and self._coord.auth_settings_available

    @property
    def native_value(self):
        required = self.data.get("auth_required")
        if required is True:
            return "enabled"
        if required is False:
            return "disabled"
        return None

    @staticmethod
    def _as_bool(value) -> bool | None:
        if value is None:
            return None
        try:
            return bool(value)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        return {
            "app_auth_enabled": self._as_bool(self.data.get("app_auth_enabled")),
            "rfid_auth_enabled": self._as_bool(self.data.get("rfid_auth_enabled")),
            "app_auth_supported": self._as_bool(self.data.get("app_auth_supported")),
            "rfid_auth_supported": self._as_bool(self.data.get("rfid_auth_supported")),
            "auth_feature_supported": self._as_bool(
                self.data.get("auth_feature_supported")
            ),
            "rfid_feature_supported": self._as_bool(
                self.data.get("rfid_feature_supported")
            ),
            "plug_and_charge_supported": self._as_bool(
                self.data.get("plug_and_charge_supported")
            ),
        }


class EnphaseLifetimeEnergySensor(EnphaseBaseEntity, RestoreSensor):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_translation_key = "lifetime_energy"
    _attr_suggested_display_precision = 2
    # Allow tiny jitter of 0.01 kWh (~10 Wh) before treating value as a drop
    _drop_tolerance = 0.01
    # Heuristics for accepting genuine meter resets reported by the API
    _reset_floor_kwh = 5.0
    _reset_drop_threshold_kwh = 0.5
    _reset_ratio = 0.5

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_lifetime_kwh"
        # Track last good value to avoid publishing bad/zero on startup
        self._last_value: float | None = None
        # Apply a one-shot boot filter to ignore an initial 0/None
        self._boot_filter: bool = True
        self._last_reset_value: float | None = None
        self._last_reset_at: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore native value using RestoreSensor helper (restores native_value/unit)
        last = await self.async_get_last_sensor_data()
        if last is None:
            return
        try:
            val = float(last.native_value) if last.native_value is not None else None
        except Exception:
            val = None
        if val is not None and val >= 0:
            rounded = round(val, 2)
            self._last_value = rounded
            self._attr_native_value = rounded
        try:
            last_state = await self.async_get_last_state()
        except Exception:
            last_state = None
        if last_state is not None:
            attrs = last_state.attributes or {}
            try:
                if attrs.get("last_reset_value") is not None:
                    self._last_reset_value = float(attrs.get("last_reset_value"))
            except Exception:
                self._last_reset_value = None
            reset_at_attr = attrs.get("last_reset_at")
            if isinstance(reset_at_attr, str):
                self._last_reset_at = reset_at_attr

    @property
    def native_value(self):
        raw = self.data.get("lifetime_kwh")
        if raw is None:
            raw = self.data.get("evse_lifetime_energy_kwh")
        # Parse and validate
        val: float | None
        try:
            val = float(raw) if raw is not None else None
        except Exception:
            val = None
        if val is None:
            fallback = self.data.get("evse_lifetime_energy_kwh")
            try:
                val = float(fallback) if fallback is not None else None
            except Exception:
                val = None

        # Reject missing or negative samples outright; keep prior value
        if val is None or val < 0:
            return self._last_value

        # Honor boot filter before running drop/reset heuristics so the initial
        # zero sample reported at startup keeps the restored value.
        if self._boot_filter:
            if val == 0 and (self._last_value or 0) > 0:
                return self._last_value
            # First good sample observed; disable boot filter
            self._boot_filter = False

        # Enforce monotonic behaviour – ignore sudden drops beyond tolerance
        if self._last_value is not None:
            if val + self._drop_tolerance < self._last_value:
                drop = self._last_value - val
                if drop >= self._reset_drop_threshold_kwh and (
                    val <= self._reset_floor_kwh
                    or val <= (self._last_value * self._reset_ratio)
                ):
                    self._last_reset_value = val
                    self._last_reset_at = dt_util.utcnow().isoformat()
                    self._boot_filter = False
                else:
                    return self._last_value
            elif val < self._last_value:
                val = self._last_value

        # Accept sample; remember as last good value
        val = round(val, 2)
        self._last_value = val
        return val

    @property
    def extra_state_attributes(self):
        return {
            "sampled_at_utc": self.data.get("sampled_at_utc"),
            "last_reset_value": self._last_reset_value,
            "last_reset_at": self._last_reset_at,
        }


class EnphaseStatusSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_status"

    @staticmethod
    def _normalize_status(value):
        if value is None:
            return None
        try:
            raw = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        acronyms = {"AC", "API", "DC", "EVSE", "RFID"}
        normalized_parts: list[str] = []
        for part in re.split(r"[\s_-]+", raw):
            if not part:
                continue
            sub_parts = [sub_part for sub_part in part.split("/") if sub_part]
            if not sub_parts:
                continue
            normalized_sub_parts: list[str] = []
            for sub_part in sub_parts:
                upper = sub_part.upper()
                if upper in acronyms:
                    normalized_sub_parts.append(upper)
                else:
                    normalized_sub_parts.append(upper[:1] + upper[1:].lower())
            normalized_parts.append("/".join(normalized_sub_parts))
        if not normalized_parts:
            return None
        return " ".join(normalized_parts)

    @property
    def native_value(self):
        return self._normalize_status(self.data.get("status"))

    @property
    def extra_state_attributes(self):
        def _as_bool(value):
            if value is None:
                return None
            try:
                return bool(value)
            except Exception:  # noqa: BLE001
                return None

        def _as_text(value):
            if value in (None, ""):
                return None
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                return None
            return text or None

        def _localize(value):
            if value in (None, ""):
                return None
            try:
                if isinstance(value, (int, float)):
                    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
                elif isinstance(value, str):
                    cleaned = value.strip()
                    if not cleaned:
                        return None
                    if cleaned.endswith("[UTC]"):
                        cleaned = cleaned[:-5]
                    if cleaned.endswith("Z"):
                        cleaned = cleaned[:-1] + "+00:00"
                    dt = datetime.fromisoformat(cleaned)
                else:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt_util.as_local(dt).isoformat(timespec="seconds")
            except Exception:  # noqa: BLE001
                return None

        return {
            "status_raw": _as_text(self.data.get("status")),
            "commissioned": _as_bool(self.data.get("commissioned")),
            "charger_problem": _as_bool(self.data.get("faulted")),
            "suspended_by_evse": _as_bool(self.data.get("suspended_by_evse")),
            "offline_since": _localize(self.data.get("offline_since")),
        }


## Removed duplicate Current Amps sensor to avoid confusion with Set Amps


## Removed unreliable sensors: Session Miles


class _TimestampFromIsoSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, coord: EnphaseCoordinator, sn: str, key: str, name: str, uniq: str
    ):
        super().__init__(coord, sn)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = uniq

    @property
    def native_value(self):
        from datetime import datetime, timezone

        s = self.data.get(self._key)
        if not s:
            return None
        s = str(s).replace("[UTC]", "").replace("Z", "")
        try:
            dt = datetime.fromisoformat(s)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None


## Removed unreliable sensors: Session Plug-in At


## Removed unreliable sensors: Session Plug-out At


class _TimestampFromEpochSensor(EnphaseBaseEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, coord: EnphaseCoordinator, sn: str, key: str, name: str, uniq: str
    ):
        super().__init__(coord, sn)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = uniq

    @property
    def native_value(self):
        from datetime import datetime, timezone

        ts = self.data.get(self._key)
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except Exception:
            return None


## Removed unreliable sensors: Schedule Type


## Removed unreliable sensors: Schedule Start


## Removed unreliable sensors: Schedule End


class EnphaseTypeInventorySensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator, type_key: str) -> None:
        super().__init__(coord)
        self._coord = coord
        self._type_key = str(type_key)
        label = _type_label(self._coord, self._type_key) or "Device"
        self._attr_name = f"{label} Inventory"
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_type_{self._type_key}_inventory"
        )

    def _fallback_count(self) -> int:
        if self._type_key != "iqevse":
            return 0
        iter_serials = getattr(self._coord, "iter_serials", None)
        if callable(iter_serials):
            try:
                return len([sn for sn in iter_serials() if sn])
            except Exception:
                return 0
        serials = getattr(self._coord, "serials", None)
        if isinstance(serials, (set, list, tuple)):
            return len([sn for sn in serials if sn])
        return 0

    @property
    def available(self) -> bool:
        return bool(
            super().available
            and self._coord.inventory_view.has_type_for_entities(self._type_key)
        )

    @property
    def native_value(self):
        bucket = self._coord.inventory_view.type_bucket(self._type_key) or {}
        try:
            count = int(bucket.get("count", 0))
        except Exception:
            count = 0
        return count or self._fallback_count()

    @property
    def extra_state_attributes(self):
        bucket = self._coord.inventory_view.type_bucket(self._type_key) or {}
        members = bucket.get("devices")
        attrs = {
            "type_key": self._type_key,
            "type_label": bucket.get("type_label")
            or _type_label(self._coord, self._type_key),
            "device_count": bucket.get("count", 0),
            "devices": members if isinstance(members, list) else [],
        }
        status_counts = bucket.get("status_counts")
        if isinstance(status_counts, dict):
            attrs["status_counts"] = dict(status_counts)
        status_summary = bucket.get("status_summary")
        if isinstance(status_summary, str) and status_summary.strip():
            attrs["status_summary"] = status_summary
        model_counts = bucket.get("model_counts")
        if isinstance(model_counts, dict):
            attrs["model_counts"] = dict(model_counts)
        model_summary = bucket.get("model_summary")
        if isinstance(model_summary, str) and model_summary.strip():
            attrs["model_summary"] = model_summary
        firmware_counts = bucket.get("firmware_counts")
        if isinstance(firmware_counts, dict):
            attrs["firmware_counts"] = dict(firmware_counts)
        firmware_summary = bucket.get("firmware_summary")
        if isinstance(firmware_summary, str) and firmware_summary.strip():
            attrs["firmware_summary"] = firmware_summary
        array_counts = bucket.get("array_counts")
        if isinstance(array_counts, dict):
            attrs["array_counts"] = dict(array_counts)
        array_summary = bucket.get("array_summary")
        if isinstance(array_summary, str) and array_summary.strip():
            attrs["array_summary"] = array_summary
        panel_info = bucket.get("panel_info")
        if isinstance(panel_info, dict):
            attrs["panel_info"] = dict(panel_info)
        status_type_counts = bucket.get("status_type_counts")
        if isinstance(status_type_counts, dict):
            attrs["status_type_counts"] = dict(status_type_counts)
        connectivity_state = bucket.get("connectivity_state")
        if isinstance(connectivity_state, str) and connectivity_state.strip():
            attrs["connectivity_state"] = connectivity_state
        reporting_count = bucket.get("reporting_count")
        if reporting_count is not None:
            attrs["reporting_count"] = reporting_count
        latest_reported_utc = bucket.get("latest_reported_utc")
        if isinstance(latest_reported_utc, str) and latest_reported_utc.strip():
            attrs["latest_reported_utc"] = latest_reported_utc
        latest_reported_device = bucket.get("latest_reported_device")
        if isinstance(latest_reported_device, dict):
            attrs["latest_reported_device"] = dict(latest_reported_device)
        production_start = bucket.get("production_start_date")
        if isinstance(production_start, str) and production_start.strip():
            attrs["production_start_date"] = production_start
        production_end = bucket.get("production_end_date")
        if isinstance(production_end, str) and production_end.strip():
            attrs["production_end_date"] = production_end
        return attrs

    @property
    def device_info(self):
        from homeassistant.helpers.entity import DeviceInfo

        info = self._coord.inventory_view.type_device_info(self._type_key)
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:{self._type_key}")},
            manufacturer="Enphase",
        )


class EnphaseInverterLifetimeEnergySensor(CoordinatorEntity, RestoreSensor):
    """Lifetime production for one inverter under the shared microinverter device."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord)
        self._coord = coord
        self._sn = str(serial)
        self._attr_name = f"{self._sn} Lifetime Energy"
        self._attr_unique_id = f"{DOMAIN}_inverter_{self._sn}_lifetime_energy"
        self._last_good_native_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Legacy builds briefly published MWh. Force canonical unit for this sensor.
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        last = await self.async_get_last_sensor_data()
        if last is None:
            return
        try:
            restored = (
                float(last.native_value) if last.native_value is not None else None
            )
        except Exception:  # noqa: BLE001
            restored = None
        if restored is not None and restored >= 0:
            restored_unit = getattr(last, "native_unit_of_measurement", None)
            unit_text = ""
            if restored_unit is not None:
                try:
                    unit_text = str(restored_unit).strip().lower()
                except Exception:  # noqa: BLE001
                    unit_text = ""
            if unit_text == "mwh":
                restored *= 1000.0
            elif unit_text == "wh":
                restored /= 1000.0
            if not math.isfinite(restored) or restored < 0:
                return
            restored = round(restored, 2)
            self._last_good_native_value = restored
            self._attr_native_value = restored

    def _snapshot(self) -> dict[str, object] | None:
        getter = getattr(self._coord, "inverter_data", None)
        if not callable(getter):
            return None
        data = getter(self._sn)
        if isinstance(data, dict):
            return data
        return None

    @property
    def available(self) -> bool:
        return bool(super().available and self._snapshot() is not None)

    @property
    def native_value(self):
        data = self._snapshot()
        if not isinstance(data, dict):
            return self._last_good_native_value
        raw_wh = data.get("lifetime_production_wh")
        try:
            value_wh = float(raw_wh) if raw_wh is not None else None
        except (TypeError, ValueError):
            value_wh = None
        if value_wh is None or value_wh < 0:
            return self._last_good_native_value
        value_kwh = round(value_wh / 1000.0, 2)
        if (
            self._last_good_native_value is not None
            and value_kwh < self._last_good_native_value
        ):
            return self._last_good_native_value
        self._last_good_native_value = value_kwh
        return value_kwh

    @property
    def extra_state_attributes(self):
        data = self._snapshot() or {}
        sampled_at = _battery_parse_timestamp(
            data.get("last_report")
            or data.get("last_reported")
            or data.get("last_reported_at")
            or data.get("lastReportedAt")
        )
        return {
            "sampled_at_utc": (
                sampled_at.isoformat() if sampled_at is not None else None
            ),
            "serial_number": data.get("serial_number"),
            "inverter_id": data.get("inverter_id"),
            "device_id": data.get("device_id"),
            "inverter_type": data.get("inverter_type"),
            "name": data.get("name"),
            "array_name": data.get("array_name"),
            "sku_id": data.get("sku_id"),
            "part_num": data.get("part_num"),
            "sku": data.get("sku"),
            "status": data.get("status"),
            "status_text": data.get("status_text"),
            "status_code": data.get("status_code"),
            "last_report": data.get("last_report"),
            "fw1": data.get("fw1"),
            "fw2": data.get("fw2"),
            "warranty_end_date": data.get("warranty_end_date"),
            "show_sig_str": data.get("show_sig_str"),
            "emu_version": data.get("emu_version"),
            "issi": data.get("issi"),
            "rssi": data.get("rssi"),
            "lifetime_production_wh": data.get("lifetime_production_wh"),
            "lifetime_query_start_date": data.get("lifetime_query_start_date"),
            "lifetime_query_end_date": data.get("lifetime_query_end_date"),
        }

    @property
    def device_info(self):
        from homeassistant.helpers.entity import DeviceInfo

        info = _type_device_info(self._coord, "microinverter")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:microinverter")},
            manufacturer="Enphase",
            name="IQ Microinverters",
        )


class _EnphaseBatteryStorageBaseSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coord: EnphaseCoordinator, serial: str, unique_suffix: str
    ) -> None:
        super().__init__(coord)
        self._coord = coord
        self._sn = str(serial)
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_battery_{self._sn}{unique_suffix}"
        )

    def _snapshot(self) -> dict[str, object] | None:
        getter = getattr(self._coord, "battery_storage", None)
        if not callable(getter):
            return None
        payload = getter(self._sn)
        if isinstance(payload, dict):
            return payload
        return None

    @staticmethod
    def _as_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        return _battery_parse_timestamp(value)

    @property
    def available(self) -> bool:
        if not _type_available(self._coord, "encharge"):
            return False
        return bool(super().available and self._snapshot() is not None)

    @property
    def _battery_label(self) -> str:
        snapshot = self._snapshot() or {}
        for key in ("name", "serial_number", "identity"):
            value = snapshot.get(key)
            if value is None:
                continue
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                return text
        return self._sn

    @property
    def device_info(self):
        from homeassistant.helpers.entity import DeviceInfo

        info = _type_device_info(self._coord, "encharge")
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:encharge")},
            manufacturer="Enphase",
            name="IQ Battery",
        )


class EnphaseBatteryStorageChargeSensor(_EnphaseBatteryStorageBaseSensor):
    """Per-battery state-of-charge sensor under the shared battery type device."""

    _attr_translation_key = "battery_storage_charge"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_charge_level")

    @property
    def name(self) -> str:
        return self._battery_label

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        value = snapshot.get("current_charge_pct")
        if value is None:
            return None
        try:
            return round(float(value), 1)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        attrs = dict(self._snapshot() or {})
        attrs.pop("led_status", None)
        return attrs


class EnphaseBatteryStorageStatusSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_status"

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_status")
        self._attr_translation_placeholders = {"serial": self._sn}

    @staticmethod
    def _led_status_value(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value) if value.is_integer() else None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        if not text:
            return None
        try:
            parsed = float(text)
        except Exception:  # noqa: BLE001
            return None
        if not math.isfinite(parsed) or not parsed.is_integer():
            return None
        return int(parsed)

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        led_status = self._led_status_value(snapshot.get("led_status"))
        return BATTERY_LED_STATUS_STATE_MAP.get(led_status, "unknown")

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot() or {}
        attrs: dict[str, object] = {}
        led_status = self._led_status_value(snapshot.get("led_status"))
        if led_status is not None:
            attrs["state"] = led_status
        return attrs


class EnphaseBatteryStorageHealthSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_health"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_health")
        self._attr_translation_placeholders = {"serial": self._sn}

    @staticmethod
    def _parse_health_value(value: object) -> float | None:
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        if not text:
            return None
        if text.endswith("%"):
            text = text[:-1].strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except Exception:  # noqa: BLE001
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    def _health_value(self) -> float | None:
        snapshot = self._snapshot() or {}
        for key in (
            "battery_soh",
            "soh",
            "state_of_health",
            "stateOfHealth",
            "battery_health",
            "health",
        ):
            parsed = self._parse_health_value(snapshot.get(key))
            if parsed is not None:
                return parsed
        return None

    @property
    def available(self) -> bool:
        return bool(super().available and self._health_value() is not None)

    @property
    def native_value(self):
        value = self._health_value()
        if value is None:
            return None
        return round(value, 1)


class EnphaseBatteryStorageCycleCountSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_cycle_count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_cycle_count")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        return self._as_int(snapshot.get("cycle_count"))


class EnphaseBatteryStorageLastReportedSensor(_EnphaseBatteryStorageBaseSensor):
    _attr_translation_key = "battery_storage_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_last_reported")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def available(self) -> bool:
        return bool(super().available and self.native_value is not None)

    @property
    def native_value(self):
        return _battery_snapshot_last_reported(self._snapshot() or {})


class _EnphaseAcBatteryStorageBaseSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coord: EnphaseCoordinator, serial: str, unique_suffix: str
    ) -> None:
        super().__init__(coord)
        self._coord = coord
        self._sn = str(serial)
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_ac_battery_{self._sn}{unique_suffix}"
        )

    def _snapshot(self) -> dict[str, object] | None:
        return ac_battery_storage_snapshot(self._coord, self._sn)

    @staticmethod
    def _as_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(round(float(str(value).strip())))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _as_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)
        except Exception:
            try:
                parsed = float(str(value).strip())
            except Exception:  # noqa: BLE001
                return None
        if not math.isfinite(parsed):
            return None
        return parsed

    @property
    def available(self) -> bool:
        if not _type_available(self._coord, "ac_battery"):
            return False
        return bool(super().available and self._snapshot() is not None)

    @property
    def _battery_label(self) -> str:
        snapshot = self._snapshot() or {}
        serial_number = snapshot.get("serial_number")
        if serial_number is not None:
            try:
                text = str(serial_number).strip()
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                return text
        return self._sn

    @property
    def device_info(self):
        return ac_battery_device_info(self._coord)


class EnphaseAcBatteryStorageChargeSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_charge"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_charge_level")

    @property
    def name(self) -> str:
        return self._battery_label

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        value = self._as_float(snapshot.get("current_charge_pct"))
        if value is None:
            return None
        return round(value, 1)

    @property
    def extra_state_attributes(self):
        snapshot = dict(self._snapshot() or {})
        return {
            "battery_id": snapshot.get("battery_id"),
            "part_number": snapshot.get("part_number"),
            "phase": snapshot.get("phase"),
            "status_text": snapshot.get("status_text"),
            "sleep_state": snapshot.get("sleep_state"),
        }


class EnphaseAcBatteryStorageStatusSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_status"

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_status")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        value = snapshot.get("status_normalized")
        if value is None:
            return None
        try:
            text = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot() or {}
        return {
            "battery_id": snapshot.get("battery_id"),
            "status_text": snapshot.get("status_text"),
            "sleep_state": snapshot.get("sleep_state"),
            "sleep_control_class": snapshot.get("sleep_control_class"),
            "sleep_control_label": snapshot.get("sleep_control_label"),
        }


class EnphaseAcBatteryStoragePowerSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_power")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        value = self._as_float(snapshot.get("power_w"))
        if value is None:
            return None
        return round(value, 3)

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot() or {}
        reported = ac_battery_snapshot_last_reported(snapshot)
        return {
            "operating_mode": snapshot.get("operating_mode"),
            "last_reported_utc": (
                reported.isoformat() if reported is not None else None
            ),
        }


class EnphaseAcBatteryStorageOperatingModeSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_operating_mode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_operating_mode")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        value = snapshot.get("operating_mode")
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot() or {}
        return {
            "status_text": snapshot.get("status_text"),
            "sleep_state": snapshot.get("sleep_state"),
        }


class EnphaseAcBatteryStorageCycleCountSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_cycle_count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_cycle_count")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def native_value(self):
        snapshot = self._snapshot() or {}
        return self._as_int(snapshot.get("cycle_count"))


class EnphaseAcBatteryStorageLastReportedSensor(_EnphaseAcBatteryStorageBaseSensor):
    _attr_translation_key = "ac_battery_storage_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial, "_last_reported")
        self._attr_translation_placeholders = {"serial": self._sn}

    @property
    def available(self) -> bool:
        return bool(super().available and self.native_value is not None)

    @property
    def native_value(self):
        return ac_battery_snapshot_last_reported(self._snapshot() or {})


_GATEWAY_STATUS_KEYS: tuple[str, ...] = ("statusText", "status")
_GATEWAY_MODEL_KEYS: tuple[str, ...] = ("model", "channel_type", "sku_id")
_GATEWAY_FIRMWARE_KEYS: tuple[str, ...] = ("envoy_sw_version", "sw_version")
_GATEWAY_LAST_REPORT_KEYS: tuple[str, ...] = (
    "last_report",
    "last_reported",
    "lastReportedAt",
)


def _gateway_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "y", "enabled", "enable", "on"):
            return True
        if normalized in ("false", "0", "no", "n", "disabled", "disable", "off"):
            return False
    return None


def _gateway_normalize_status(value: object) -> str:
    text = _gateway_clean_text(value)
    if not text:
        return "unknown"
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    if any(token in normalized for token in ("fault", "error", "critical")):
        return "error"
    if "warn" in normalized:
        return "warning"
    if any(
        token in normalized
        for token in ("not_reporting", "offline", "disconnected", "retired")
    ):
        return "not_reporting"
    if any(token in normalized for token in ("normal", "online", "connected", "ok")):
        return "normal"
    return "unknown"


def _gateway_parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    timestamp_seconds: float | None = None
    if isinstance(value, (int, float)):
        try:
            timestamp_seconds = float(value)
        except Exception:  # noqa: BLE001
            timestamp_seconds = None
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            timestamp_seconds = float(cleaned)
        except Exception:
            timestamp_seconds = None
        if timestamp_seconds is None:
            normalized = cleaned.replace("[UTC]", "").replace("Z", "+00:00")
            parsed = dt_util.parse_datetime(normalized)
            if parsed is None:
                try:
                    parsed = datetime.fromisoformat(normalized)
                except Exception:  # noqa: BLE001
                    return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    if timestamp_seconds is None:
        return None
    if timestamp_seconds > 1_000_000_000_000:
        timestamp_seconds /= 1000.0
    try:
        return datetime.fromtimestamp(timestamp_seconds, tz=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _gateway_format_counts(counts: dict[str, int]) -> str | None:
    clean: dict[str, int] = {}
    for key, value in (counts or {}).items():
        label = _gateway_clean_text(key)
        if not label:
            continue
        try:
            count = int(value)
        except Exception:  # noqa: BLE001
            continue
        if count <= 0:
            continue
        clean[label] = count
    if not clean:
        return None
    ordered = sorted(clean.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{name} x{count}" for name, count in ordered)


def _gateway_inventory_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    summary_getter = getattr(coord, "gateway_inventory_summary", None)
    if callable(summary_getter):
        try:
            snapshot = summary_getter()
        except Exception:  # noqa: BLE001
            snapshot = None
        if isinstance(snapshot, dict):
            return snapshot
    bucket = coord.inventory_view.type_bucket("envoy") or {}
    members_raw = bucket.get("devices")
    members = (
        [item for item in members_raw if isinstance(item, dict)]
        if isinstance(members_raw, list)
        else []
    )
    detail_getter = getattr(coord, "system_dashboard_envoy_detail", None)
    dashboard_envoy = detail_getter() if callable(detail_getter) else None
    if not members and isinstance(dashboard_envoy, dict):
        members = [dict(dashboard_envoy)]
    try:
        total_devices = int(bucket.get("count", len(members)))
    except Exception:  # noqa: BLE001
        total_devices = len(members)
    total_devices = max(total_devices, len(members))

    status_counts: dict[str, int] = {
        "normal": 0,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
        "unknown": 0,
    }
    model_counts: dict[str, int] = {}
    firmware_counts: dict[str, int] = {}
    property_keys: set[str] = set()
    connected_devices = 0
    disconnected_devices = 0
    latest_reported: datetime | None = None
    latest_reported_device: dict[str, object] | None = None
    without_last_report_count = 0

    for member in members:
        property_keys.update(str(key) for key in member.keys())

        status_source = None
        for key in _GATEWAY_STATUS_KEYS:
            if member.get(key) is not None:
                status_source = member.get(key)
                break
        status = _gateway_normalize_status(status_source)
        status_counts[status] = status_counts.get(status, 0) + 1

        connected = _gateway_optional_bool(member.get("connected"))
        if connected is None:
            if status == "normal":
                connected = True
            elif status == "not_reporting":
                connected = False
        if connected is True:
            connected_devices += 1
        elif connected is False:
            disconnected_devices += 1

        model_name = None
        for key in _GATEWAY_MODEL_KEYS:
            model_name = _gateway_clean_text(member.get(key))
            if model_name:
                break
        if model_name:
            model_counts[model_name] = model_counts.get(model_name, 0) + 1

        firmware_version = None
        for key in _GATEWAY_FIRMWARE_KEYS:
            firmware_version = _gateway_clean_text(member.get(key))
            if firmware_version:
                break
        if firmware_version:
            firmware_counts[firmware_version] = (
                firmware_counts.get(firmware_version, 0) + 1
            )

        parsed_last_report = None
        for key in _GATEWAY_LAST_REPORT_KEYS:
            parsed_last_report = _gateway_parse_timestamp(member.get(key))
            if parsed_last_report is not None:
                break
        if parsed_last_report is None:
            without_last_report_count += 1
            continue
        if latest_reported is None or parsed_last_report > latest_reported:
            latest_reported = parsed_last_report
            latest_reported_device = {
                "name": _gateway_clean_text(member.get("name")),
                "serial_number": _gateway_clean_text(member.get("serial_number")),
                "status": _gateway_clean_text(status_source),
            }

    unknown_connection_devices = max(
        0, total_devices - connected_devices - disconnected_devices
    )
    status_summary = (
        f"Normal {status_counts.get('normal', 0)} | "
        f"Warning {status_counts.get('warning', 0)} | "
        f"Error {status_counts.get('error', 0)} | "
        f"Not Reporting {status_counts.get('not_reporting', 0)} | "
        f"Unknown {status_counts.get('unknown', 0)}"
    )
    if total_devices <= 0:
        status_summary = None
    if latest_reported is None and isinstance(dashboard_envoy, dict):
        fallback_last = None
        for key in ("last_report", "last_interval_end_date"):
            fallback_last = _gateway_parse_timestamp(dashboard_envoy.get(key))
            if fallback_last is not None:
                break
        if fallback_last is not None:
            latest_reported = fallback_last
            latest_reported_device = {
                "name": _gateway_clean_text(dashboard_envoy.get("name"))
                or "IQ Gateway",
                "serial_number": _gateway_clean_text(
                    dashboard_envoy.get("serial_number")
                ),
                "status": _gateway_clean_text(
                    dashboard_envoy.get("statusText")
                    if dashboard_envoy.get("statusText") is not None
                    else dashboard_envoy.get("status")
                ),
            }

    return {
        "total_devices": total_devices,
        "connected_devices": connected_devices,
        "disconnected_devices": disconnected_devices,
        "unknown_connection_devices": unknown_connection_devices,
        "without_last_report_count": without_last_report_count,
        "status_counts": status_counts,
        "status_summary": status_summary,
        "model_counts": model_counts,
        "model_summary": _gateway_format_counts(model_counts),
        "firmware_counts": firmware_counts,
        "firmware_summary": _gateway_format_counts(firmware_counts),
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_reported_device,
        "property_keys": sorted(property_keys),
    }


def _gateway_connectivity_state(snapshot: dict[str, object]) -> str | None:
    total = int(snapshot.get("total_devices", 0) or 0)
    connected = int(snapshot.get("connected_devices", 0) or 0)
    disconnected = int(snapshot.get("disconnected_devices", 0) or 0)
    unknown = int(snapshot.get("unknown_connection_devices", 0) or 0)
    if total <= 0:
        return None
    if connected >= total:
        return "online"
    if connected == 0 and disconnected > 0:
        return "offline"
    if connected > 0 and connected < total:
        return "degraded"
    if unknown >= total:
        return "unknown"
    return "degraded"


def _microinverter_connectivity_state(snapshot: dict[str, object]) -> str | None:
    total = int(snapshot.get("total_inverters", 0) or 0)
    reporting = int(snapshot.get("reporting_inverters", 0) or 0)
    not_reporting = int(snapshot.get("not_reporting_inverters", 0) or 0)
    unknown = int(snapshot.get("unknown_inverters", 0) or 0)
    if total <= 0:
        return None
    if reporting >= total:
        return "online"
    if reporting == 0 and not_reporting > 0:
        return "offline"
    if reporting > 0 and reporting < total:
        return "degraded"
    if unknown >= total:
        return "unknown"
    return "degraded"


def _microinverter_inventory_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    summary_getter = getattr(coord, "microinverter_inventory_summary", None)
    if callable(summary_getter):
        try:
            snapshot = summary_getter()
        except Exception:  # noqa: BLE001
            snapshot = None
        if isinstance(snapshot, dict):
            return snapshot
    bucket = coord.inventory_view.type_bucket("microinverter") or {}
    members = bucket.get("devices")
    if isinstance(members, list):
        safe_members = [dict(item) for item in members if isinstance(item, dict)]
    else:
        safe_members = []

    status_counts_raw = bucket.get("status_counts")
    status_counts: dict[str, int] = {}
    has_status_counts = isinstance(status_counts_raw, dict)
    if isinstance(status_counts_raw, dict):
        for key in ("total", "normal", "warning", "error", "not_reporting", "unknown"):
            try:
                status_counts[key] = int(status_counts_raw.get(key, 0) or 0)
            except Exception:
                status_counts[key] = 0

    try:
        total_inverters = int(bucket.get("count", len(safe_members)) or 0)
    except Exception:
        total_inverters = len(safe_members)
    if status_counts.get("total", 0) > 0:
        total_inverters = max(total_inverters, int(status_counts.get("total", 0)))

    not_reporting = max(0, int(status_counts.get("not_reporting", 0)))
    unknown = max(0, int(status_counts.get("unknown", 0)))
    if not has_status_counts:
        unknown = total_inverters
    elif (
        total_inverters > 0
        and int(status_counts.get("total", 0) or 0) <= 0
        and max(
            0,
            int(status_counts.get("normal", 0) or 0)
            + int(status_counts.get("warning", 0) or 0)
            + int(status_counts.get("error", 0) or 0)
            + not_reporting
            + unknown,
        )
        == 0
    ):
        unknown = total_inverters
    known_status_total = not_reporting + unknown
    if known_status_total > total_inverters:
        overflow = known_status_total - total_inverters
        unknown = max(0, unknown - overflow)
    reporting = max(0, total_inverters - not_reporting - unknown)

    latest_reported = _gateway_parse_timestamp(
        bucket.get("latest_reported_utc")
        if bucket.get("latest_reported_utc") is not None
        else bucket.get("latest_reported")
    )
    latest_reported_device = (
        dict(bucket.get("latest_reported_device"))
        if isinstance(bucket.get("latest_reported_device"), dict)
        else None
    )
    if latest_reported is None:
        for member in safe_members:
            parsed_last = _gateway_parse_timestamp(member.get("last_report"))
            if parsed_last is None:
                continue
            if latest_reported is None or parsed_last > latest_reported:
                latest_reported = parsed_last
                latest_reported_device = {
                    "serial_number": _gateway_clean_text(member.get("serial_number")),
                    "name": _gateway_clean_text(member.get("name")),
                    "status": _gateway_clean_text(
                        member.get("statusText")
                        if member.get("statusText") is not None
                        else member.get("status")
                    ),
                }

    snapshot: dict[str, object] = {
        "total_inverters": total_inverters,
        "reporting_inverters": reporting,
        "not_reporting_inverters": not_reporting,
        "unknown_inverters": unknown,
        "status_counts": status_counts,
        "status_summary": bucket.get("status_summary"),
        "model_summary": bucket.get("model_summary"),
        "firmware_summary": bucket.get("firmware_summary"),
        "array_summary": bucket.get("array_summary"),
        "panel_info": (
            dict(bucket.get("panel_info"))
            if isinstance(bucket.get("panel_info"), dict)
            else None
        ),
        "status_type_counts": (
            dict(bucket.get("status_type_counts"))
            if isinstance(bucket.get("status_type_counts"), dict)
            else None
        ),
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_reported_device,
        "production_start_date": bucket.get("production_start_date"),
        "production_end_date": bucket.get("production_end_date"),
    }
    connectivity_state = bucket.get("connectivity_state")
    if not isinstance(connectivity_state, str) or not connectivity_state.strip():
        connectivity_state = _microinverter_connectivity_state(snapshot)
    snapshot["connectivity_state"] = connectivity_state
    return snapshot


def _heatpump_member_device_type(member: dict[str, object] | None) -> str | None:
    if not isinstance(member, dict):
        return None
    value = (
        member.get("device_type")
        if member.get("device_type") is not None
        else member.get("device-type")
    )
    text = _gateway_clean_text(value)
    if not text:
        return None
    return text.upper()


def _heatpump_member_status_text(member: dict[str, object] | None) -> str | None:
    return heatpump_status_text(member)


def _heatpump_status_counts(members: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {
        "total": len(members),
        "normal": 0,
        "warning": 0,
        "error": 0,
        "not_reporting": 0,
        "unknown": 0,
    }
    for member in members:
        status_key = EnphaseCoordinator._normalize_inverter_status(
            _heatpump_member_status_text(member)
        )
        counts[status_key] = int(counts.get(status_key, 0)) + 1
    return counts


def _heatpump_worst_status_text(status_counts: dict[str, int]) -> str | None:
    if int(status_counts.get("error", 0) or 0) > 0:
        return "Error"
    if int(status_counts.get("warning", 0) or 0) > 0:
        return "Warning"
    if int(status_counts.get("not_reporting", 0) or 0) > 0:
        return "Not Reporting"
    if int(status_counts.get("unknown", 0) or 0) > 0:
        return "Unknown"
    if int(status_counts.get("normal", 0) or 0) > 0:
        return "Normal"
    return None


def _heatpump_member_last_reported(member: dict[str, object] | None) -> datetime | None:
    if not isinstance(member, dict):
        return None
    for key in ("last_report", "last_reported", "last_reported_at", "last-report"):
        parsed = _gateway_parse_timestamp(member.get(key))
        if parsed is not None:
            return parsed
    return None


def _heatpump_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    summary_getter = getattr(coord, "heatpump_inventory_summary", None)
    if callable(summary_getter):
        try:
            snapshot = summary_getter()
        except Exception:  # noqa: BLE001
            snapshot = None
        if isinstance(snapshot, dict):
            return snapshot
    bucket = coord.inventory_view.type_bucket("heatpump") or {}
    members = bucket.get("devices")
    safe_members = (
        [dict(item) for item in members if isinstance(item, dict)]
        if isinstance(members, list)
        else []
    )
    status_counts_raw = bucket.get("status_counts")
    status_counts: dict[str, int] | None = None
    if isinstance(status_counts_raw, dict):
        parsed_counts = {
            "total": 0,
            "normal": 0,
            "warning": 0,
            "error": 0,
            "not_reporting": 0,
            "unknown": 0,
        }
        try:
            for key in parsed_counts:
                parsed_counts[key] = int(status_counts_raw.get(key, 0) or 0)
            status_counts = parsed_counts
        except Exception:
            status_counts = None
    if status_counts is None:
        status_counts = _heatpump_status_counts(safe_members)

    try:
        total_devices = int(bucket.get("count", len(safe_members)) or 0)
    except Exception:
        total_devices = len(safe_members)
    if total_devices <= 0:
        total_devices = len(safe_members)
    status_counts["total"] = max(int(status_counts.get("total", 0) or 0), total_devices)

    latest_reported = _gateway_parse_timestamp(
        bucket.get("latest_reported_utc")
        if bucket.get("latest_reported_utc") is not None
        else bucket.get("latest_reported")
    )
    latest_reported_device = (
        dict(bucket.get("latest_reported_device"))
        if isinstance(bucket.get("latest_reported_device"), dict)
        else None
    )
    without_last_report_count = 0
    if latest_reported is None:
        for member in safe_members:
            member_last_reported = _heatpump_member_last_reported(member)
            if member_last_reported is None:
                without_last_report_count += 1
                continue
            if latest_reported is None or member_last_reported > latest_reported:
                latest_reported = member_last_reported
                latest_reported_device = {
                    "device_type": _heatpump_member_device_type(member),
                    "name": _gateway_clean_text(member.get("name")),
                    "device_uid": _gateway_clean_text(
                        member.get("device_uid")
                        if member.get("device_uid") is not None
                        else member.get("device-uid")
                    ),
                    "status": _heatpump_member_status_text(member),
                }
    overall_status_text = _gateway_clean_text(bucket.get("overall_status_text"))
    if not overall_status_text:
        for member in safe_members:
            if _heatpump_member_device_type(member) != "HEAT_PUMP":
                continue
            overall_status_text = _heatpump_member_status_text(member)
            if overall_status_text:
                break
    if not overall_status_text:
        overall_status_text = _heatpump_worst_status_text(status_counts)

    device_type_counts: dict[str, int]
    if isinstance(bucket.get("device_type_counts"), dict):
        device_type_counts = {}
        for key, value in bucket.get("device_type_counts", {}).items():
            if key is None:
                continue
            try:
                count = int(value)
            except Exception:
                continue
            if count <= 0:
                continue
            device_type_counts[str(key)] = count
    else:
        device_type_counts = {}
        for member in safe_members:
            device_type = _heatpump_member_device_type(member) or "UNKNOWN"
            device_type_counts[device_type] = device_type_counts.get(device_type, 0) + 1

    status_summary = bucket.get("status_summary")
    if not isinstance(status_summary, str) or not status_summary.strip():
        status_summary = EnphaseCoordinator._format_inverter_status_summary(
            status_counts
        )

    hems_last_success_utc = getattr(coord, "_hems_devices_last_success_utc", None)
    if not isinstance(hems_last_success_utc, datetime):
        hems_last_success_utc = None
    hems_last_success_mono = getattr(coord, "_hems_devices_last_success_mono", None)
    hems_last_success_age_s: float | None = None
    if isinstance(hems_last_success_mono, (int, float)):
        age = time.monotonic() - float(hems_last_success_mono)
        if age >= 0:
            hems_last_success_age_s = round(age, 1)

    return {
        "total_devices": total_devices,
        "members": safe_members,
        "status_counts": status_counts,
        "status_summary": status_summary,
        "device_type_counts": device_type_counts,
        "model_summary": bucket.get("model_summary"),
        "firmware_summary": bucket.get("firmware_summary"),
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_reported_device,
        "without_last_report_count": without_last_report_count,
        "overall_status_text": overall_status_text,
        "hems_data_stale": bool(getattr(coord, "_hems_devices_using_stale", False)),
        "hems_last_success_utc": (
            hems_last_success_utc.isoformat()
            if hems_last_success_utc is not None
            else None
        ),
        "hems_last_success_age_s": hems_last_success_age_s,
    }


def _heatpump_type_snapshot(
    coord: EnphaseCoordinator, *, device_type: str
) -> dict[str, object]:
    summary_getter = getattr(coord, "heatpump_type_summary", None)
    if callable(summary_getter):
        try:
            snapshot = summary_getter(device_type)
        except Exception:  # noqa: BLE001
            snapshot = None
        if isinstance(snapshot, dict):
            return snapshot
    snapshot = _heatpump_snapshot(coord)
    members = [
        member
        for member in snapshot.get("members", [])
        if isinstance(member, dict)
        and _heatpump_member_device_type(member) == device_type.upper()
    ]
    counts = _heatpump_status_counts(members)
    latest_reported: datetime | None = None
    latest_device: dict[str, object] | None = None
    for member in members:
        member_last_reported = _heatpump_member_last_reported(member)
        if member_last_reported is None:
            continue
        if latest_reported is None or member_last_reported > latest_reported:
            latest_reported = member_last_reported
            latest_device = {
                "name": _gateway_clean_text(member.get("name")),
                "device_uid": _gateway_clean_text(
                    member.get("device_uid")
                    if member.get("device_uid") is not None
                    else member.get("device-uid")
                ),
                "status": _heatpump_member_status_text(member),
            }
    status_texts = [
        status
        for status in (_heatpump_member_status_text(member) for member in members)
        if status
    ]
    unique_statuses = list(dict.fromkeys(status_texts))
    if len(unique_statuses) == 1:
        native_status = unique_statuses[0]
    else:
        native_status = _heatpump_worst_status_text(counts)
    return {
        "device_type": device_type.upper(),
        "members": members,
        "member_count": len(members),
        "status_counts": counts,
        "status_summary": EnphaseCoordinator._format_inverter_status_summary(counts),
        "native_status": native_status,
        "latest_reported": latest_reported,
        "latest_reported_utc": (
            latest_reported.isoformat() if latest_reported is not None else None
        ),
        "latest_reported_device": latest_device,
        "hems_data_stale": snapshot.get("hems_data_stale"),
        "hems_last_success_utc": snapshot.get("hems_last_success_utc"),
        "hems_last_success_age_s": snapshot.get("hems_last_success_age_s"),
    }


def _heatpump_runtime_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    snapshot = getattr(coord, "heatpump_runtime_state", None)
    if isinstance(snapshot, dict):
        return dict(snapshot)
    return {}


def _heatpump_daily_snapshot(coord: EnphaseCoordinator) -> dict[str, object]:
    snapshot = getattr(coord, "heatpump_daily_consumption", None)
    if isinstance(snapshot, dict):
        return dict(snapshot)
    return {}


def _heatpump_runtime_device_uid(coord: EnphaseCoordinator) -> str | None:
    getter = getattr(coord, "_heatpump_runtime_device_uid", None)
    if callable(getter):
        try:
            return _gateway_clean_text(getter())
        except Exception:  # noqa: BLE001
            return None
    return None


def _heatpump_runtime_last_reported(snapshot: dict[str, object]) -> datetime | None:
    return _gateway_parse_timestamp(
        snapshot.get("last_report_at")
        if snapshot.get("last_report_at") is not None
        else snapshot.get("last_reported_at")
    )


def _heatpump_runtime_common_attrs(
    coord: EnphaseCoordinator, snapshot: dict[str, object]
) -> dict[str, object]:
    last_reported = _heatpump_runtime_last_reported(snapshot)
    return {
        "device_uid": snapshot.get("device_uid"),
        "member_name": snapshot.get("member_name"),
        "member_device_type": snapshot.get("member_device_type"),
        "pairing_status": snapshot.get("pairing_status"),
        "device_state": snapshot.get("device_state"),
        "runtime_endpoint_type": snapshot.get("endpoint_type"),
        "runtime_endpoint_timestamp": snapshot.get("endpoint_timestamp"),
        "last_report_at_utc": (
            last_reported.isoformat() if last_reported is not None else None
        ),
        "source": snapshot.get("source"),
        "using_stale": bool(
            getattr(coord, "heatpump_runtime_state_using_stale", False)
        ),
        "last_success_utc": (
            coord.heatpump_runtime_state_last_success_utc.isoformat()
            if getattr(coord, "heatpump_runtime_state_last_success_utc", None)
            is not None
            else None
        ),
        "last_error": getattr(coord, "heatpump_runtime_state_last_error", None),
    }


def _heatpump_daily_common_attrs(
    coord: EnphaseCoordinator, snapshot: dict[str, object]
) -> dict[str, object]:
    return {
        "sampled_at_utc": snapshot.get("sampled_at_utc"),
        "device_uid": snapshot.get("device_uid"),
        "device_name": snapshot.get("device_name"),
        "split_device_uid": snapshot.get("split_device_uid"),
        "split_device_name": snapshot.get("split_device_name"),
        "member_name": snapshot.get("member_name"),
        "member_device_type": snapshot.get("member_device_type"),
        "pairing_status": snapshot.get("pairing_status"),
        "device_state": snapshot.get("device_state"),
        "daily_endpoint_type": snapshot.get("endpoint_type"),
        "daily_endpoint_timestamp": snapshot.get("endpoint_timestamp"),
        "split_endpoint_type": snapshot.get("split_endpoint_type"),
        "split_endpoint_timestamp": snapshot.get("split_endpoint_timestamp"),
        "day_key": snapshot.get("day_key"),
        "timezone": snapshot.get("timezone"),
        "source": snapshot.get("source"),
        "split_source": snapshot.get("split_source"),
        "using_stale": bool(
            getattr(coord, "heatpump_daily_consumption_using_stale", False)
        ),
        "last_success_utc": (
            coord.heatpump_daily_consumption_last_success_utc.isoformat()
            if getattr(coord, "heatpump_daily_consumption_last_success_utc", None)
            is not None
            else None
        ),
        "last_error": getattr(coord, "heatpump_daily_consumption_last_error", None),
    }


def _title_case_status(value: object, hass: object | None = None) -> str | None:
    return status_label(value, hass=hass) or friendly_status_text(value)


def _gateway_channel_type_kind(value: object) -> str | None:
    text = _gateway_clean_text(value)
    if not text:
        return None
    normalized = "".join(ch if ch.isalnum() else "_" for ch in text.lower())
    if "production" in normalized or normalized in ("prod", "pv", "solar"):
        return "production"
    if "consumption" in normalized or normalized in ("cons", "load", "site_load"):
        return "consumption"
    return None


_NON_ATTR_CHARS_RE = re.compile(r"[^a-z0-9]+")
_SYSTEM_CONTROLLER_TERMINAL_DESCRIPTIONS: dict[str, str] = {
    "mid": "Microgrid interconnection device line",
    "mid_n": "Microgrid interconnection device neutral",
    "der_l1": "Distributed energy resource line 1",
    "der_l2": "Distributed energy resource line 2",
    "der_l3": "Distributed energy resource line 3",
    "der_n": "Distributed energy resource neutral",
    "nc1": "Load-control relay NC1 (normally closed)",
    "nc2": "Load-control relay NC2 (normally closed)",
    "no1": "Load-control relay NO1 (normally open)",
    "no2": "Load-control relay NO2 (normally open)",
}
_SYSTEM_CONTROLLER_TERMINAL_KEYS: dict[str, str] = {
    "MID": "mid",
    "MID_N": "mid_n",
    "DER_L1": "der_l1",
    "DER_L2": "der_l2",
    "DER_L3": "der_l3",
    "DER_N": "der_n",
    "NC1": "nc1",
    "NC2": "nc2",
    "NO1": "no1",
    "NO2": "no2",
}


def _gateway_attr_key(key: object) -> str | None:
    text = _gateway_clean_text(key)
    if not text:
        return None
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", text)
    normalized = _NON_ATTR_CHARS_RE.sub("_", normalized.lower()).strip("_")
    return normalized or None


def _gateway_flat_member_attributes(
    member: dict[str, object],
    *,
    skip_keys: set[str] | None = None,
) -> dict[str, object]:
    flattened: dict[str, object] = {}
    skip = skip_keys or set()
    for raw_key, raw_value in member.items():
        key = _gateway_attr_key(raw_key)
        if not key or key in skip:
            continue
        if raw_value is None:
            continue
        if isinstance(raw_value, (str, int, float, bool)):
            value = raw_value
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
            flattened[key] = value
    return flattened


def _gateway_terminal_descriptions(
    member: dict[str, object] | None,
) -> dict[str, str]:
    if not isinstance(member, dict):
        return {}
    descriptions: dict[str, str] = {}
    for raw_key, raw_value in member.items():
        key = _gateway_terminal_key(raw_key)
        if key is None:
            continue
        if raw_value is None:
            continue
        if isinstance(raw_value, str) and not raw_value.strip():
            continue
        descriptions[key] = _SYSTEM_CONTROLLER_TERMINAL_DESCRIPTIONS[key]
    return descriptions


def _gateway_terminal_key(raw_key: object) -> str | None:
    text = _gateway_clean_text(raw_key)
    if not text:
        return None
    normalized = re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")
    return _SYSTEM_CONTROLLER_TERMINAL_KEYS.get(normalized)


def _gateway_terminal_values(member: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(member, dict):
        return {}
    values: dict[str, object] = {}
    for raw_key, raw_value in member.items():
        key = _gateway_terminal_key(raw_key)
        if key is None or raw_value is None:
            continue
        if isinstance(raw_value, str):
            value = raw_value.strip()
            if not value:
                continue
            values[key] = value
            continue
        if isinstance(raw_value, (int, float, bool)):
            values[key] = raw_value
    return values


def _heatpump_sg_ready_semantics(status_text: object) -> dict[str, object]:
    text = _gateway_clean_text(status_text)
    if not text:
        return {}
    normalized = text.casefold()
    if normalized in {"recommended", "mode_3", "mode3"}:
        return {
            "sg_ready_mode": 3,
            "sg_ready_contact_state": "closed",
            "status_explanation": "Recommended means the SG Ready contact is closed.",
        }
    if normalized in {"normal", "mode_2", "mode2"}:
        return {
            "sg_ready_mode": 2,
            "sg_ready_contact_state": "open",
            "status_explanation": "Normal means the SG Ready contacts are open.",
        }
    return {}


def _gateway_iq_energy_router_inventory_buckets(
    payload: object,
) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    wrapped = payload.get("value")
    if isinstance(wrapped, dict):
        wrapped_result = wrapped.get("result")
        if isinstance(wrapped_result, list):
            return [item for item in wrapped_result if isinstance(item, dict)]
    return []


def _gateway_iq_energy_router_identity(value: object) -> str | None:
    text = _gateway_clean_text(value)
    if not text:
        return None
    normalized = _NON_ATTR_CHARS_RE.sub("_", text.lower()).strip("_")
    return normalized or None


def _gateway_iq_energy_router_member_key(
    member: dict[str, object],
    *,
    fallback_index: int,
) -> str:
    for key in ("device-uid", "device_uid", "uid"):
        identity = _gateway_iq_energy_router_identity(member.get(key))
        if identity:
            return identity
    name_identity = _gateway_iq_energy_router_identity(member.get("name"))
    if name_identity:
        return f"name_{name_identity}"
    return f"index_{fallback_index}"


def _gateway_iq_energy_router_records(
    coord: EnphaseCoordinator,
) -> list[dict[str, object]]:
    records_getter = coord.inventory_view.gateway_iq_energy_router_summary_records
    if callable(records_getter):
        try:
            records = records_getter()
        except Exception:  # noqa: BLE001
            records = None
        if isinstance(records, list):
            return [dict(record) for record in records if isinstance(record, dict)]
    router_members: list[dict[str, object]] = []
    restored_records = coord.inventory_view.gateway_iq_energy_router_records
    if callable(restored_records):
        try:
            router_members = [
                dict(member)
                for member in restored_records()
                if isinstance(member, dict)
            ]
        except Exception:  # noqa: BLE001
            router_members = []
    grouped_fetch = getattr(coord, "_hems_group_members", None)
    if not router_members and callable(grouped_fetch):
        for member in grouped_fetch("gateway"):
            device_type = _gateway_clean_text(
                member.get("device-type")
                if member.get("device-type") is not None
                else member.get("device_type")
            )
            if (device_type or "").upper() != "IQ_ENERGY_ROUTER":
                continue
            router_members.append(dict(member))
    elif not router_members:
        payload = getattr(coord, "_devices_inventory_payload", None)
        buckets = _gateway_iq_energy_router_inventory_buckets(payload)
        for bucket in buckets:
            raw_type = (
                bucket.get("type")
                if bucket.get("type") is not None
                else (
                    bucket.get("deviceType")
                    if bucket.get("deviceType") is not None
                    else bucket.get("device_type")
                )
            )
            type_key = _gateway_iq_energy_router_identity(raw_type)
            if not type_key:
                continue
            if type_key.replace("_", "") != "hemsdevices":
                continue
            devices = bucket.get("devices")
            if not isinstance(devices, list):
                continue
            for grouped in devices:
                if not isinstance(grouped, dict):
                    continue
                gateways = grouped.get("gateway")
                if not isinstance(gateways, list):
                    continue
                for member in gateways:
                    if not isinstance(member, dict):
                        continue
                    if member_is_retired(member):
                        continue
                    device_type = _gateway_clean_text(
                        member.get("device-type")
                        if member.get("device-type") is not None
                        else member.get("device_type")
                    )
                    if (device_type or "").upper() != "IQ_ENERGY_ROUTER":
                        continue
                    router_members.append(dict(member))

    records: list[dict[str, object]] = []
    key_counts: dict[str, int] = {}
    for member in router_members:
        index = len(records) + 1
        base_key = _gateway_iq_energy_router_member_key(member, fallback_index=index)
        key_counts[base_key] = key_counts.get(base_key, 0) + 1
        key = base_key
        if key_counts[base_key] > 1:
            key = f"{base_key}_{key_counts[base_key]}"
        records.append(
            {
                "key": key,
                "index": index,
                "name": _gateway_clean_text(member.get("name"))
                or f"IQ Energy Router_{index}",
                "member": dict(member),
            }
        )
    return records


def _gateway_iq_energy_router_record(
    coord: EnphaseCoordinator,
    router_key: object,
) -> dict[str, object] | None:
    key = _gateway_clean_text(router_key)
    if not key:
        return None
    record_getter = getattr(coord, "gateway_iq_energy_router_record", None)
    if callable(record_getter):
        try:
            record = record_getter(key)
        except Exception:  # noqa: BLE001
            record = None
        if isinstance(record, dict):
            return record
    for record in _gateway_iq_energy_router_records(coord):
        if _gateway_clean_text(record.get("key")) == key:
            return record
    return None


def _gateway_iq_energy_router_last_reported(
    member: dict[str, object] | None,
) -> datetime | None:
    if not isinstance(member, dict):
        return None
    for key in ("last-report", *list(_GATEWAY_LAST_REPORT_KEYS)):
        parsed = _gateway_parse_timestamp(member.get(key))
        if parsed is not None:
            return parsed
    return None


def _gateway_meter_member(
    coord: EnphaseCoordinator, meter_kind: str
) -> dict[str, object] | None:
    bucket = coord.inventory_view.type_bucket("envoy") or {}
    members = bucket.get("devices")
    dashboard_detail = None
    detail_getter = getattr(coord, "system_dashboard_meter_detail", None)
    if callable(detail_getter):
        dashboard_detail = detail_getter(meter_kind)
    if not isinstance(members, list):
        return dict(dashboard_detail) if isinstance(dashboard_detail, dict) else None
    for member in members:
        if not isinstance(member, dict):
            continue
        kind = _gateway_channel_type_kind(member.get("channel_type"))
        if kind is None:
            name = _gateway_clean_text(member.get("name")) or ""
            if "production" in name.lower():
                kind = "production"
            elif "consumption" in name.lower():
                kind = "consumption"
        if kind == meter_kind:
            merged = dict(member)
            if isinstance(dashboard_detail, dict):
                for key, value in dashboard_detail.items():
                    if value is None:
                        continue
                    if merged.get(key) in (None, "") or key in (
                        "meter_state",
                        "config_type",
                        "meter_type",
                    ):
                        merged[key] = value
            return merged
    return dict(dashboard_detail) if isinstance(dashboard_detail, dict) else None


def _gateway_meter_status_text(
    member: dict[str, object] | None, hass: object | None = None
) -> str | None:
    if not isinstance(member, dict):
        return None
    status_text = _gateway_clean_text(member.get("statusText"))
    if status_text:
        return status_label(status_text, hass=hass) or status_text
    status_raw = _gateway_clean_text(member.get("status"))
    if not status_raw:
        return None
    return status_label(status_raw, hass=hass) or friendly_status_text(status_raw)


def _gateway_meter_last_reported(member: dict[str, object] | None) -> datetime | None:
    if not isinstance(member, dict):
        return None
    for key in _GATEWAY_LAST_REPORT_KEYS:
        parsed = _gateway_parse_timestamp(member.get(key))
        if parsed is not None:
            return parsed
    return None


def _gateway_system_controller_member(
    coord: EnphaseCoordinator,
) -> dict[str, object] | None:
    bucket = coord.inventory_view.type_bucket("envoy") or {}
    members = bucket.get("devices")
    if not isinstance(members, list):
        return None
    for member in members:
        if not isinstance(member, dict):
            continue
        channel_type = (_gateway_clean_text(member.get("channel_type")) or "").lower()
        if channel_type in ("enpower", "system_controller", "systemcontroller"):
            return dict(member)
        name = (_gateway_clean_text(member.get("name")) or "").lower()
        if "system controller" in name:
            return dict(member)
    return None


def _is_dry_contact_type_key(type_key: object) -> bool:
    return is_dry_contact_type_key(type_key)


def _gateway_member_is_dry_contact(member: object) -> bool:
    if not isinstance(member, dict):
        return False
    candidates = (
        member.get("channel_type"),
        member.get("channelType"),
        member.get("meter_type"),
        member.get("device_type"),
        member.get("device-type"),
        member.get("name"),
    )
    for candidate in candidates:
        if _is_dry_contact_type_key(candidate):
            return True
    return False


def _gateway_dry_contact_members(
    coord: EnphaseCoordinator,
) -> list[dict[str, object]]:
    members_out: list[dict[str, object]] = []
    seen_keys: set[str] = set()

    def _identity(member: dict[str, object]) -> str | None:
        device_uid = _gateway_clean_text(
            member.get("device_uid")
            if member.get("device_uid") is not None
            else member.get("device-uid")
        )
        uid = _gateway_clean_text(member.get("uid"))
        contact_id = _gateway_clean_text(
            member.get("contact_id")
            if member.get("contact_id") is not None
            else (
                member.get("contactId")
                if member.get("contactId") is not None
                else member.get("id")
            )
        )
        channel_type = _gateway_clean_text(
            member.get("channel_type")
            if member.get("channel_type") is not None
            else (
                member.get("channelType")
                if member.get("channelType") is not None
                else member.get("meter_type")
            )
        )
        serial_number = _gateway_clean_text(
            member.get("serial_number")
            if member.get("serial_number") is not None
            else (
                member.get("serial")
                if member.get("serial") is not None
                else member.get("serialNumber")
            )
        )

        if device_uid:
            if contact_id or channel_type:
                return "|".join(
                    part
                    for part in (
                        f"device_uid:{device_uid.lower()}",
                        (
                            f"contact_id:{contact_id.lower()}"
                            if contact_id is not None
                            else None
                        ),
                        (
                            f"channel_type:{channel_type.lower()}"
                            if channel_type is not None
                            else None
                        ),
                    )
                    if part is not None
                )
            return f"device_uid:{device_uid.lower()}"
        if uid:
            if contact_id or channel_type:
                return "|".join(
                    part
                    for part in (
                        f"uid:{uid.lower()}",
                        (
                            f"contact_id:{contact_id.lower()}"
                            if contact_id is not None
                            else None
                        ),
                        (
                            f"channel_type:{channel_type.lower()}"
                            if channel_type is not None
                            else None
                        ),
                    )
                    if part is not None
                )
            return f"uid:{uid.lower()}"
        if contact_id and channel_type:
            return (
                f"contact_id:{contact_id.lower()}|channel_type:{channel_type.lower()}"
            )
        if channel_type and serial_number:
            return f"channel_type:{channel_type.lower()}|serial_number:{serial_number.lower()}"
        if contact_id and serial_number:
            return (
                f"contact_id:{contact_id.lower()}|serial_number:{serial_number.lower()}"
            )
        if contact_id:
            return f"contact_id:{contact_id.lower()}"
        if channel_type:
            return f"channel_type:{channel_type.lower()}"
        if serial_number:
            return f"serial_number:{serial_number.lower()}"
        return None

    def _fingerprint(member: dict[str, object]) -> str | None:
        parts: list[tuple[str, str]] = []
        for raw_key in sorted(member):
            key = _gateway_attr_key(raw_key)
            if not key:
                continue
            raw_value = member.get(raw_key)
            if raw_value is None:
                continue
            if not isinstance(raw_value, (str, int, float, bool)):
                continue
            if isinstance(raw_value, str):
                value = raw_value.strip()
                if not value:
                    continue
            else:
                value = str(raw_value)
            parts.append((key, value))
        if not parts:
            return None
        return repr(tuple(parts))

    def _append_member(raw_member: object) -> None:
        if not isinstance(raw_member, dict):
            return
        if member_is_retired(raw_member):
            return
        member = dict(raw_member)
        identity = _identity(member)
        fingerprint = _fingerprint(member)
        key = (
            f"id:{identity}"
            if identity is not None
            else (
                f"fp:{fingerprint}"
                if fingerprint is not None
                else f"idx:{len(members_out)}"
            )
        )
        if key in seen_keys:
            return
        seen_keys.add(key)
        members_out.append(member)

    envoy_bucket = coord.inventory_view.type_bucket("envoy") or {}
    envoy_members = envoy_bucket.get("devices")
    if isinstance(envoy_members, list):
        for member in envoy_members:
            if _gateway_member_is_dry_contact(member):
                _append_member(member)

    buckets = getattr(coord, "_type_device_buckets", None)
    if isinstance(buckets, dict):
        for type_key, bucket in buckets.items():
            if not _is_dry_contact_type_key(type_key):
                continue
            if not isinstance(bucket, dict):
                continue
            bucket_members = bucket.get("devices")
            if not isinstance(bucket_members, list):
                continue
            for member in bucket_members:
                _append_member(member)

    members_out.sort(
        key=lambda member: (
            _identity(member) or "",
            _gateway_clean_text(
                member.get("channel_type")
                if member.get("channel_type") is not None
                else member.get("channelType")
            )
            or "",
            _gateway_clean_text(
                member.get("serial_number")
                if member.get("serial_number") is not None
                else member.get("serial")
            )
            or "",
            _gateway_clean_text(member.get("name")) or "",
        )
    )
    return members_out


class _SiteBaseEntity(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset(
        {
            "last_success_utc",
            "last_failure_utc",
            "backoff_ends_utc",
            "last_failure_response",
        }
    )

    def __init__(
        self,
        coord: EnphaseCoordinator,
        key: str,
        _name: str,
        type_key: str | None = "envoy",
    ):
        super().__init__(coord)
        self._coord = coord
        self._key = key
        self._type_key = type_key
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{key}"

    @property
    def available(self) -> bool:
        if self._type_key is not None and not _type_available(
            self._coord, self._type_key
        ):
            return False
        if self._coord.last_success_utc is not None:
            return True
        return super().available

    def _cloud_diag_attrs(
        self, *, include_last_success: bool = True
    ) -> dict[str, object]:
        attrs: dict[str, object] = {}
        # These attributes are intentionally sanitized by the coordinator
        # before they become visible on diagnostic sensors.
        if include_last_success and self._coord.last_success_utc:
            attrs["last_success_utc"] = self._coord.last_success_utc.isoformat()
        if self._coord.last_failure_utc:
            attrs["last_failure_utc"] = self._coord.last_failure_utc.isoformat()
        if self._coord.last_failure_status is not None:
            attrs["last_failure_status"] = self._coord.last_failure_status
        if self._coord.last_failure_description:
            attrs["code_description"] = self._coord.last_failure_description
        if self._coord.last_failure_response:
            attrs["last_failure_response"] = self._coord.last_failure_response
        if self._coord.last_failure_source:
            attrs["last_failure_source"] = self._coord.last_failure_source
        last_failure_endpoint = getattr(self._coord, "last_failure_endpoint", None)
        if last_failure_endpoint:
            attrs["last_failure_endpoint"] = last_failure_endpoint
        payload_failure_kind = getattr(self._coord, "payload_failure_kind", None)
        if payload_failure_kind:
            attrs["payload_failure_kind"] = payload_failure_kind
        payload_using_stale = bool(getattr(self._coord, "payload_using_stale", False))
        if payload_using_stale:
            attrs["payload_using_stale"] = True
        if self._coord.backoff_ends_utc:
            attrs["backoff_ends_utc"] = self._coord.backoff_ends_utc.isoformat()
        return attrs

    def _backoff_remaining_seconds(self) -> int | None:
        ends = self._coord.backoff_ends_utc
        if ends is None:
            return None
        try:
            remaining = (ends - dt_util.utcnow()).total_seconds()
        except Exception:
            return None
        if remaining <= 0:
            return 0
        rounded = int(round(remaining))
        if rounded <= 0:
            return 1
        return rounded

    @property
    def extra_state_attributes(self):
        return self._cloud_diag_attrs()

    @property
    def device_info(self):
        if self._type_key is None:
            return _cloud_device_info(self._coord.site_id)
        info = _type_device_info(self._coord, self._type_key)
        if info is not None:
            return info
        from homeassistant.helpers.entity import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:{self._type_key}")},
            manufacturer="Enphase",
        )


class EnphaseSiteEnergySensor(_SiteBaseEntity, RestoreSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coord: EnphaseCoordinator,
        flow_key: str,
        translation_key: str,
        name: str,
    ) -> None:
        super().__init__(coord, flow_key, name)
        self._flow_key = flow_key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{flow_key}"
        self._restored_value: float | None = None
        self._restored_reset_at: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None:
            try:
                restored = (
                    float(last.native_value) if last.native_value is not None else None
                )
            except Exception:  # noqa: BLE001
                restored = None
            if restored is not None and restored >= 0:
                self._restored_value = restored
                self._attr_native_value = restored
        try:
            last_state = await self.async_get_last_state()
        except Exception:  # noqa: BLE001
            last_state = None
        if last_state is not None:
            reset_attr = (last_state.attributes or {}).get("last_reset_at")
            if isinstance(reset_attr, str):
                self._restored_reset_at = reset_attr

    def _flow_data(self) -> dict[str, object]:
        energy = getattr(self._coord, "energy", None)
        flows = (
            getattr(energy, "site_energy", None)
            if energy is not None
            else getattr(self._coord, "site_energy", None)
        ) or {}
        entry = flows.get(self._flow_key)
        if isinstance(entry, SiteEnergyFlow):
            return {
                "value_kwh": entry.value_kwh,
                "bucket_count": entry.bucket_count,
                "fields_used": entry.fields_used,
                "start_date": entry.start_date,
                "last_report_date": entry.last_report_date,
                "update_pending": entry.update_pending,
                "source_unit": entry.source_unit,
                "last_reset_at": entry.last_reset_at,
                "interval_minutes": entry.interval_minutes,
            }
        if isinstance(entry, dict):
            return entry
        return {}

    def _current_value(self) -> float | None:
        data = self._flow_data()
        val = data.get("value_kwh")
        if val is None:
            return None
        try:
            return float(val)
        except Exception:  # noqa: BLE001
            return None

    @property
    def available(self) -> bool:
        if self._coord.last_success_utc is None and not bool(
            getattr(self._coord, "last_update_success", False)
        ):
            return False
        if self._current_value() is not None:
            return True
        return self._restored_value is not None

    @property
    def device_info(self):
        heatpump_available = self._flow_key == "heat_pump" and _has_type(
            self._coord, "heatpump"
        )
        if self._flow_key == "heat_pump" and heatpump_available:
            heatpump_info = _type_device_info(self._coord, "heatpump")
            if heatpump_info is not None:
                return heatpump_info
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)

    @property
    def native_value(self):
        current = self._current_value()
        if current is not None:
            return round(current, 2)
        if self._restored_value is None:
            return None
        return round(self._restored_value, 2)

    @property
    def extra_state_attributes(self):
        data = self._flow_data()
        attrs: dict[str, object] = {}
        last_report_raw = data.get("last_report_date")
        parsed_sample_ts = _EnphaseSiteLifetimePowerSensor._parse_sample_timestamp(
            last_report_raw
        )
        if parsed_sample_ts is not None:
            attrs["sampled_at_utc"] = datetime.fromtimestamp(
                parsed_sample_ts, tz=timezone.utc
            ).isoformat()

        reset_at = data.get("last_reset_at") or self._restored_reset_at
        if reset_at:
            attrs["last_reset_at"] = reset_at

        if self._flow_key != "heat_pump":
            return attrs

        heatpump_power = getattr(self._coord, "heatpump_power_w", None)
        if heatpump_power is not None:
            try:
                attrs["heat_pump_power_w"] = round(float(heatpump_power), 3)
            except Exception:  # noqa: BLE001
                attrs["heat_pump_power_w"] = None

        daily = getattr(self._coord, "heatpump_daily_consumption", None)
        if not isinstance(daily, dict):
            return attrs

        for key in (
            "daily_energy_wh",
            "daily_solar_wh",
            "daily_battery_wh",
            "daily_grid_wh",
            "device_uid",
            "device_name",
            "member_name",
            "member_device_type",
            "pairing_status",
            "device_state",
            "endpoint_type",
            "endpoint_timestamp",
            "day_key",
            "timezone",
            "source",
        ):
            if key not in daily:
                continue
            attr_key = {
                "device_uid": "daily_device_uid",
                "device_name": "daily_device_name",
                "member_name": "daily_member_name",
                "member_device_type": "daily_member_device_type",
                "pairing_status": "daily_pairing_status",
                "device_state": "daily_device_state",
                "endpoint_type": "daily_endpoint_type",
                "endpoint_timestamp": "daily_endpoint_timestamp",
                "source": "daily_source",
            }.get(key, key)
            attrs[attr_key] = daily.get(key)

        return attrs


class _EnphaseSiteLifetimePowerSensor(_SiteBaseEntity, RestoreEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes | frozenset(
        {
            "last_flow_kwh",
            "last_energy_ts",
            "last_sample_ts",
            "last_power_w",
            "last_window_seconds",
            "last_report_date",
            "last_reset_at",
            "method",
            "source_flows",
        }
    )

    _DEFAULT_WINDOW_S = 300.0
    _MIN_DELTA_KWH = 0.0005
    _RESET_DROP_KWH = 0.25
    _OUTLIER_MIN_POWER_W = 100_000
    _OUTLIER_MIN_DELTA_KWH = 5.0
    _OUTLIER_RELATIVE_FLOW_RATIO = 0.15

    def __init__(
        self,
        coord: EnphaseCoordinator,
        key: str,
        name: str,
        *,
        translation_key: str,
        flow_signs: dict[str, int],
        type_key: str | None = None,
    ) -> None:
        super().__init__(coord, key, name, type_key=type_key)
        self._attr_translation_key = translation_key
        self._flow_signs = dict(flow_signs)
        self._last_flow_kwh: dict[str, float] = {}
        self._last_energy_ts: float | None = None
        self._last_sample_ts: float | None = None
        self._last_power_w: int = 0
        self._last_window_s: float | None = None
        self._last_method: str = "seeded"
        self._last_reset_at: float | None = None
        self._last_report_date_iso: str | None = None
        self._restored_power_w: int | None = None
        self._synthetic_zero_flows: set[str] = set()
        self._live_flow_sample_count: int = 0
        self._previous_live_flow_kwh: dict[str, float] = {}
        self._previous_live_energy_ts: float | None = None
        self._previous_live_sample_ts: float | None = None
        self._last_live_interval_minutes: float | None = None
        self._restored_method_explicit = False

    def _clear_restored_live_history(self, *, discard_power: bool = False) -> None:
        """Drop restored live-history samples that are not safe to reuse."""

        self._previous_live_flow_kwh = {}
        self._previous_live_energy_ts = None
        self._previous_live_sample_ts = None
        if discard_power:
            self._restored_power_w = None

    def _discard_restored_baseline(self) -> None:
        """Drop a restored baseline sample that is not safe to reuse."""

        self._last_flow_kwh = {}
        self._last_energy_ts = None
        self._last_sample_ts = None
        self._last_window_s = None
        self._last_method = "seeded"

    def _restored_flows_zeroed(self, flows: dict[str, float]) -> bool:
        """Return True when every restored flow is effectively zero."""

        return bool(flows) and all(
            abs(value) <= self._MIN_DELTA_KWH for value in flows.values()
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        last_extra = await self.async_get_last_extra_data()
        extra_data = _SiteLifetimePowerRestoreData.from_dict(
            last_extra.as_dict() if last_extra is not None else None
        )
        if not last_state:
            return
        attrs = last_state.attributes or {}
        raw_last_flow_kwh = attrs.get("last_flow_kwh")
        if isinstance(raw_last_flow_kwh, dict):
            restored_flows: dict[str, float] = {}
            for flow_key in self._flow_signs:
                raw_value = raw_last_flow_kwh.get(flow_key)
                try:
                    if raw_value is not None:
                        restored_flows[flow_key] = float(raw_value)
                except Exception:
                    continue
            self._last_flow_kwh = restored_flows
        self._last_energy_ts = _restore_optional_float_attribute(
            attrs, "last_energy_ts"
        )
        self._last_sample_ts = _restore_optional_float_attribute(
            attrs, "last_sample_ts"
        )
        restored_power = _restore_optional_int_value(last_state.state)
        if restored_power is None:
            self._restored_power_w = None
        else:
            self._last_power_w = restored_power
            self._restored_power_w = restored_power
            self._last_power_w = 0
        attr_power = _restore_optional_int_value(attrs.get("last_power_w"))
        if attr_power is not None:
            self._restored_power_w = attr_power
        self._last_window_s = _restore_optional_float_attribute(
            attrs, "last_window_seconds"
        )
        self._last_reset_at = _restore_optional_float_attribute(attrs, "last_reset_at")
        last_method = attrs.get("method")
        if isinstance(last_method, str) and last_method.strip():
            self._last_method = last_method
            self._restored_method_explicit = True
        last_report_date = attrs.get("last_report_date")
        if isinstance(last_report_date, str) and last_report_date.strip():
            self._last_report_date_iso = last_report_date
        self._previous_live_flow_kwh = {
            flow_key: value
            for flow_key, value in extra_data.previous_live_flow_kwh.items()
            if flow_key in self._flow_signs
        }
        self._previous_live_energy_ts = extra_data.previous_live_energy_ts
        self._previous_live_sample_ts = extra_data.previous_live_sample_ts
        self._last_live_interval_minutes = extra_data.last_live_interval_minutes
        self._restore_live_history()

    def _restore_live_history(self) -> None:
        """Restore a valid two-sample live history when available."""

        restored_baseline_zeroed = self._restored_flows_zeroed(self._last_flow_kwh)
        restored_previous_zeroed = self._restored_flows_zeroed(
            self._previous_live_flow_kwh
        )

        if self._restored_method_explicit and self._last_method in {
            "seeded",
            "no_live_data",
        }:
            self._clear_restored_live_history(discard_power=True)
            return

        if restored_baseline_zeroed and not self._restored_method_explicit:
            self._clear_restored_live_history(discard_power=True)
            return

        if (
            self._restored_method_explicit
            and self._last_method in {"lifetime_reset", "restored_lifetime_reset"}
            and restored_baseline_zeroed
        ):
            self._clear_restored_live_history(discard_power=True)
            return

        if (
            restored_previous_zeroed
            and not restored_baseline_zeroed
            and any(
                value > self._RESET_DROP_KWH for value in self._last_flow_kwh.values()
            )
        ):
            self._clear_restored_live_history(discard_power=True)
            return

        if (
            not self._last_flow_kwh
            or not self._previous_live_flow_kwh
            or self._last_sample_ts is None
            or self._previous_live_sample_ts is None
            or self._previous_live_sample_ts >= self._last_sample_ts
        ):
            self._clear_restored_live_history(discard_power=True)
            return

        reset_detected = False
        signed_delta_kwh = 0.0
        for flow_key, sign in self._flow_signs.items():
            current = self._last_flow_kwh.get(flow_key)
            previous = self._previous_live_flow_kwh.get(flow_key)
            if current is None or previous is None:
                continue
            delta, flow_reset = _lifetime_energy_delta(
                current_kwh=current,
                previous_kwh=previous,
                reset_drop_kwh=self._RESET_DROP_KWH,
            )
            if flow_reset:
                reset_detected = True
                break
            if delta is not None:
                signed_delta_kwh += delta * sign

        if reset_detected:
            self._last_power_w = 0
            self._last_method = "restored_lifetime_reset"
            self._last_reset_at = self._last_sample_ts
        else:
            window_s = _resolve_lifetime_power_window(
                sample_ts=self._last_sample_ts,
                previous_energy_ts=(
                    self._previous_live_energy_ts or self._previous_live_sample_ts
                ),
                default_window_s=self._DEFAULT_WINDOW_S,
            )
            window_s = max(window_s, self._DEFAULT_WINDOW_S)
            if self._last_live_interval_minutes is not None:
                window_s = max(window_s, self._last_live_interval_minutes * 60.0)
            self._last_window_s = window_s
            if abs(signed_delta_kwh) <= self._MIN_DELTA_KWH:
                self._last_power_w = 0
                self._last_method = "restored_no_change"
            else:
                restored_power_w = _energy_delta_to_power_w(
                    signed_delta_kwh,
                    window_s=window_s,
                )
                if not self._power_sample_is_plausible(
                    power_w=restored_power_w,
                    signed_delta_kwh=signed_delta_kwh,
                    current_values=self._last_flow_kwh,
                    previous_values=self._previous_live_flow_kwh,
                ):
                    self._clear_restored_live_history(discard_power=True)
                    self._discard_restored_baseline()
                    return
                self._last_power_w = restored_power_w
                self._last_method = "restored_lifetime_energy_window"

        self._restored_power_w = self._last_power_w
        self._live_flow_sample_count = 2

    @staticmethod
    def _coerce_flow_value(entry: object) -> float | None:
        value = None
        if isinstance(entry, SiteEnergyFlow):
            value = entry.value_kwh
        elif isinstance(entry, dict):
            value = entry.get("value_kwh")
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric < 0:
            return None
        return round(numeric, 3)

    @staticmethod
    def _parse_sample_timestamp(raw: object) -> float | None:
        if raw is None:
            return None
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                return raw.replace(tzinfo=timezone.utc).timestamp()
            return raw.astimezone(timezone.utc).timestamp()
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value > 10**12:
                value = value / 1000.0
            return value if value > 0 else None
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return None
            if stripped.isdigit():
                return _EnphaseSiteLifetimePowerSensor._parse_sample_timestamp(
                    int(stripped)
                )
            normalized = stripped.replace("[UTC]", "").replace("Z", "+00:00")
            try:
                dt_obj = datetime.fromisoformat(normalized)
            except ValueError:
                dt_obj = dt_util.parse_datetime(stripped)
            if dt_obj is None:
                try:
                    date_obj = dt_util.parse_date(stripped)
                except Exception:
                    date_obj = None
                if date_obj is None:
                    return None
                dt_obj = datetime.combine(
                    date_obj, datetime.min.time(), tzinfo=timezone.utc
                )
            elif dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
            return dt_obj.astimezone(timezone.utc).timestamp()
        return None

    def _site_energy_flows(self) -> dict[str, object]:
        energy = getattr(self._coord, "energy", None)
        flows = (
            getattr(energy, "site_energy", None)
            if energy is not None
            else getattr(self._coord, "site_energy", None)
        )
        return flows if isinstance(flows, dict) else {}

    def _site_energy_meta(self) -> dict[str, object]:
        energy = getattr(self._coord, "energy", None)
        meta = (
            getattr(energy, "site_energy_meta", None)
            if energy is not None
            else getattr(self._coord, "site_energy_meta", None)
        )
        return meta if isinstance(meta, dict) else {}

    @classmethod
    def _power_sample_is_plausible(
        self,
        *,
        power_w: int,
        signed_delta_kwh: float,
        current_values: dict[str, float],
        previous_values: dict[str, float],
    ) -> bool:
        """Reject obviously corrupt cumulative-delta samples without capping large sites."""

        abs_power_w = abs(power_w)
        abs_delta_kwh = abs(signed_delta_kwh)
        if (
            abs_power_w < self._OUTLIER_MIN_POWER_W
            or abs_delta_kwh < self._OUTLIER_MIN_DELTA_KWH
        ):
            return True

        flow_scale_kwh = 0.0
        for value in (*current_values.values(), *previous_values.values()):
            try:
                numeric = abs(float(value))
            except Exception:
                continue
            flow_scale_kwh = max(flow_scale_kwh, numeric)

        if flow_scale_kwh <= 0:
            return True

        return abs_delta_kwh < (flow_scale_kwh * self._OUTLIER_RELATIVE_FLOW_RATIO)

    def _flow_supported(self, flow_key: str) -> bool:
        if flow_key in self._site_energy_flows():
            return True

        known_channel = getattr(
            getattr(self._coord, "discovery_snapshot", None),
            "site_energy_channel_known",
            None,
        )
        if callable(known_channel):
            try:
                if known_channel(flow_key):
                    return True
            except Exception:  # noqa: BLE001
                pass

        bucket_lengths = self._site_energy_meta().get("bucket_lengths")
        if not isinstance(bucket_lengths, dict):
            return False
        for bucket_key in SITE_LIFETIME_FLOW_BUCKET_LENGTH_KEYS.get(
            flow_key, (flow_key,)
        ):
            bucket_length = bucket_lengths.get(bucket_key)
            try:
                if int(bucket_length) > 0:
                    return True
            except (TypeError, ValueError):
                if bucket_length:
                    return True
        return False

    def _current_flow_values(self) -> tuple[dict[str, float], set[str]]:
        flows = self._site_energy_flows()
        values: dict[str, float] = {}
        synthetic_zero_flows: set[str] = set()
        for flow_key in self._flow_signs:
            current = self._coerce_flow_value(flows.get(flow_key))
            if current is not None:
                values[flow_key] = current
            elif self._flow_supported(flow_key):
                values[flow_key] = 0.0
                synthetic_zero_flows.add(flow_key)
        return values, synthetic_zero_flows

    @staticmethod
    def _has_live_flow_values(
        current_values: dict[str, float], synthetic_zero_flows: set[str]
    ) -> bool:
        """Return True when at least one flow has a live reading."""

        return any(flow_key not in synthetic_zero_flows for flow_key in current_values)

    def _sample_timestamp(self, flows: dict[str, object]) -> tuple[float, str | None]:
        for flow_key in self._flow_signs:
            entry = flows.get(flow_key)
            raw_report_date = None
            if isinstance(entry, SiteEnergyFlow):
                raw_report_date = entry.last_report_date
            elif isinstance(entry, dict):
                raw_report_date = entry.get("last_report_date")
            parsed = self._parse_sample_timestamp(raw_report_date)
            if parsed is not None:
                iso = datetime.fromtimestamp(parsed, tz=timezone.utc).isoformat()
                return parsed, iso

        meta_report_date = self._site_energy_meta().get("last_report_date")
        parsed = self._parse_sample_timestamp(meta_report_date)
        if parsed is not None:
            iso = datetime.fromtimestamp(parsed, tz=timezone.utc).isoformat()
            return parsed, iso

        last_success_utc = getattr(self._coord, "last_success_utc", None)
        parsed = self._parse_sample_timestamp(last_success_utc)
        if parsed is not None:
            iso = datetime.fromtimestamp(parsed, tz=timezone.utc).isoformat()
            return parsed, iso

        now = dt_util.utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp(), now.isoformat()

    @staticmethod
    def _coerce_interval_minutes(raw: object) -> float | None:
        try:
            interval_minutes = float(raw)
        except Exception:
            return None
        return interval_minutes if interval_minutes > 0 else None

    def _minimum_window_seconds(
        self,
        flows: dict[str, object],
        current_values: dict[str, float],
    ) -> float | None:
        interval_minutes_values: list[float] = []

        for flow_key in self._flow_signs:
            if flow_key not in current_values:
                continue
            entry = flows.get(flow_key)
            raw_interval = None
            if isinstance(entry, SiteEnergyFlow):
                raw_interval = entry.interval_minutes
            elif isinstance(entry, dict):
                raw_interval = entry.get("interval_minutes")
            interval_minutes = self._coerce_interval_minutes(raw_interval)
            if interval_minutes is not None:
                interval_minutes_values.append(interval_minutes)

        meta_interval_minutes = self._coerce_interval_minutes(
            self._site_energy_meta().get("interval_minutes")
        )
        if meta_interval_minutes is not None:
            interval_minutes_values.append(meta_interval_minutes)

        if not interval_minutes_values:
            return None
        return max(interval_minutes_values) * 60.0

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        current_values, _synthetic_zero_flows = self._current_flow_values()
        return bool(current_values)

    @property
    def native_value(self):
        flows = self._site_energy_flows()
        current_values, synthetic_zero_flows = self._current_flow_values()
        has_live_flow_values = self._has_live_flow_values(
            current_values, synthetic_zero_flows
        )

        sample_ts, sample_iso = self._sample_timestamp(flows)
        self._last_report_date_iso = sample_iso
        if self._last_sample_ts is not None and sample_ts == self._last_sample_ts:
            if self._live_flow_sample_count >= 2:
                return self._last_power_w
            return None

        if self._last_flow_kwh:
            for flow_key in self._flow_signs:
                if flow_key in current_values or flow_key not in self._last_flow_kwh:
                    continue
                current_values[flow_key] = 0.0
                synthetic_zero_flows.add(flow_key)

        self._synthetic_zero_flows = synthetic_zero_flows
        if not current_values:
            return None
        if not has_live_flow_values:
            if self._last_flow_kwh:
                self._last_flow_kwh.update(current_values)
                self._last_energy_ts = sample_ts
                self._last_sample_ts = sample_ts
                self._last_power_w = 0
                self._last_method = "no_live_data"
                self._last_window_s = None
            else:
                self._last_sample_ts = sample_ts
                self._last_power_w = 0
                self._last_method = "seeded"
                self._last_window_s = None
            return 0

        if self._live_flow_sample_count == 0:
            self._previous_live_flow_kwh = {}
            self._previous_live_energy_ts = None
            self._previous_live_sample_ts = None
            self._last_flow_kwh = dict(current_values)
            self._last_energy_ts = sample_ts
            self._last_sample_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "seeded"
            self._last_window_s = None
            self._live_flow_sample_count = 1
            return None

        if not self._last_flow_kwh:
            self._previous_live_flow_kwh = {}
            self._previous_live_energy_ts = None
            self._previous_live_sample_ts = None
            self._last_flow_kwh = dict(current_values)
            self._last_energy_ts = sample_ts
            self._last_sample_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "seeded"
            self._last_window_s = None
            return None

        prior_last_power_w = self._last_power_w
        prior_live_sample_count = self._live_flow_sample_count

        reset_detected = False
        signed_delta_kwh = 0.0
        previous_live_flow_kwh = dict(self._last_flow_kwh)
        previous_live_energy_ts = self._last_energy_ts
        previous_live_sample_ts = self._last_sample_ts
        for flow_key, sign in self._flow_signs.items():
            current = current_values.get(flow_key)
            if current is None:
                continue
            previous = self._last_flow_kwh.get(flow_key)
            if previous is None:
                continue
            if flow_key in synthetic_zero_flows and current <= 0 and previous > 0:
                continue
            delta, flow_reset = _lifetime_energy_delta(
                current_kwh=current,
                previous_kwh=previous,
                reset_drop_kwh=self._RESET_DROP_KWH,
            )
            if flow_reset:
                reset_detected = True
                break
            if delta is not None:
                signed_delta_kwh += delta * sign

        self._previous_live_flow_kwh = previous_live_flow_kwh
        self._previous_live_energy_ts = previous_live_energy_ts
        self._previous_live_sample_ts = previous_live_sample_ts
        self._last_flow_kwh = dict(current_values)
        self._last_sample_ts = sample_ts

        if reset_detected:
            self._last_energy_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "lifetime_reset"
            self._last_window_s = None
            self._last_reset_at = sample_ts
            return 0

        window_s = _resolve_lifetime_power_window(
            sample_ts=sample_ts,
            previous_energy_ts=self._last_energy_ts,
            default_window_s=self._DEFAULT_WINDOW_S,
        )
        minimum_window_s = self._minimum_window_seconds(flows, current_values)
        self._last_live_interval_minutes = (
            minimum_window_s / 60.0 if minimum_window_s is not None else None
        )
        if minimum_window_s is not None and window_s < minimum_window_s:
            window_s = minimum_window_s
        self._last_energy_ts = sample_ts
        self._last_window_s = window_s
        self._live_flow_sample_count += 1

        if abs(signed_delta_kwh) <= self._MIN_DELTA_KWH:
            self._last_power_w = 0
            self._last_method = "no_change"
            return 0

        candidate_power_w = _energy_delta_to_power_w(
            signed_delta_kwh,
            window_s=window_s,
        )
        if not self._power_sample_is_plausible(
            power_w=candidate_power_w,
            signed_delta_kwh=signed_delta_kwh,
            current_values=current_values,
            previous_values=previous_live_flow_kwh,
        ):
            self._last_power_w = prior_last_power_w
            self._last_method = "outlier_ignored"
            return prior_last_power_w if prior_live_sample_count >= 2 else None
        self._last_power_w = candidate_power_w
        self._last_method = "lifetime_energy_window"
        return self._last_power_w

    @property
    def extra_state_attributes(self):
        return {
            "sampled_at_utc": self._last_report_date_iso,
            "last_flow_kwh": dict(self._last_flow_kwh),
            "last_energy_ts": self._last_energy_ts,
            "last_sample_ts": self._last_sample_ts,
            "last_power_w": self._last_power_w,
            "last_window_seconds": self._last_window_s,
            "last_reset_at": self._last_reset_at,
            "method": self._last_method,
            "source_flows": list(self._flow_signs),
        }

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        return _SiteLifetimePowerRestoreData(
            previous_live_flow_kwh=dict(self._previous_live_flow_kwh),
            previous_live_energy_ts=self._previous_live_energy_ts,
            previous_live_sample_ts=self._previous_live_sample_ts,
            last_live_interval_minutes=self._last_live_interval_minutes,
        )


class EnphaseGridPowerSensor(_EnphaseSiteLifetimePowerSensor):
    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "grid_power",
            "Current Grid Power",
            translation_key="site_grid_power",
            flow_signs={"grid_import": 1, "grid_export": -1},
            type_key=None,
        )


class EnphaseBatteryPowerSensor(_EnphaseSiteLifetimePowerSensor):
    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "battery_power",
            "Current Battery Power",
            translation_key="site_battery_power",
            flow_signs={"battery_discharge": 1, "battery_charge": -1},
            type_key=None,
        )


class EnphaseSiteLastUpdateSensor(_SiteBaseEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_successful_update"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "last_update", "Last Successful Update", type_key=None)

    @property
    def native_value(self):
        return self._coord.last_success_utc

    @property
    def extra_state_attributes(self):
        return self._cloud_diag_attrs(include_last_success=False)

    @property
    def device_info(self):
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)  # pragma: no cover


class EnphaseCloudLatencySensor(_SiteBaseEntity):
    _attr_translation_key = "cloud_latency"
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "latency_ms", "Cloud Latency", type_key=None)

    @property
    def native_value(self):
        return self._coord.latency_ms

    @property
    def extra_state_attributes(self):
        return {}

    @property
    def device_info(self):
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)  # pragma: no cover


class EnphaseCurrentPowerConsumptionSensor(_SiteBaseEntity, RestoreSensor):
    _attr_translation_key = "current_production_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "current_production_power",
            "Current Production Power",
            type_key=None,
        )
        self._last_good_value: float | None = None
        self._last_good_sample_utc: datetime | None = None
        self._last_good_cached_at_utc: datetime | None = None
        self._last_good_source: str | None = None
        self._last_good_reported_units: str | None = None
        self._last_good_reported_precision: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None:
            try:
                restored = (
                    float(last.native_value) if last.native_value is not None else None
                )
            except Exception:  # noqa: BLE001
                restored = None
            if restored is not None and math.isfinite(restored):
                self._last_good_value = restored

        try:
            last_state = await self.async_get_last_state()
        except Exception:  # noqa: BLE001
            last_state = None
        if last_state is None:
            return
        attrs = last_state.attributes or {}
        sample_raw = attrs.get("sampled_at_utc")
        if isinstance(sample_raw, str):
            parsed = dt_util.parse_datetime(sample_raw)
            if parsed is not None:
                self._last_good_sample_utc = _normalize_utc_datetime(parsed)
        cached_raw = attrs.get("cached_at_utc")
        if isinstance(cached_raw, str):
            parsed_cached = dt_util.parse_datetime(cached_raw)
            if parsed_cached is not None:
                self._last_good_cached_at_utc = _normalize_utc_datetime(parsed_cached)
        source = attrs.get("source")
        if isinstance(source, str) and source.strip():
            self._last_good_source = source
        units = attrs.get("reported_units")
        if isinstance(units, str) and units.strip():
            self._last_good_reported_units = units
        precision = attrs.get("reported_precision")
        try:
            if precision is not None:
                self._last_good_reported_precision = int(precision)
        except Exception:  # noqa: BLE001
            self._last_good_reported_precision = None

    def _cache_ttl(self) -> timedelta:
        interval = getattr(self._coord, "update_interval", None)
        if isinstance(interval, timedelta) and interval.total_seconds() > 0:
            return interval * CURRENT_POWER_CACHE_TTL_MULTIPLIER
        return timedelta(minutes=CURRENT_POWER_CACHE_TTL_MULTIPLIER)

    def _freshness_reference_utc(self) -> datetime:
        success_utc = _normalize_utc_datetime(
            getattr(self._coord, "last_success_utc", None)
        )
        try:
            now = _normalize_utc_datetime(dt_util.utcnow())
        except Exception:  # noqa: BLE001
            now = None
        if success_utc is not None and now is not None:
            return max(success_utc, now)
        if success_utc is not None:
            return success_utc
        if now is not None:
            return now
        return datetime.now(timezone.utc)

    def _cached_sample_is_fresh(self) -> bool:
        sample_utc = self._last_good_cached_at_utc or self._last_good_sample_utc
        if sample_utc is None:
            return False
        reference_utc = self._freshness_reference_utc()
        return reference_utc - sample_utc <= self._cache_ttl()

    def _clear_last_good_sample(self) -> None:
        self._last_good_value = None
        self._last_good_sample_utc = None
        self._last_good_cached_at_utc = None
        self._last_good_source = None
        self._last_good_reported_units = None
        self._last_good_reported_precision = None

    def _current_or_cached_snapshot(
        self,
    ) -> tuple[float | None, datetime | None, str | None, str | None, int | None]:
        value = self._coord.current_power_consumption_w
        sample_utc = self._coord.current_power_consumption_sample_utc
        source = self._coord.current_power_consumption_source
        units = self._coord.current_power_consumption_reported_units
        precision = self._coord.current_power_consumption_reported_precision

        if value is not None:
            self._last_good_value = float(value)
            self._last_good_sample_utc = _normalize_utc_datetime(sample_utc)
            self._last_good_cached_at_utc = (
                self._last_good_sample_utc
                or _normalize_utc_datetime(
                    getattr(self._coord, "last_success_utc", None)
                )
                or self._freshness_reference_utc()
            )
            self._last_good_source = source
            self._last_good_reported_units = units
            self._last_good_reported_precision = precision
            return (
                float(value),
                self._last_good_sample_utc,
                source,
                units,
                precision,
            )

        if self._last_good_value is not None and not self._cached_sample_is_fresh():
            self._clear_last_good_sample()

        return (
            self._last_good_value,
            self._last_good_sample_utc,
            self._last_good_source,
            self._last_good_reported_units,
            self._last_good_reported_precision,
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        value, _sample_utc, _source, _units, _precision = (
            self._current_or_cached_snapshot()
        )
        return value is not None

    @property
    def native_value(self):
        value, _sample_utc, _source, _units, _precision = (
            self._current_or_cached_snapshot()
        )
        if value is None:
            return None
        rounded = round(value, 3)
        if float(rounded).is_integer():
            return int(rounded)
        return rounded

    @property
    def extra_state_attributes(self):
        _value, sample_utc, source, units, precision = (
            self._current_or_cached_snapshot()
        )
        return {
            "sampled_at_utc": (
                sample_utc.isoformat() if sample_utc is not None else None
            ),
            "cached_at_utc": (
                self._last_good_cached_at_utc.isoformat()
                if self._last_good_cached_at_utc is not None
                else None
            ),
            "source": source,
            "reported_units": units,
            "reported_precision": precision,
        }

    @property
    def device_info(self):
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)


class EnphaseSiteLastErrorCodeSensor(_SiteBaseEntity):
    _attr_translation_key = "cloud_error_code"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(CLOUD_ERROR_CODE_STATES)

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "last_error_code", "Cloud Error Code", type_key=None)

    def _auth_block_is_active(self) -> bool:
        """Return True when auth is currently blocked without mutating coordinator state."""

        if getattr(self._coord, "_last_error", None) == "auth_blocked":
            return True
        blocked_until = getattr(self._coord, "_auth_blocked_until_utc", None)
        if isinstance(blocked_until, datetime):
            return blocked_until > dt_util.utcnow()
        return False

    @property
    def native_value(self):
        failure_ts = self._coord.last_failure_utc
        success_ts = self._coord.last_success_utc
        failure_active = bool(
            failure_ts and (success_ts is None or failure_ts > success_ts)
        )
        if not failure_active:
            return STATE_NONE
        failure_source = getattr(self._coord, "last_failure_source", None)
        if (
            failure_source == "payload"
            or getattr(self._coord, "payload_failure_kind", None) is not None
        ):
            return "invalid_payload"
        if failure_source == "auth" and self._auth_block_is_active():
            return "auth_blocked"
        code = getattr(self._coord, "last_failure_status", None)
        if code is None:
            if failure_source == "auth":
                return "authentication_error"
            description = (
                getattr(self._coord, "last_failure_description", None) or ""
            ).lower()
            if failure_source == "network":
                dns_tokens = (
                    "dns",
                    "name or service not known",
                    "temporary failure in name resolution",
                    "resolv",
                )
                if any(token in description for token in dns_tokens):
                    return "dns_error"
                return "network_error"
            return STATE_NONE
        try:
            status = int(code)
        except (TypeError, ValueError):
            return "request_error"
        if status == 429:
            return "rate_limited"
        if status in (401, 403):
            return "authentication_error"
        if 500 <= status < 600:
            return "service_unavailable"
        return "request_error"

    @property
    def extra_state_attributes(self):
        return {}

    @property
    def device_info(self):
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)


class EnphaseSiteServiceStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "site_service_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(SITE_SERVICE_STATUS_STATES)
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes | frozenset(
        {
            "degraded_services",
            "degraded_endpoint_families",
        }
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "service_status",
            "Service Status",
            type_key=None,
        )
        self._metrics_snapshot: dict[str, object] | None = self._read_metrics()

    def _read_metrics(self) -> dict[str, object] | None:
        collect_site_metrics = getattr(self._coord, "collect_site_metrics", None)
        if not callable(collect_site_metrics):
            return None
        try:
            metrics = collect_site_metrics()
        except Exception:  # noqa: BLE001
            return None
        return metrics if isinstance(metrics, dict) else None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._metrics_snapshot = self._read_metrics()
        super()._handle_coordinator_update()

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        return sorted(
            text for item in value if (text := _gateway_clean_text(item)) is not None
        )

    @property
    def native_value(self):
        metrics = self._metrics_snapshot
        if metrics is None:
            return "unknown"
        degraded_services = self._string_list(metrics.get("degraded_services"))
        degraded_endpoint_families = self._string_list(
            metrics.get("degraded_endpoint_families")
        )
        if degraded_services or degraded_endpoint_families:
            return "degraded"
        return "ok"

    @property
    def icon(self):
        if self.native_value == "degraded":
            return "mdi:cloud-alert"
        if self.native_value == "unknown":
            return "mdi:cloud-question"
        return "mdi:cloud-check"

    @property
    def extra_state_attributes(self):
        metrics = self._metrics_snapshot
        metrics_available = metrics is not None
        if metrics is None:
            metrics = {}
        degraded_services = self._string_list(metrics.get("degraded_services"))
        degraded_endpoint_families = self._string_list(
            metrics.get("degraded_endpoint_families")
        )
        attrs: dict[str, object] = {
            "degraded_services": degraded_services,
            "degraded_endpoint_families": degraded_endpoint_families,
            "degraded_service_count": len(degraded_services),
            "degraded_endpoint_family_count": len(degraded_endpoint_families),
            "metrics_available": metrics_available,
        }
        attrs.update(self._cloud_diag_attrs())
        return attrs

    @property
    def device_info(self):
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)


class EnphaseSiteBackoffEndsSensor(_SiteBaseEntity):
    _attr_translation_key = "cloud_backoff_ends"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "backoff_ends", "Cloud Backoff Ends", type_key=None)
        self._expiry_cancel: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._ensure_expiry_timer()

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        self._cancel_expiry_timer()

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._ensure_expiry_timer()

    @property
    def native_value(self):
        ends = self._coord.backoff_ends_utc
        if ends is None:
            return None
        try:
            now = dt_util.utcnow()
        except Exception:  # noqa: BLE001
            return None
        if ends <= now:
            return None
        return ends

    @property
    def extra_state_attributes(self):
        return {}

    @property
    def device_info(self):
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)

    @callback
    def _ensure_expiry_timer(self) -> None:
        if self.hass is None:
            return
        ends = self._coord.backoff_ends_utc
        try:
            now = dt_util.utcnow()
        except Exception:  # noqa: BLE001
            self._cancel_expiry_timer()
            return
        if ends is None or ends <= now:
            self._cancel_expiry_timer()
            return
        self._cancel_expiry_timer()
        fire_at = ends + timedelta(seconds=1)
        self._expiry_cancel = async_track_point_in_utc_time(
            self.hass, self._handle_backoff_expired, fire_at
        )

    @callback
    def _handle_backoff_expired(self, _now: datetime) -> None:
        self._cancel_expiry_timer()
        self.async_write_ha_state()

    @callback
    def _cancel_expiry_timer(self) -> None:
        if self._expiry_cancel:
            try:
                self._expiry_cancel()
            except Exception:  # noqa: BLE001
                pass
            self._expiry_cancel = None


class EnphaseSystemControllerInventorySensor(_SiteBaseEntity):
    _attr_translation_key = "system_controller_inventory"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "last_reported_utc",
            "last_reported",
            "last_report",
            "last_reported_at",
        }
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "type_enpower_inventory",
            "System Controller",
            type_key="envoy",
        )

    def _member(self) -> dict[str, object] | None:
        return _gateway_system_controller_member(self._coord)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._member() is not None

    @property
    def native_value(self):
        return _gateway_meter_status_text(
            self._member(), getattr(self, "hass", None) or self._coord.hass
        )

    @property
    def extra_state_attributes(self):
        member = self._member()
        if not isinstance(member, dict):
            return {}
        last_reported = _gateway_meter_last_reported(member)
        terminal_values = _gateway_terminal_values(member)
        terminal_descriptions = _gateway_terminal_descriptions(member)
        attrs = {
            "name": _gateway_clean_text(member.get("name")) or "System Controller",
            "status_text": _gateway_meter_status_text(
                member, getattr(self, "hass", None) or self._coord.hass
            ),
            "status_raw": _gateway_clean_text(
                member.get("statusText")
                if member.get("statusText") is not None
                else member.get("status")
            ),
            "connected": _gateway_optional_bool(member.get("connected")),
            "channel_type": _gateway_clean_text(member.get("channel_type")),
            "serial_number": _gateway_clean_text(member.get("serial_number")),
            "last_reported_utc": (
                last_reported.isoformat() if last_reported is not None else None
            ),
        }
        attrs.update(terminal_values)
        if terminal_descriptions:
            attrs["terminal_descriptions"] = terminal_descriptions
        attrs.update(
            _gateway_flat_member_attributes(
                member,
                skip_keys={
                    "name",
                    "status_text",
                    "status_raw",
                    "connected",
                    "channel_type",
                    "serial_number",
                    "last_reported_utc",
                    "status",
                    "statusText",
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                },
            )
        )
        return attrs


class EnphaseDryContactsInventorySensor(_SiteBaseEntity):
    _attr_translation_key = "dry_contacts"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "members",
            "contacts",
            "unmatched_settings",
            "last_reported_utc",
        }
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "dry_contacts_inventory",
            "Dry Contacts",
            type_key="envoy",
        )

    def _members(self) -> list[dict[str, object]]:
        return _gateway_dry_contact_members(self._coord)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return bool(self._members())

    @property
    def native_value(self):
        status_values: dict[str, str] = {}
        for member in self._members():
            status_text = _gateway_meter_status_text(
                member, getattr(self, "hass", None) or self._coord.hass
            )
            if status_text:
                normalized = status_text.casefold()
                if normalized not in status_values:
                    status_values[normalized] = status_text
        if not status_values:
            return None
        unique_values = [status_values[key] for key in sorted(status_values)]
        if len(unique_values) == 1:
            return unique_values[0]
        return " | ".join(unique_values)

    @property
    def extra_state_attributes(self):
        members = self._members()
        if not members:
            return {}
        settings_matches, unmatched_settings = self._coord.dry_contact_settings_matches(
            members
        )
        dry_contact_settings_supported = self._coord.dry_contact_settings_supported
        latest_reported: datetime | None = None
        visible_count = 0
        visible_seen = False
        enabled_count = 0
        enabled_seen = False
        in_use_count = 0
        in_use_seen = False
        contacts: list[dict[str, object]] = []
        for index, member in enumerate(members, start=1):
            member_last_reported = _gateway_meter_last_reported(member)
            if member_last_reported is None:
                pass
            elif latest_reported is None or member_last_reported > latest_reported:
                latest_reported = member_last_reported
            visible = _gateway_optional_bool(
                member.get("visible")
                if member.get("visible") is not None
                else (
                    member.get("is_visible")
                    if member.get("is_visible") is not None
                    else member.get("isVisible")
                )
            )
            if visible is not None:
                visible_seen = True
                if visible:
                    visible_count += 1
            enabled = _gateway_optional_bool(
                member.get("enabled")
                if member.get("enabled") is not None
                else (
                    member.get("is_enabled")
                    if member.get("is_enabled") is not None
                    else member.get("isEnabled")
                )
            )
            if enabled is not None:
                enabled_seen = True
                if enabled:
                    enabled_count += 1
            in_use = _gateway_optional_bool(
                member.get("in_use")
                if member.get("in_use") is not None
                else (
                    member.get("inUse")
                    if member.get("inUse") is not None
                    else (
                        member.get("used")
                        if member.get("used") is not None
                        else member.get("active")
                    )
                )
            )
            if in_use is not None:
                in_use_seen = True
                if in_use:
                    in_use_count += 1
            status_raw = _gateway_clean_text(
                member.get("statusText")
                if member.get("statusText") is not None
                else member.get("status")
            )
            terminal_values = _gateway_terminal_values(member)
            terminal_descriptions = _gateway_terminal_descriptions(member)
            contact: dict[str, object] = {
                "index": index,
                "name": _gateway_clean_text(member.get("name"))
                or f"Dry Contact {index}",
                "status_text": _gateway_meter_status_text(
                    member, getattr(self, "hass", None) or self._coord.hass
                ),
                "status_raw": status_raw,
                "connected": _gateway_optional_bool(member.get("connected")),
                "channel_type": _gateway_clean_text(
                    member.get("channel_type")
                    if member.get("channel_type") is not None
                    else member.get("channelType")
                ),
                "serial_number": _gateway_clean_text(
                    member.get("serial_number")
                    if member.get("serial_number") is not None
                    else member.get("serial")
                ),
                "visible": visible,
                "enabled": enabled,
                "in_use": in_use,
                "properties": dict(member),
                **terminal_values,
                "terminal_descriptions": terminal_descriptions,
            }
            matched_settings = (
                settings_matches[index - 1]
                if (index - 1) < len(settings_matches)
                else None
            )
            if isinstance(matched_settings, dict):
                for key in (
                    "configured_name",
                    "override_supported",
                    "override_active",
                    "control_mode",
                    "polling_interval_seconds",
                    "soc_threshold",
                    "soc_threshold_min",
                    "soc_threshold_max",
                ):
                    value = matched_settings.get(key)
                    if value is not None:
                        contact[key] = value
                schedule_windows = matched_settings.get("schedule_windows")
                if isinstance(schedule_windows, list) and schedule_windows:
                    contact["schedule_windows"] = [
                        dict(window) if isinstance(window, dict) else window
                        for window in schedule_windows
                    ]
            contacts.append(contact)

        attrs: dict[str, object] = {
            "name": "Dry Contacts",
            "member_count": len(members),
            "status_text": self.native_value,
            "last_reported_utc": (
                latest_reported.isoformat() if latest_reported is not None else None
            ),
            "contacts": contacts,
            "members": [dict(member) for member in members],
            "dry_contact_settings_supported": dry_contact_settings_supported,
            "dry_contact_settings_contact_count": len(
                self._coord.dry_contact_settings_entries()
            ),
        }
        if unmatched_settings:
            attrs["unmatched_settings"] = [
                dict(entry) if isinstance(entry, dict) else entry
                for entry in unmatched_settings
            ]
        if visible_seen:
            attrs["visible_contact_count"] = visible_count
        if enabled_seen:
            attrs["enabled_contact_count"] = enabled_count
        if in_use_seen:
            attrs["in_use_contact_count"] = in_use_count
        if len(members) == 1:
            member = members[0]
            matched_settings = settings_matches[0] if settings_matches else None
            attrs.update(
                {
                    "channel_type": _gateway_clean_text(
                        member.get("channel_type")
                        if member.get("channel_type") is not None
                        else member.get("channelType")
                    ),
                    "serial_number": _gateway_clean_text(
                        member.get("serial_number")
                        if member.get("serial_number") is not None
                        else member.get("serial")
                    ),
                    "connected": _gateway_optional_bool(member.get("connected")),
                    "status_raw": _gateway_clean_text(
                        member.get("statusText")
                        if member.get("statusText") is not None
                        else member.get("status")
                    ),
                }
            )
            terminal_descriptions = _gateway_terminal_descriptions(member)
            attrs.update(_gateway_terminal_values(member))
            if terminal_descriptions:
                attrs["terminal_descriptions"] = terminal_descriptions
            if isinstance(matched_settings, dict):
                for key in (
                    "configured_name",
                    "override_supported",
                    "override_active",
                    "control_mode",
                    "polling_interval_seconds",
                    "soc_threshold",
                    "soc_threshold_min",
                    "soc_threshold_max",
                ):
                    value = matched_settings.get(key)
                    if value is not None:
                        attrs[key] = value
                schedule_windows = matched_settings.get("schedule_windows")
                if isinstance(schedule_windows, list) and schedule_windows:
                    attrs["schedule_windows"] = [
                        dict(window) if isinstance(window, dict) else window
                        for window in schedule_windows
                    ]
            attrs.update(
                _gateway_flat_member_attributes(
                    member,
                    skip_keys={
                        "name",
                        "status",
                        "status_text",
                        "status_raw",
                        "channel_type",
                        "serial_number",
                        "connected",
                        "last_reported_utc",
                        "last_report",
                        "last_reported",
                        "last_reported_at",
                        "members",
                    },
                )
            )
        return attrs


class _EnphaseGatewayMeterSensor(_SiteBaseEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "meter_attributes",
            "last_reported_utc",
        }
    )

    def __init__(
        self,
        coord: EnphaseCoordinator,
        meter_kind: str,
        label: str,
    ) -> None:
        super().__init__(
            coord,
            f"gateway_{meter_kind}_meter",
            label,
            type_key="envoy",
        )
        self._meter_kind = meter_kind

    def _member(self) -> dict[str, object] | None:
        return _gateway_meter_member(self._coord, self._meter_kind)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._member() is not None

    @property
    def native_value(self):
        return _gateway_meter_status_text(
            self._member(), getattr(self, "hass", None) or self._coord.hass
        )

    @property
    def extra_state_attributes(self):
        member = self._member()
        if not isinstance(member, dict):
            return {}
        last_reported = _gateway_meter_last_reported(member)
        status_text = _gateway_meter_status_text(
            member, getattr(self, "hass", None) or self._coord.hass
        )
        attrs: dict[str, object] = {
            "meter_name": _gateway_clean_text(member.get("name")),
            "meter_type": self._meter_kind,
            "dashboard_meter_type": _gateway_clean_text(member.get("meter_type")),
            "channel_type": _gateway_clean_text(member.get("channel_type")),
            "serial_number": _gateway_clean_text(member.get("serial_number")),
            "connected": _gateway_optional_bool(member.get("connected")),
            "status_text": status_text,
            "status_raw": _gateway_clean_text(
                member.get("statusText")
                if member.get("statusText") is not None
                else member.get("status")
            ),
            "last_reported_utc": (
                last_reported.isoformat() if last_reported is not None else None
            ),
            "meter_state": _gateway_clean_text(member.get("meter_state")),
            "config_type": _gateway_clean_text(member.get("config_type")),
            "ip_address": _gateway_clean_text(
                member.get("ip")
                if member.get("ip") is not None
                else member.get("ip_address")
            ),
            "meter_attributes": dict(member),
        }
        attrs.update(
            _gateway_flat_member_attributes(
                member,
                skip_keys={
                    "name",
                    "channel_type",
                    "serial_number",
                    "connected",
                    "status_text",
                    "status_raw",
                    "meter_type",
                    "meter_state",
                    "config_type",
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                    "ip",
                    "ip_address",
                },
            )
        )
        return attrs


class EnphaseGatewayProductionMeterSensor(_EnphaseGatewayMeterSensor):
    _attr_translation_key = "gateway_production_meter"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "production", "Production Meter")


class EnphaseGatewayConsumptionMeterSensor(_EnphaseGatewayMeterSensor):
    _attr_translation_key = "gateway_consumption_meter"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "consumption", "Consumption Meter")


class EnphaseGatewayIQEnergyRouterSensor(_SiteBaseEntity):
    _attr_translation_key = "gateway_iq_energy_router"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"last_reported_utc"}
    )

    def __init__(
        self,
        coord: EnphaseCoordinator,
        router_key: str,
        index: int,
    ) -> None:
        normalized_key = _gateway_iq_energy_router_identity(router_key) or str(
            router_key
        )
        super().__init__(
            coord,
            f"gateway_iq_energy_router_{normalized_key}",
            f"IQ Energy Router_{index}",
            type_key="envoy",
        )
        self._router_key = normalized_key
        self._index = max(1, int(index))
        self._attr_translation_placeholders = {"index": str(self._index)}

    def _member(self) -> dict[str, object] | None:
        record = _gateway_iq_energy_router_record(self._coord, self._router_key)
        if not isinstance(record, dict):
            return None
        member = record.get("member")
        if not isinstance(member, dict):
            return None
        return dict(member)

    @property
    def name(self) -> str | None:
        member = self._member()
        member_name = (
            _gateway_clean_text(member.get("name"))
            if isinstance(member, dict)
            else None
        )
        if member_name:
            return member_name
        # Prefer translated fallback names when this entity is platform-attached.
        if getattr(self, "platform", None) is not None:
            try:
                translated_name = super().name
            except Exception:  # noqa: BLE001
                translated_name = None
            if translated_name:
                return translated_name
        return f"IQ Energy Router_{self._index}"

    @property
    def available(self) -> bool:
        if self._member() is None:
            return False
        if self._coord.last_success_utc is not None:
            return True
        return CoordinatorEntity.available.fget(self)

    @property
    def native_value(self):
        return _gateway_meter_status_text(
            self._member(), getattr(self, "hass", None) or self._coord.hass
        )

    @property
    def extra_state_attributes(self):
        member = self._member()
        if not isinstance(member, dict):
            return {}
        status_text = _gateway_meter_status_text(
            member, getattr(self, "hass", None) or self._coord.hass
        )
        last_reported = _gateway_iq_energy_router_last_reported(member)
        attrs: dict[str, object] = {
            "name": _gateway_clean_text(member.get("name"))
            or f"IQ Energy Router_{self._index}",
            "status_text": status_text,
            "status_raw": _gateway_clean_text(
                member.get("statusText")
                if member.get("statusText") is not None
                else member.get("status")
            ),
            "device_type": _gateway_clean_text(
                member.get("device-type")
                if member.get("device-type") is not None
                else member.get("device_type")
            ),
            "uid": _gateway_clean_text(member.get("uid")),
            "device_uid": _gateway_clean_text(
                member.get("device-uid")
                if member.get("device-uid") is not None
                else member.get("device_uid")
            ),
            "make": _gateway_clean_text(member.get("make")),
            "model": _gateway_clean_text(member.get("model")),
            "pairing_status": _gateway_clean_text(
                member.get("pairing-status")
                if member.get("pairing-status") is not None
                else member.get("pairing_status")
            ),
            "device_state": _gateway_clean_text(
                member.get("device-state")
                if member.get("device-state") is not None
                else member.get("device_state")
            ),
            "iqer_uid": _gateway_clean_text(
                member.get("iqer-uid")
                if member.get("iqer-uid") is not None
                else member.get("iqer_uid")
            ),
            "hems_device_id": _gateway_clean_text(
                member.get("hems-device-id")
                if member.get("hems-device-id") is not None
                else member.get("hems_device_id")
            ),
            "hems_device_facet_id": _gateway_clean_text(
                member.get("hems-device-facet-id")
                if member.get("hems-device-facet-id") is not None
                else member.get("hems_device_facet_id")
            ),
            "last_reported_utc": (
                last_reported.isoformat() if last_reported is not None else None
            ),
        }
        attrs.update(
            _gateway_flat_member_attributes(
                member,
                skip_keys={
                    "name",
                    "status",
                    "status_text",
                    "status_raw",
                    "device_type",
                    "uid",
                    "device_uid",
                    "make",
                    "model",
                    "pairing_status",
                    "device_state",
                    "iqer_uid",
                    "hems_device_id",
                    "hems_device_facet_id",
                    "last_reported_utc",
                    "last_report",
                    "last_reported",
                    "last_reported_at",
                    "last_reported_at_utc",
                },
            )
        )
        return attrs


class EnphaseGatewayConnectivityStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "gateway_connectivity_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "latest_reported_utc",
            "latest_reported_device",
            "property_keys",
        }
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "gateway_connectivity_status",
            "Gateway Status",
            type_key="envoy",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = _gateway_inventory_snapshot(self._coord)
        if int(snapshot.get("total_devices", 0) or 0) > 0:
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self):
        return _title_case_status(
            _gateway_connectivity_state(_gateway_inventory_snapshot(self._coord)),
            getattr(self, "hass", None) or self._coord.hass,
        )

    @property
    def extra_state_attributes(self):
        snapshot = _gateway_inventory_snapshot(self._coord)
        return {
            "total_devices": snapshot.get("total_devices"),
            "connected_devices": snapshot.get("connected_devices"),
            "disconnected_devices": snapshot.get("disconnected_devices"),
            "unknown_connection_devices": snapshot.get("unknown_connection_devices"),
            "status_counts": snapshot.get("status_counts"),
            "status_summary": snapshot.get("status_summary"),
            "model_summary": snapshot.get("model_summary"),
            "firmware_summary": snapshot.get("firmware_summary"),
            "latest_reported_utc": snapshot.get("latest_reported_utc"),
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "property_keys": snapshot.get("property_keys"),
        }


class EnphaseGatewayLastReportedSensor(_SiteBaseEntity):
    _attr_translation_key = "gateway_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "gateway_last_reported",
            "Gateway Last Reported",
            type_key="envoy",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = _gateway_inventory_snapshot(self._coord)
        return snapshot.get("latest_reported") is not None

    @property
    def native_value(self):
        snapshot = _gateway_inventory_snapshot(self._coord)
        return snapshot.get("latest_reported")

    @property
    def extra_state_attributes(self):
        snapshot = _gateway_inventory_snapshot(self._coord)
        return {
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "without_last_report_count": snapshot.get("without_last_report_count"),
            "total_devices": snapshot.get("total_devices"),
            "status_summary": snapshot.get("status_summary"),
        }


class EnphaseMicroinverterConnectivityStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "microinverter_connectivity_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "microinverter_connectivity_status",
            "Microinverter Connectivity Status",
            type_key="microinverter",
        )

    @property
    def available(self) -> bool:
        if not bool(getattr(self._coord, "include_inverters", True)):
            return False
        if not super().available:
            return False
        snapshot = _microinverter_inventory_snapshot(self._coord)
        if int(snapshot.get("total_inverters", 0) or 0) > 0:
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self):
        return _title_case_status(
            _microinverter_inventory_snapshot(self._coord).get("connectivity_state"),
            getattr(self, "hass", None) or self._coord.hass,
        )

    @property
    def extra_state_attributes(self):
        snapshot = _microinverter_inventory_snapshot(self._coord)
        return {
            "total_inverters": snapshot.get("total_inverters"),
            "reporting_inverters": snapshot.get("reporting_inverters"),
            "not_reporting_inverters": snapshot.get("not_reporting_inverters"),
            "unknown_inverters": snapshot.get("unknown_inverters"),
            "status_counts": snapshot.get("status_counts"),
            "status_summary": snapshot.get("status_summary"),
        }


class EnphaseMicroinverterReportingCountSensor(_SiteBaseEntity):
    _attr_translation_key = "microinverter_reporting_count"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "devices",
            "model_counts",
            "firmware_counts",
            "array_counts",
            "panel_info",
            "status_type_counts",
        }
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "microinverter_reporting_count",
            "Active Microinverters",
            type_key="microinverter",
        )

    @property
    def available(self) -> bool:
        if not bool(getattr(self._coord, "include_inverters", True)):
            return False
        if not super().available:
            return False
        snapshot = _microinverter_inventory_snapshot(self._coord)
        if int(snapshot.get("total_inverters", 0) or 0) > 0:
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self):
        snapshot = _microinverter_inventory_snapshot(self._coord)
        if int(snapshot.get("total_inverters", 0) or 0) <= 0:
            return None
        return int(snapshot.get("reporting_inverters", 0) or 0)

    @property
    def extra_state_attributes(self):
        snapshot = _microinverter_inventory_snapshot(self._coord)
        bucket = self._coord.inventory_view.type_bucket("microinverter") or {}
        members = bucket.get("devices")
        safe_members = (
            [dict(item) for item in members if isinstance(item, dict)]
            if isinstance(members, list)
            else []
        )
        try:
            device_count = int(
                bucket.get("count", snapshot.get("total_inverters", 0)) or 0
            )
        except Exception:
            device_count = int(snapshot.get("total_inverters", 0) or 0)
        type_label = bucket.get("type_label")
        if not isinstance(type_label, str) or not type_label.strip():
            candidate = self._coord.inventory_view.type_label("microinverter")
            if isinstance(candidate, str) and candidate.strip():
                type_label = candidate
            else:
                type_label = "Microinverters"
        return {
            "type_key": bucket.get("type_key") or "microinverter",
            "type_label": type_label,
            "device_count": device_count,
            "devices": safe_members,
            "model_counts": (
                dict(bucket.get("model_counts"))
                if isinstance(bucket.get("model_counts"), dict)
                else None
            ),
            "model_summary": snapshot.get("model_summary"),
            "firmware_counts": (
                dict(bucket.get("firmware_counts"))
                if isinstance(bucket.get("firmware_counts"), dict)
                else None
            ),
            "firmware_summary": snapshot.get("firmware_summary"),
            "array_counts": (
                dict(bucket.get("array_counts"))
                if isinstance(bucket.get("array_counts"), dict)
                else None
            ),
            "array_summary": snapshot.get("array_summary"),
            "panel_info": snapshot.get("panel_info"),
            "status_type_counts": snapshot.get("status_type_counts"),
            "production_start_date": snapshot.get("production_start_date"),
            "production_end_date": snapshot.get("production_end_date"),
        }


class EnphaseMicroinverterLastReportedSensor(_SiteBaseEntity):
    _attr_translation_key = "microinverter_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "microinverter_last_reported",
            "Microinverter Last Reported",
            type_key="microinverter",
        )

    @property
    def available(self) -> bool:
        if not bool(getattr(self._coord, "include_inverters", True)):
            return False
        if not super().available:
            return False
        snapshot = _microinverter_inventory_snapshot(self._coord)
        return snapshot.get("latest_reported") is not None

    @property
    def native_value(self):
        return _microinverter_inventory_snapshot(self._coord).get("latest_reported")

    @property
    def extra_state_attributes(self):
        snapshot = _microinverter_inventory_snapshot(self._coord)
        return {
            "latest_reported_device": snapshot.get("latest_reported_device"),
        }


class EnphaseHeatPumpStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "heat_pump_status"
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord, "heat_pump_status", "Heat Pump Status", type_key="heatpump"
        )

    def _snapshot(self) -> dict[str, object]:
        return _heatpump_runtime_snapshot(self._coord)

    def _runtime_device_uid(self) -> str | None:
        getter = getattr(self._coord, "_heatpump_runtime_device_uid", None)
        if callable(getter):
            try:
                return getter()
            except Exception:  # noqa: BLE001
                return None
        uid = self._snapshot().get("device_uid")
        return _gateway_clean_text(uid)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if not self._runtime_device_uid():
            return False
        return self.native_value is not None

    @property
    def native_value(self):
        return _title_case_status(
            self._snapshot().get("heatpump_status"),
            getattr(self, "hass", None) or self._coord.hass,
        )

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot()
        sg_ready_details = _heatpump_sg_ready_semantics(
            snapshot.get("sg_ready_mode_label") or snapshot.get("sg_ready_mode_raw")
        )
        attrs = _heatpump_runtime_common_attrs(self._coord, snapshot)
        attrs.update(
            {
                "heatpump_status_raw": snapshot.get("heatpump_status"),
                "sg_ready_mode_raw": snapshot.get("sg_ready_mode_raw"),
                "sg_ready_mode_label": snapshot.get("sg_ready_mode_label"),
                "sg_ready_active": snapshot.get("sg_ready_active"),
                "sg_ready_contact_state": snapshot.get("sg_ready_contact_state"),
                "vpp_sgready_mode_override": snapshot.get("vpp_sgready_mode_override"),
                **sg_ready_details,
            }
        )
        return attrs


class EnphaseHeatPumpConnectivityStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "heat_pump_connectivity_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"members", "latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "heat_pump_connectivity_status",
            "Heat Pump Connectivity Status",
            type_key="heatpump",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = _heatpump_snapshot(self._coord)
        if int(snapshot.get("total_devices", 0) or 0) > 0:
            return True
        return not bool(getattr(self._coord, "_devices_inventory_ready", False))

    @property
    def native_value(self):
        return _title_case_status(
            _heatpump_snapshot(self._coord).get("overall_status_text"),
            getattr(self, "hass", None) or self._coord.hass,
        )

    @property
    def extra_state_attributes(self):
        snapshot = _heatpump_snapshot(self._coord)
        members = snapshot.get("members")
        safe_members = (
            [dict(member) for member in members if isinstance(member, dict)]
            if isinstance(members, list)
            else []
        )
        return {
            "total_devices": snapshot.get("total_devices"),
            "status_counts": snapshot.get("status_counts"),
            "status_summary": snapshot.get("status_summary"),
            "device_type_counts": snapshot.get("device_type_counts"),
            "model_summary": snapshot.get("model_summary"),
            "firmware_summary": snapshot.get("firmware_summary"),
            "latest_reported_utc": snapshot.get("latest_reported_utc"),
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "hems_data_stale": snapshot.get("hems_data_stale"),
            "hems_last_success_utc": snapshot.get("hems_last_success_utc"),
            "hems_last_success_age_s": snapshot.get("hems_last_success_age_s"),
            "members": safe_members,
        }


class _EnphaseHeatPumpDeviceTypeSensor(_SiteBaseEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"members", "latest_reported_device"}
    )
    _heatpump_device_type: str

    def __init__(
        self,
        coord: EnphaseCoordinator,
        *,
        key: str,
        name: str,
        device_type: str,
    ) -> None:
        super().__init__(coord, key, name, type_key="heatpump")
        self._heatpump_device_type = device_type

    def _snapshot(self) -> dict[str, object]:
        return _heatpump_type_snapshot(
            self._coord, device_type=self._heatpump_device_type
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return int(self._snapshot().get("member_count", 0) or 0) > 0

    @property
    def native_value(self):
        return self._snapshot().get("native_status")

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot()
        members = snapshot.get("members")
        return {
            "device_type": snapshot.get("device_type"),
            "member_count": snapshot.get("member_count"),
            "status_counts": snapshot.get("status_counts"),
            "status_summary": snapshot.get("status_summary"),
            "latest_reported_utc": snapshot.get("latest_reported_utc"),
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "hems_data_stale": snapshot.get("hems_data_stale"),
            "hems_last_success_utc": snapshot.get("hems_last_success_utc"),
            "hems_last_success_age_s": snapshot.get("hems_last_success_age_s"),
            "members": (
                [dict(member) for member in members if isinstance(member, dict)]
                if isinstance(members, list)
                else []
            ),
        }


class EnphaseHeatPumpSgReadyModeSensor(_SiteBaseEntity):
    _attr_translation_key = "heat_pump_sg_ready_mode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "heat_pump_sg_ready_mode",
            "Heat Pump SG-Ready Mode",
            type_key="heatpump",
        )

    def _snapshot(self) -> dict[str, object]:
        return _heatpump_runtime_snapshot(self._coord)

    def _runtime_device_uid(self) -> str | None:
        getter = getattr(self._coord, "_heatpump_runtime_device_uid", None)
        if callable(getter):
            try:
                return getter()
            except Exception:  # noqa: BLE001
                return None
        uid = self._snapshot().get("device_uid")
        return _gateway_clean_text(uid)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if not self._runtime_device_uid():
            return False
        snapshot = self._snapshot()
        return any(
            snapshot.get(key) is not None
            for key in ("sg_ready_mode_label", "sg_ready_mode_raw")
        )

    @property
    def native_value(self):
        snapshot = self._snapshot()
        return snapshot.get("sg_ready_mode_label") or snapshot.get("sg_ready_mode_raw")

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot()
        details = _heatpump_sg_ready_semantics(
            snapshot.get("sg_ready_mode_label") or snapshot.get("sg_ready_mode_raw")
        )
        return {
            "heatpump_status_raw": snapshot.get("heatpump_status"),
            "sg_ready_mode_raw": snapshot.get("sg_ready_mode_raw"),
            "sg_ready_mode_label": snapshot.get("sg_ready_mode_label"),
            "sg_ready_active": snapshot.get("sg_ready_active"),
            "sg_ready_contact_state": snapshot.get("sg_ready_contact_state"),
            "vpp_sgready_mode_override": snapshot.get("vpp_sgready_mode_override"),
            **details,
        }


class EnphaseHeatPumpEnergyMeterSensor(_EnphaseHeatPumpDeviceTypeSensor):
    _attr_translation_key = "heat_pump_energy_meter"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            key="heat_pump_energy_meter",
            name="Heat Pump Energy Meter Status",
            device_type="ENERGY_METER",
        )


class EnphaseHeatPumpSgReadyGatewaySensor(_EnphaseHeatPumpDeviceTypeSensor):
    _attr_translation_key = "heat_pump_sg_ready_gateway"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            key="heat_pump_sg_ready_gateway",
            name="Heat Pump SG-Ready Gateway Status",
            device_type="SG_READY_GATEWAY",
        )


class EnphaseHeatPumpLastReportedSensor(_SiteBaseEntity):
    _attr_translation_key = "heat_pump_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "heat_pump_last_reported",
            "Heat Pump Last Reported",
            type_key="heatpump",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if _heatpump_runtime_device_uid(self._coord) is None:
            return False
        return (
            _heatpump_runtime_last_reported(_heatpump_runtime_snapshot(self._coord))
            is not None
        )

    @property
    def native_value(self):
        return _heatpump_runtime_last_reported(_heatpump_runtime_snapshot(self._coord))

    @property
    def extra_state_attributes(self):
        return {}


class _EnphaseHeatPumpDailyEnergySensor(_SiteBaseEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 3
    _daily_key: str

    def __init__(self, coord: EnphaseCoordinator, key: str, name: str) -> None:
        super().__init__(coord, key, name, type_key="heatpump")

    def _snapshot(self) -> dict[str, object]:
        return _heatpump_daily_snapshot(self._coord)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = self._snapshot()
        return snapshot.get(self._daily_key) is not None

    @property
    def native_value(self):
        snapshot = self._snapshot()
        value = snapshot.get(self._daily_key)
        if value is None:
            return None
        try:
            return round(float(value) / 1000.0, 3)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot()
        attrs = _heatpump_daily_common_attrs(self._coord, snapshot)
        if self._daily_key == "daily_energy_wh":
            attrs["source"] = snapshot.get("source")
            attrs["device_uid"] = snapshot.get("device_uid")
            attrs["device_name"] = snapshot.get("device_name")
        else:
            attrs["source"] = snapshot.get("split_source") or snapshot.get("source")
            attrs["device_uid"] = snapshot.get("split_device_uid")
            attrs["device_name"] = snapshot.get("split_device_name")
            attrs["using_stale"] = bool(
                getattr(self._coord, "heatpump_daily_split_using_stale", False)
            )
            attrs["last_success_utc"] = (
                self._coord.heatpump_daily_split_last_success_utc.isoformat()
                if getattr(self._coord, "heatpump_daily_split_last_success_utc", None)
                is not None
                else None
            )
            attrs["last_error"] = getattr(
                self._coord, "heatpump_daily_split_last_error", None
            )
        attrs["details"] = (
            list(snapshot.get("details"))
            if isinstance(snapshot.get("details"), list)
            else []
        )
        attrs["daily_energy_wh"] = snapshot.get("daily_energy_wh")
        attrs["split_daily_energy_wh"] = snapshot.get("split_daily_energy_wh")
        attrs["daily_grid_wh"] = snapshot.get("daily_grid_wh")
        attrs["daily_solar_wh"] = snapshot.get("daily_solar_wh")
        attrs["daily_battery_wh"] = snapshot.get("daily_battery_wh")
        return attrs


class EnphaseHeatPumpDailyEnergySensor(_EnphaseHeatPumpDailyEnergySensor):
    _attr_translation_key = "heat_pump_daily_energy"
    _daily_key = "daily_energy_wh"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "heat_pump_daily_energy", "Heat Pump Daily Energy")


class EnphaseHeatPumpDailyGridEnergySensor(_EnphaseHeatPumpDailyEnergySensor):
    _attr_translation_key = "heat_pump_daily_grid_energy"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _daily_key = "daily_grid_wh"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "heat_pump_daily_grid_energy",
            "Heat Pump Daily Grid Energy",
        )


class EnphaseHeatPumpDailySolarEnergySensor(_EnphaseHeatPumpDailyEnergySensor):
    _attr_translation_key = "heat_pump_daily_solar_energy"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _daily_key = "daily_solar_wh"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "heat_pump_daily_solar_energy",
            "Heat Pump Daily Solar Energy",
        )


class EnphaseHeatPumpDailyBatteryEnergySensor(_EnphaseHeatPumpDailyEnergySensor):
    _attr_translation_key = "heat_pump_daily_battery_energy"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _daily_key = "daily_battery_wh"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "heat_pump_daily_battery_energy",
            "Heat Pump Daily Battery Energy",
        )


class EnphaseHeatPumpPowerSensor(_SiteBaseEntity):
    _attr_translation_key = "heat_pump_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord, "heat_pump_power", "Heat Pump Power", type_key="heatpump"
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coord.heatpump_power_w is not None

    @property
    def native_value(self):
        value = self._coord.heatpump_power_w
        if value is None:
            return None
        return round(value, 3)

    @property
    def extra_state_attributes(self):
        runtime_snapshot = _heatpump_runtime_snapshot(self._coord)
        daily_snapshot = _heatpump_daily_snapshot(self._coord)
        attrs: dict[str, object] = {
            "sampled_at_utc": (
                self._coord.heatpump_power_sample_utc.isoformat()
                if self._coord.heatpump_power_sample_utc is not None
                else None
            ),
            "series_start_utc": (
                self._coord.heatpump_power_start_utc.isoformat()
                if self._coord.heatpump_power_start_utc is not None
                else None
            ),
            "device_uid": self._coord.heatpump_power_device_uid,
            "source": self._coord.heatpump_power_source,
            "raw_power_w": self._coord.heatpump_power_raw_w,
            "power_window_seconds": self._coord.heatpump_power_window_seconds,
            "power_validation": self._coord.heatpump_power_validation,
            "smoothed": self._coord.heatpump_power_smoothed,
            "using_stale": bool(
                getattr(self._coord, "heatpump_power_using_stale", False)
            ),
            "last_success_utc": (
                self._coord.heatpump_power_last_success_utc.isoformat()
                if getattr(self._coord, "heatpump_power_last_success_utc", None)
                is not None
                else None
            ),
            "last_error": self._coord.heatpump_power_last_error,
        }
        attrs.update(
            {
                key: value
                for key, value in _heatpump_runtime_common_attrs(
                    self._coord, runtime_snapshot
                ).items()
                if key
                not in {"device_uid", "source", "last_error", "last_report_at_utc"}
            }
        )
        attrs.update(
            {
                key: value
                for key, value in _heatpump_daily_common_attrs(
                    self._coord, daily_snapshot
                ).items()
                if key
                in {
                    "device_name",
                    "member_name",
                    "member_device_type",
                    "pairing_status",
                    "device_state",
                    "daily_endpoint_type",
                    "daily_endpoint_timestamp",
                    "day_key",
                    "timezone",
                }
            }
        )
        if self._coord.heatpump_power_last_error:
            attrs["last_error"] = self._coord.heatpump_power_last_error
        return attrs


class EnphaseStormAlertSensor(_SiteBaseEntity):
    _attr_translation_key = "storm_alert"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "storm_alert", "Storm Alert", type_key="envoy")

    @property
    def native_value(self):
        active = self._coord.storm_alert_active
        if active is None:
            return None
        return "active" if active else "inactive"

    @property
    def extra_state_attributes(self):
        alerts = getattr(self._coord, "storm_alerts", None)
        if not isinstance(alerts, list):
            alerts = []
        return {
            "storm_alert_active": self._coord.storm_alert_active,
            "critical_alert_override": getattr(
                self._coord, "storm_alert_critical_override", None
            ),
            "storm_alert_count": len(alerts),
            "storm_alerts": alerts,
        }


class EnphaseBatteryOverallChargeSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_overall_charge"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_overall_charge",
            "Battery Overall Charge",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coord.battery_aggregate_charge_pct is not None

    @property
    def native_value(self):
        value = self._coord.battery_aggregate_charge_pct
        if value is None:
            return None
        try:
            return round(float(value), 1)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        return {}


class EnphaseBatteryOverallStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_overall_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_overall_status",
            "Battery Overall Status",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coord.battery_aggregate_status is not None

    @property
    def native_value(self):
        return self._coord.battery_aggregate_status

    @property
    def extra_state_attributes(self):
        summary = self._coord.battery_status_summary
        return {
            "worst_storage_key": summary.get("worst_storage_key"),
            "worst_status": summary.get("worst_status"),
            "per_battery_status": summary.get("per_battery_status"),
            "per_battery_status_raw": summary.get("per_battery_status_raw"),
            "per_battery_status_text": summary.get("per_battery_status_text"),
            "battery_order": summary.get("battery_order"),
        }


class EnphaseBatteryCfgScheduleStatusSensor(_SiteBaseEntity):
    """CFG schedule sync status (none / pending / active)."""

    _attr_translation_key = "battery_cfg_schedule_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_cfg_schedule_status",
            "Battery CFG Schedule Status",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coord.charge_from_grid_control_available

    @property
    def native_value(self):
        return self._coord.battery_cfg_schedule_status or "none"


class _BaseBatteryScheduleInventorySensor(_SiteBaseEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self, coord: EnphaseCoordinator, key: str, translation_key: str
    ) -> None:
        super().__init__(coord, key, translation_key, type_key="encharge")
        self._attr_translation_key = translation_key

    def _inventory(self) -> list[BatteryScheduleRecord]:
        return battery_schedule_inventory(self._coord)

    @property
    def available(self) -> bool:
        return super().available and _battery_schedule_inventory_supported(self._coord)


class EnphaseBatteryScheduleModeSensor(_BaseBatteryScheduleInventorySensor):
    def __init__(self, coord: EnphaseCoordinator, schedule_type: str):
        mode_key = str(schedule_type).lower()
        super().__init__(
            coord,
            f"battery_{mode_key}_schedules",
            f"battery_{mode_key}_schedules",
        )
        self._schedule_type = mode_key

    def _records(self) -> list[BatteryScheduleRecord]:
        return [
            schedule
            for schedule in self._inventory()
            if schedule.schedule_type == self._schedule_type
        ]

    @property
    def native_value(self) -> str:
        return str(len(self._records()))

    @property
    def extra_state_attributes(self):
        records = self._records()
        attrs = self._cloud_diag_attrs()
        attrs.update(
            {
                "schedule_type": self._schedule_type,
                "schedule_count": len(records),
                "schedule_ids": [schedule.schedule_id for schedule in records],
                "schedules": [schedule.as_dict() for schedule in records],
            }
        )
        return attrs


class EnphaseBatteryAvailableEnergySensor(_SiteBaseEntity):
    _attr_translation_key = "battery_available_energy"
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_available_energy",
            "Battery Available Energy",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self.native_value is not None

    @property
    def native_value(self):
        summary = self._coord.battery_status_summary
        value = summary.get("site_available_energy_kwh")
        if value is None:
            return None
        try:
            return round(float(value), 2)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        sampled_at = getattr(self._coord, "battery_summary_sample_utc", None)
        return {
            "sampled_at_utc": (
                sampled_at.isoformat() if sampled_at is not None else None
            ),
        }


class EnphaseBatteryAvailablePowerSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_available_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_available_power",
            "Battery Available Power",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self.native_value is not None

    @property
    def native_value(self):
        summary = self._coord.battery_status_summary
        value = summary.get("site_available_power_kw")
        if value is None:
            return None
        try:
            return round(float(value), 3)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        sampled_at = getattr(self._coord, "battery_summary_sample_utc", None)
        return {
            "sampled_at_utc": (
                sampled_at.isoformat() if sampled_at is not None else None
            ),
        }


class EnphaseBatteryLastReportedSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "battery_last_reported",
            "Battery Last Reported",
            type_key="encharge",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = _battery_last_reported_snapshot(self._coord)
        return snapshot.get("latest_reported") is not None

    @property
    def native_value(self):
        return _battery_last_reported_snapshot(self._coord).get("latest_reported")

    @property
    def extra_state_attributes(self):
        snapshot = _battery_last_reported_snapshot(self._coord)
        return {
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "without_last_report_count": snapshot.get("without_last_report_count"),
            "total_batteries": snapshot.get("total_batteries"),
        }


class _EnphaseTariffBaseSensor(_SiteBaseEntity):
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "configured_rates",
            "seasons",
            "last_refresh_utc",
        }
    )

    @property
    def available(self) -> bool:
        return bool(_tariff_data_available(self._coord) and super().available)

    @property
    def device_info(self):
        info = _type_device_info(self._coord, "envoy")
        if info is not None:
            return info
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)

    def _last_refresh_attr(self) -> dict[str, object]:
        last_refresh = getattr(self._coord, "tariff_last_refresh_utc", None)
        if isinstance(last_refresh, datetime):
            return {"last_refresh_utc": last_refresh.isoformat()}
        return {}


class EnphaseTariffBillingSensor(_EnphaseTariffBaseSensor):
    _attr_translation_key = "tariff_billing_cycle"
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-month"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord, "tariff_billing_cycle", "Next Billing Date", type_key=None
        )

    def _snapshot(self):
        return getattr(self._coord, "tariff_billing", None)

    @property
    def available(self) -> bool:
        snapshot = self._snapshot()
        return (
            snapshot is not None
            and next_billing_date(snapshot) is not None
            and super().available
        )

    @property
    def native_value(self):
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        return next_billing_date(snapshot)

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot()
        if snapshot is None:
            return {}
        attrs = dict(snapshot.attributes)
        attrs.update(self._last_refresh_attr())
        return attrs


class EnphaseTariffRateSensor(_EnphaseTariffBaseSensor):
    def __init__(self, coord: EnphaseCoordinator, is_import: bool):
        self._is_import = is_import
        key = "tariff_import_rate" if is_import else "tariff_export_rate"
        name = "Import Rate" if is_import else "Export Rate"
        self._attr_translation_key = key
        self._attr_icon = "mdi:cash-minus" if is_import else "mdi:cash-plus"
        super().__init__(coord, key, name, type_key=None)

    def _snapshot(self):
        attr = "tariff_import_rate" if self._is_import else "tariff_export_rate"
        return getattr(self._coord, attr, None)

    @property
    def available(self) -> bool:
        return self._snapshot() is not None and super().available

    @property
    def native_value(self):
        snapshot = self._snapshot()
        return getattr(snapshot, "state", None)

    @property
    def extra_state_attributes(self):
        snapshot = self._snapshot()
        if snapshot is None:
            return {}
        attrs = dict(snapshot.attributes)
        attrs.update(self._last_refresh_attr())
        return attrs


class EnphaseCurrentTariffRateSensor(_EnphaseTariffBaseSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, coord: EnphaseCoordinator, *, is_import: bool):
        self._is_import = is_import
        key = (
            "tariff_current_import_rate" if is_import else "tariff_current_export_rate"
        )
        name = "Current Import Rate" if is_import else "Current Export Rate"
        self._rate_attr = "tariff_import_rate" if is_import else "tariff_export_rate"
        self._attr_translation_key = key
        self._attr_icon = "mdi:cash-minus" if is_import else "mdi:cash-plus"
        self._tariff_boundary_cancel: CALLBACK_TYPE | None = None
        super().__init__(coord, key, name, type_key=None)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._ensure_tariff_boundary_timer()

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        self._cancel_tariff_boundary_timer()

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._ensure_tariff_boundary_timer()

    def _spec(self):
        return current_tariff_rate_sensor_spec(
            getattr(self._coord, self._rate_attr, None),
            _tariff_now(self._coord, getattr(self, "hass", None)),
        )

    def _configured_rates(self) -> list[dict[str, object]]:
        rates: list[dict[str, object]] = []
        for spec in tariff_rate_sensor_specs(
            getattr(self._coord, self._rate_attr, None)
        ):
            attrs = spec.get("attributes") or {}
            rate: dict[str, object] = {
                key: value
                for key, value in {
                    "name": spec.get("name"),
                    "rate": attrs.get("rate"),
                    "formatted_rate": attrs.get("formatted_rate"),
                    "unit": spec.get("unit"),
                    "season_id": attrs.get("season_id"),
                    "start_month": attrs.get("start_month"),
                    "end_month": attrs.get("end_month"),
                    "day_group_id": attrs.get("day_group_id"),
                    "days": attrs.get("days"),
                    "period_type": attrs.get("period_type"),
                    "start_time": attrs.get("start_time"),
                    "end_time": attrs.get("end_time"),
                    "tier_id": attrs.get("tier_id"),
                    "start_value": attrs.get("start_value"),
                    "end_value": attrs.get("end_value"),
                    "unbounded": attrs.get("unbounded"),
                    "tariff_locator": attrs.get("tariff_locator"),
                }.items()
                if value is not None
            }
            rates.append(rate)
        return rates

    @property
    def available(self) -> bool:
        return self._spec() is not None and super().available

    @property
    def native_value(self):
        spec = self._spec()
        if spec is None:
            return None
        return spec.get("state")

    @property
    def native_unit_of_measurement(self):
        spec = self._spec()
        if spec is None:
            return None
        hass = getattr(self, "hass", None)
        currency = _gateway_clean_text(
            getattr(getattr(hass, "config", None), "currency", None)
        )
        if currency is not None:
            return f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}"
        return spec.get("unit")

    @property
    def extra_state_attributes(self):
        spec = self._spec()
        if spec is None:
            return {}
        attrs = dict(spec.get("attributes") or {})
        attrs["active_rate_name"] = spec.get("name")
        attrs["configured_rates"] = self._configured_rates()
        attrs.update(self._last_refresh_attr())
        return attrs

    @callback
    def _ensure_tariff_boundary_timer(self) -> None:
        if self.hass is None:
            return
        when = _tariff_now(self._coord, self.hass)
        next_change = next_tariff_rate_change(
            getattr(self._coord, self._rate_attr, None),
            when,
        )
        self._cancel_tariff_boundary_timer()
        if next_change is None:
            return
        fire_at = dt_util.as_utc(next_change)
        if fire_at <= dt_util.utcnow():
            fire_at = dt_util.utcnow() + timedelta(seconds=1)
        self._tariff_boundary_cancel = async_track_point_in_utc_time(
            self.hass, self._handle_tariff_boundary, fire_at
        )

    @callback
    def _handle_tariff_boundary(self, _now: datetime) -> None:
        self._cancel_tariff_boundary_timer()
        self.async_write_ha_state()
        self._ensure_tariff_boundary_timer()

    @callback
    def _cancel_tariff_boundary_timer(self) -> None:
        if self._tariff_boundary_cancel:
            try:
                self._tariff_boundary_cancel()
            except Exception:  # noqa: BLE001
                pass
            self._tariff_boundary_cancel = None


class EnphaseTariffRateValueSensor(_EnphaseTariffBaseSensor):
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, coord: EnphaseCoordinator, spec: dict, *, is_import: bool):
        self._is_import = is_import
        self._rate_prefix = "tariff_import_rate" if is_import else "tariff_export_rate"
        self._rate_attr = "tariff_import_rate" if is_import else "tariff_export_rate"
        label_prefix = "Import Rate" if is_import else "Export Rate"
        self._attr_icon = "mdi:cash-minus" if is_import else "mdi:cash-plus"

        self._detail_key = str(spec.get("key") or "rate")
        detail_name = str(
            spec.get("name") or self._detail_key.replace("_", " ").title()
        )
        name = f"{label_prefix} {detail_name}"
        self._attr_translation_key = f"{self._rate_prefix}_value"
        self._attr_translation_placeholders = {"detail": detail_name}
        super().__init__(
            coord,
            f"{self._rate_prefix}_{self._detail_key}",
            name,
            type_key=None,
        )

    def _spec(self):
        for spec in tariff_rate_sensor_specs(
            getattr(self._coord, self._rate_attr, None)
        ):
            if spec.get("key") == self._detail_key:
                return spec
        return None

    @property
    def available(self) -> bool:
        return self._spec() is not None and super().available

    @property
    def native_value(self):
        spec = self._spec()
        if spec is None:
            return None
        return spec.get("state")

    @property
    def native_unit_of_measurement(self):
        spec = self._spec()
        if spec is None:
            return None
        hass = getattr(self, "hass", None)
        currency = _gateway_clean_text(
            getattr(getattr(hass, "config", None), "currency", None)
        )
        if currency is not None:
            return f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}"
        return spec.get("unit")

    @property
    def extra_state_attributes(self):
        spec = self._spec()
        if spec is None:
            return {}
        attrs = dict(spec.get("attributes") or {})
        attrs.update(self._last_refresh_attr())
        return attrs


class EnphaseTariffExportRateValueSensor(EnphaseTariffRateValueSensor):
    def __init__(self, coord: EnphaseCoordinator, spec: dict):
        super().__init__(coord, spec, is_import=False)


class EnphaseAcBatteryOverallStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "ac_battery_overall_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "ac_battery_overall_status",
            "AC Battery Overall Status",
            type_key="ac_battery",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._coord.ac_battery_aggregate_status is not None

    @property
    def native_value(self):
        return self._coord.ac_battery_aggregate_status

    @property
    def extra_state_attributes(self):
        summary = self._coord.ac_battery_status_summary
        return {
            "battery_count": summary.get("battery_count"),
            "worst_storage_key": summary.get("worst_storage_key"),
            "worst_status": summary.get("worst_status"),
            "sleep_state": summary.get("sleep_state"),
            "sleep_state_map": summary.get("sleep_state_map"),
            "sleep_state_raw": summary.get("sleep_state_raw"),
            "last_command": getattr(self._coord, "_ac_battery_last_command", None),
        }


class EnphaseAcBatteryPowerSensor(_SiteBaseEntity):
    _attr_translation_key = "ac_battery_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "ac_battery_power",
            "AC Battery Power",
            type_key="ac_battery",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self.native_value is not None

    @property
    def native_value(self):
        summary = self._coord.ac_battery_status_summary
        value = summary.get("power_w")
        if value is None:
            return None
        try:
            return round(float(value), 3)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self):
        sampled_at = getattr(self._coord, "ac_battery_summary_sample_utc", None)
        return {
            "sampled_at_utc": (
                sampled_at.isoformat() if sampled_at is not None else None
            ),
            "power_map_w": self._coord.ac_battery_status_summary.get("power_map_w"),
        }


class EnphaseAcBatteryLastReportedSensor(_SiteBaseEntity):
    _attr_translation_key = "ac_battery_last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {"latest_reported_device"}
    )

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "ac_battery_last_reported",
            "AC Battery Last Reported",
            type_key="ac_battery",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        snapshot = ac_battery_last_reported_snapshot(self._coord)
        return snapshot.get("latest_reported") is not None

    @property
    def native_value(self):
        return ac_battery_last_reported_snapshot(self._coord).get("latest_reported")

    @property
    def extra_state_attributes(self):
        snapshot = ac_battery_last_reported_snapshot(self._coord)
        return {
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "without_last_report_count": snapshot.get("without_last_report_count"),
            "total_batteries": snapshot.get("total_batteries"),
        }


class EnphaseBatteryModeSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_mode"
    _attr_icon = "mdi:battery"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "battery_mode", "Battery Mode", type_key="encharge")

    def _mode_raw(self) -> str | None:
        raw_mode = getattr(self._coord, "battery_grid_mode", None)
        if raw_mode is not None:
            return raw_mode
        payload = getattr(self._coord, "battery_status_payload", None)
        if isinstance(payload, dict):
            storages = payload.get("storages")
            if isinstance(storages, list):
                for storage in storages:
                    if not isinstance(storage, dict):
                        continue
                    raw_mode = storage.get("battery_mode")
                    if raw_mode is None:
                        continue
                    try:
                        text = str(raw_mode).strip()
                    except Exception:  # noqa: BLE001
                        continue
                    if text:
                        return text
        return None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self.native_value is not None

    @property
    def native_value(self):
        display = getattr(self._coord, "battery_mode_display", None)
        if display is not None:
            return display
        return self._mode_raw()

    @property
    def extra_state_attributes(self):
        return {
            "mode_raw": self._mode_raw(),
            "charge_from_grid_allowed": self._coord.battery_charge_from_grid_allowed,
            "discharge_to_grid_allowed": self._coord.battery_discharge_to_grid_allowed,
            "shutdown_level": getattr(self._coord, "battery_shutdown_level", None),
            "shutdown_level_min": getattr(
                self._coord, "battery_shutdown_level_min", None
            ),
            "shutdown_level_max": getattr(
                self._coord, "battery_shutdown_level_max", None
            ),
            "hide_charge_from_grid": getattr(
                self._coord, "_battery_hide_charge_from_grid", None
            ),
            "envoy_supports_vls": getattr(
                self._coord, "_battery_envoy_supports_vls", None
            ),
            "use_battery_for_self_consumption": getattr(
                self._coord, "battery_use_battery_for_self_consumption", None
            ),
        }


class EnphaseGridControlStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "grid_control_status"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "grid_control_status",
            "Grid Control Status",
            type_key="enpower",
        )

    @property
    def available(self) -> bool:
        if not _grid_control_site_applicable(self._coord):
            return False
        if not (
            _type_available(self._coord, "enpower")
            or _type_available(self._coord, "envoy")
        ):
            return False
        if self._coord.last_success_utc is not None:
            return True
        return bool(getattr(self._coord, "last_update_success", False))

    @property
    def native_value(self):
        if not self._coord.grid_control_supported:
            return None
        if self._coord.grid_toggle_pending:
            return "pending"
        allowed = self._coord.grid_toggle_allowed
        if allowed is True:
            return "ready"
        if allowed is False:
            return "blocked"
        return None

    @property
    def icon(self) -> str:
        state = self.native_value
        if state == "pending":
            return "mdi:progress-clock"
        if state == "ready":
            return "mdi:check-circle"
        if state == "blocked":
            return "mdi:alert-circle"
        return "mdi:transmission-tower"

    @property
    def extra_state_attributes(self):
        return {
            "grid_toggle_pending": self._coord.grid_toggle_pending,
            "blocked_reasons": self._coord.grid_toggle_blocked_reasons,
            "disable_grid_control": self._coord.grid_control_disable,
            "active_download": self._coord.grid_control_active_download,
            "sunlight_backup_system_check": self._coord.grid_control_sunlight_backup_system_check,
            "grid_outage_check": self._coord.grid_control_grid_outage_check,
            "user_initiated_grid_toggle": self._coord.grid_control_user_initiated_toggle,
        }

    @property
    def device_info(self):
        for type_key in ("enpower", "envoy"):
            info = _type_device_info(self._coord, type_key)
            if info is not None:
                return info
        from homeassistant.helpers.entity import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class EnphaseGridModeSensor(_SiteBaseEntity):
    _attr_translation_key = "grid_mode"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord, "grid_mode", "Grid Mode", type_key="enpower")

    @property
    def available(self) -> bool:
        if not _grid_control_site_applicable(self._coord):
            return False
        if not (
            _type_available(self._coord, "enpower")
            or _type_available(self._coord, "envoy")
        ):
            return False
        if self._coord.last_success_utc is not None:
            return True
        return bool(getattr(self._coord, "last_update_success", False))

    @property
    def native_value(self):
        mode = getattr(self._coord, "grid_mode", None)
        if mode in {"on_grid", "off_grid", "unknown"}:
            return mode
        return "unknown"

    @property
    def extra_state_attributes(self):
        return {
            "raw_states": getattr(self._coord, "grid_mode_raw_states", []),
            "grid_control_supported": self._coord.grid_control_supported,
            "grid_toggle_allowed": self._coord.grid_toggle_allowed,
        }

    @property
    def device_info(self):
        for type_key in ("enpower", "envoy"):
            info = _type_device_info(self._coord, type_key)
            if info is not None:
                return info
        from homeassistant.helpers.entity import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
            manufacturer="Enphase",
        )


class EnphaseSystemProfileStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "system_profile_status"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(
            coord,
            "system_profile_status",
            "System Profile Status",
            type_key="envoy",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._coord.battery_controls_available:
            return True
        return self._coord.battery_profile is not None

    @property
    def native_value(self):
        if self._coord.battery_profile_pending:
            return (
                self._coord.battery_profile_display
                or self._coord.battery_effective_profile_display
            )
        return self._coord.battery_effective_profile_display

    @property
    def extra_state_attributes(self):
        labels = self._coord.battery_profile_option_labels
        attrs = {
            "effective_profile": self._coord.battery_effective_profile,
            "effective_profile_label": self._coord.battery_effective_profile_display,
            "configured_profile": self._coord.battery_profile,
            "live_profile": self._coord.battery_live_profile,
            "live_profile_label": getattr(
                self._coord, "_battery_live_profile_label", None
            ),
            "effective_reserve_percentage": self._coord.battery_effective_backup_percentage,
            "effective_operation_mode_sub_type": self._coord.battery_effective_operation_mode_sub_type,
            "requested_profile": self._coord.battery_pending_profile,
            "requested_profile_label": labels.get(
                self._coord.battery_pending_profile or ""
            ),
            "requested_reserve_percentage": self._coord.battery_pending_backup_percentage,
            "requested_operation_mode_sub_type": self._coord.battery_pending_operation_mode_sub_type,
            "pending": self._coord.battery_profile_pending,
            "pending_requires_exact_settings": getattr(
                self._coord, "_battery_pending_require_exact_settings", None
            ),
            "pending_requested_at": (
                self._coord.battery_pending_requested_at.isoformat()
                if self._coord.battery_pending_requested_at
                else None
            ),
            "selected_profile": self._coord.battery_selected_profile,
            "selected_profile_label": self._coord.battery_profile_display,
            "selected_reserve_percentage": self._coord.battery_selected_backup_percentage,
            "selected_operation_mode_sub_type": self._coord.battery_selected_operation_mode_sub_type,
            "available_profile_keys": self._coord.battery_profile_option_keys,
            "available_profile_labels": labels,
        }
        attrs["supports_mqtt"] = getattr(self._coord, "battery_supports_mqtt", None)
        attrs["polling_interval_seconds"] = getattr(
            self._coord, "battery_profile_polling_interval", None
        )
        attrs["cfg_control_show"] = getattr(
            self._coord, "battery_cfg_control_show", None
        )
        attrs["cfg_control_enabled"] = getattr(
            self._coord, "battery_cfg_control_enabled", None
        )
        attrs["cfg_control_schedule_supported"] = getattr(
            self._coord, "battery_cfg_control_schedule_supported", None
        )
        attrs["cfg_control_force_schedule_supported"] = getattr(
            self._coord, "battery_cfg_control_force_schedule_supported", None
        )
        attrs["cfg_control_locked"] = getattr(
            self._coord, "battery_cfg_control_locked", None
        )
        attrs["cfg_control_show_day_schedule"] = getattr(
            self._coord, "battery_cfg_control_show_day_schedule", None
        )
        attrs["cfg_control_force_schedule_opted"] = getattr(
            self._coord, "battery_cfg_control_force_schedule_opted", None
        )
        attrs["dtg_control"] = getattr(self._coord, "battery_dtg_control", None)
        attrs["cfg_control"] = getattr(self._coord, "battery_cfg_control", None)
        attrs["rbd_control"] = getattr(self._coord, "battery_rbd_control", None)
        attrs["battery_system_task"] = getattr(self._coord, "battery_system_task", None)
        attrs["site_show_production"] = getattr(
            self._coord, "battery_show_production", None
        )
        attrs["site_show_consumption"] = getattr(
            self._coord, "battery_show_consumption", None
        )
        attrs["site_show_charge_from_grid"] = getattr(
            self._coord, "_battery_show_charge_from_grid", None
        )
        attrs["site_show_savings_mode"] = getattr(
            self._coord, "_battery_show_savings_mode", None
        )
        attrs["site_show_full_backup"] = getattr(
            self._coord, "_battery_show_full_backup", None
        )
        attrs["site_show_storm_guard"] = getattr(
            self._coord, "battery_show_storm_guard", None
        )
        attrs["site_show_backup_percentage"] = getattr(
            self._coord, "battery_show_battery_backup_percentage", None
        )
        attrs["site_has_encharge"] = getattr(self._coord, "battery_has_encharge", None)
        attrs["site_has_enpower"] = getattr(self._coord, "battery_has_enpower", None)
        attrs["site_charging_modes_enabled"] = getattr(
            self._coord, "battery_is_charging_modes_enabled", None
        )
        attrs["site_country_code"] = getattr(self._coord, "battery_country_code", None)
        attrs["site_region"] = getattr(self._coord, "battery_region", None)
        attrs["site_locale"] = getattr(self._coord, "battery_locale", None)
        attrs["site_timezone"] = getattr(self._coord, "battery_timezone", None)
        attrs["site_user_is_owner"] = getattr(
            self._coord, "battery_user_is_owner", None
        )
        attrs["site_user_is_installer"] = getattr(
            self._coord, "battery_user_is_installer", None
        )
        attrs["site_status_code"] = getattr(
            self._coord, "battery_site_status_code", None
        )
        attrs["site_status_text"] = getattr(
            self._coord, "battery_site_status_text", None
        )
        attrs["site_status_severity"] = getattr(
            self._coord, "battery_site_status_severity", None
        )
        attrs["feature_details"] = getattr(self._coord, "battery_feature_details", {})
        evse_profile = getattr(self._coord, "battery_profile_evse_device", None)
        if isinstance(evse_profile, dict):
            attrs["evse_profile"] = evse_profile
        return attrs
