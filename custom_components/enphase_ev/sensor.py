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
from typing import Any, cast

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
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import DistanceConverter

from .ac_battery_support import (
    ac_battery_entities_available,
    ac_battery_last_reported_snapshot,
)
from .battery_schedule_editor import (
    BatteryScheduleRecord,
    battery_schedule_inventory,
    battery_scheduler_enabled,
)
from .entity import evse_safe_limit_active, evse_charging_active
from .const import (
    DEFAULT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
    DEFAULT_MICROINVERTER_POWER_ENABLED,
    DEFAULT_NOMINAL_VOLTAGE,
    DEFAULT_VPP_EVENTS_ENABLED,
    DOMAIN,
    OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
    OPT_MICROINVERTER_POWER_ENABLED,
    OPT_VPP_EVENTS_ENABLED,
    PHASE_SWITCH_CONFIG_SETTING,
    SAFE_LIMIT_AMPS,
)
from .coordinator import EnphaseCoordinator
from .device_info_helpers import _cloud_device_info
from .entity import (
    EnphaseBaseEntity,
    evse_amp_control_applicable,
    evse_resolved_charge_mode,
)
from .log_redaction import redact_text
from .grid_profile_runtime import (
    SUPPORT_READ_ONLY,
    SUPPORT_UNKNOWN,
    SUPPORT_UNAVAILABLE,
    GridProfileRuntime,
)
from .scalar_helpers import (
    coerce_snapshot_bool,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data
from .sensor_snapshot_helpers import restore_power_w
from .sensor_base import EnphaseSiteSensorEntity as _SiteBaseEntity
from .sensor_battery import (
    BATTERY_LED_STATUS_STATE_MAP as BATTERY_LED_STATUS_STATE_MAP,
    EnphaseAcBatteryStorageChargeSensor,
    EnphaseAcBatteryStorageCycleCountSensor,
    EnphaseAcBatteryStorageLastReportedSensor,
    EnphaseAcBatteryStorageOperatingModeSensor,
    EnphaseAcBatteryStoragePowerSensor,
    EnphaseAcBatteryStorageStatusSensor,
    EnphaseBatteryStorageChargeSensor,
    EnphaseBatteryStorageCycleCountSensor,
    EnphaseBatteryStorageHealthSensor,
    EnphaseBatteryStorageLastReportedSensor as EnphaseBatteryStorageLastReportedSensor,
    EnphaseBatteryStorageStatusSensor,
    _EnphaseAcBatteryStorageBaseSensor as _EnphaseAcBatteryStorageBaseSensor,
    _EnphaseBatteryStorageBaseSensor as _EnphaseBatteryStorageBaseSensor,
)
from .sensor_heatpump import (
    EnphaseHeatPumpConnectivityStatusSensor,
    EnphaseHeatPumpDailyBatteryEnergySensor,
    EnphaseHeatPumpDailyEnergySensor,
    EnphaseHeatPumpDailyGridEnergySensor,
    EnphaseHeatPumpDailySolarEnergySensor,
    EnphaseHeatPumpEnergyMeterSensor,
    EnphaseHeatPumpLastReportedSensor,
    EnphaseHeatPumpPowerSensor,
    EnphaseHeatPumpSgReadyGatewaySensor,
    EnphaseHeatPumpSgReadyModeSensor,
    EnphaseHeatPumpStatusSensor,
    _heatpump_daily_snapshot as _heatpump_daily_snapshot,
    _heatpump_member_device_type as _heatpump_member_device_type,
    _heatpump_member_last_reported as _heatpump_member_last_reported,
    _heatpump_member_status_text as _heatpump_member_status_text,
    _heatpump_runtime_device_uid as _heatpump_runtime_device_uid,
    _heatpump_runtime_snapshot as _heatpump_runtime_snapshot,
    _heatpump_sg_ready_semantics as _heatpump_sg_ready_semantics,
    _heatpump_snapshot as _heatpump_snapshot,
    _heatpump_type_snapshot as _heatpump_type_snapshot,
    _heatpump_worst_status_text as _heatpump_worst_status_text,
)
from .runtime_helpers import (
    coerce_optional_text as _gateway_clean_text,
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
    normalize_evse_session_energy,
)
from .serial_discovery import (
    active_ac_battery_serials_for_cleanup,
    active_battery_serials_for_cleanup,
    active_charger_serials_for_cleanup,
    active_inverter_serials_for_cleanup,
)
from .sensor_registry import EnphaseSensorRegistrySetup
from .sensor_vpp import VPP_SENSOR_KEYS, vpp_sensor_entities
from .serial_entity_metadata import (
    AC_BATTERY_ENTITY_UNIQUE_SUFFIXES as AC_BATTERY_ENTITY_UNIQUE_SUFFIXES,
    AC_BATTERY_RETIRED_UNIQUE_SUFFIXES as AC_BATTERY_RETIRED_UNIQUE_SUFFIXES,
    BATTERY_ENTITY_UNIQUE_SUFFIXES as BATTERY_ENTITY_UNIQUE_SUFFIXES,
    BATTERY_RETIRED_UNIQUE_SUFFIXES as BATTERY_RETIRED_UNIQUE_SUFFIXES,
    HISTORICAL_CHARGER_SENSOR_UNIQUE_SUFFIXES as HISTORICAL_CHARGER_SENSOR_UNIQUE_SUFFIXES,
)
from . import sensor_battery_helpers as _battery_helpers
from .evse_runtime import evse_power_is_actively_charging


from .sensor_common import (
    _CallbackT as _CallbackT,
    _battery_parse_timestamp as _battery_parse_timestamp,
    _energy_delta_to_power_w as _energy_delta_to_power_w,
    _has_type as _has_type,
    _lifetime_energy_delta as _lifetime_energy_delta,
    _normalize_utc_datetime as _normalize_utc_datetime,
    _resolve_lifetime_power_window as _resolve_lifetime_power_window,
    _restore_optional_float_attribute as _restore_optional_float_attribute,
    _restore_optional_int_value as _restore_optional_int_value,
    _title_case_status as _title_case_status,
    _type_label as _type_label,
    callback as callback,
)
from .sensor_gateway import (
    EnphaseDryContactsInventorySensor as EnphaseDryContactsInventorySensor,
    EnphaseGatewayConnectivityStatusSensor as EnphaseGatewayConnectivityStatusSensor,
    EnphaseGatewayConsumptionMeterSensor as EnphaseGatewayConsumptionMeterSensor,
    EnphaseGatewayIQEnergyRouterSensor as EnphaseGatewayIQEnergyRouterSensor,
    EnphaseGatewayLastReportedSensor as EnphaseGatewayLastReportedSensor,
    EnphaseGatewayProductionMeterSensor as EnphaseGatewayProductionMeterSensor,
    EnphaseSystemControllerInventorySensor as EnphaseSystemControllerInventorySensor,
    _EnphaseGatewayMeterSensor as _EnphaseGatewayMeterSensor,
    _GATEWAY_FIRMWARE_KEYS as _GATEWAY_FIRMWARE_KEYS,
    _GATEWAY_IP_KEYS as _GATEWAY_IP_KEYS,
    _GATEWAY_LAST_REPORT_KEYS as _GATEWAY_LAST_REPORT_KEYS,
    _GATEWAY_MODEL_KEYS as _GATEWAY_MODEL_KEYS,
    _GATEWAY_STATUS_KEYS as _GATEWAY_STATUS_KEYS,
    _NON_ATTR_CHARS_RE as _NON_ATTR_CHARS_RE,
    _SYSTEM_CONTROLLER_TERMINAL_DESCRIPTIONS as _SYSTEM_CONTROLLER_TERMINAL_DESCRIPTIONS,
    _SYSTEM_CONTROLLER_TERMINAL_KEYS as _SYSTEM_CONTROLLER_TERMINAL_KEYS,
    _gateway_attr_key as _gateway_attr_key,
    _gateway_channel_type_kind as _gateway_channel_type_kind,
    _gateway_connectivity_state as _gateway_connectivity_state,
    _gateway_dry_contact_members as _gateway_dry_contact_members,
    _gateway_flat_member_attributes as _gateway_flat_member_attributes,
    _gateway_format_counts as _gateway_format_counts,
    _gateway_inventory_snapshot as _gateway_inventory_snapshot,
    _gateway_ip_member_kind as _gateway_ip_member_kind,
    _gateway_iq_energy_router_identity as _gateway_iq_energy_router_identity,
    _gateway_iq_energy_router_inventory_buckets as _gateway_iq_energy_router_inventory_buckets,
    _gateway_iq_energy_router_last_reported as _gateway_iq_energy_router_last_reported,
    _gateway_iq_energy_router_member_key as _gateway_iq_energy_router_member_key,
    _gateway_iq_energy_router_record as _gateway_iq_energy_router_record,
    _gateway_iq_energy_router_records as _gateway_iq_energy_router_records,
    _gateway_member_ip_address as _gateway_member_ip_address,
    _gateway_member_is_dry_contact as _gateway_member_is_dry_contact,
    _gateway_member_preferred_for_ip as _gateway_member_preferred_for_ip,
    _gateway_meter_last_reported as _gateway_meter_last_reported,
    _gateway_meter_member as _gateway_meter_member,
    _gateway_meter_status_text as _gateway_meter_status_text,
    _gateway_normalize_status as _gateway_normalize_status,
    _gateway_summary_ip_address as _gateway_summary_ip_address,
    _gateway_system_controller_member as _gateway_system_controller_member,
    _gateway_terminal_descriptions as _gateway_terminal_descriptions,
    _gateway_terminal_key as _gateway_terminal_key,
    _gateway_terminal_values as _gateway_terminal_values,
    _is_dry_contact_type_key as _is_dry_contact_type_key,
)
from .sensor_inverter import (
    EnphaseInverterLifetimeEnergySensor as EnphaseInverterLifetimeEnergySensor,
    EnphaseInverterTelemetrySensor as EnphaseInverterTelemetrySensor,
    EnphaseMicroinverterConnectivityStatusSensor as EnphaseMicroinverterConnectivityStatusSensor,
    EnphaseMicroinverterLastReportedSensor as EnphaseMicroinverterLastReportedSensor,
    EnphaseMicroinverterReportingCountSensor as EnphaseMicroinverterReportingCountSensor,
    _microinverter_connectivity_state as _microinverter_connectivity_state,
    _microinverter_inventory_snapshot as _microinverter_inventory_snapshot,
)
from .sensor_site_energy import (
    CURRENT_POWER_CACHE_TTL_MULTIPLIER as CURRENT_POWER_CACHE_TTL_MULTIPLIER,
    EnphaseBatteryPowerSensor as EnphaseBatteryPowerSensor,
    EnphaseCurrentPowerConsumptionSensor as EnphaseCurrentPowerConsumptionSensor,
    EnphaseGridPowerSensor as EnphaseGridPowerSensor,
    EnphaseSiteConsumptionPowerSensor as EnphaseSiteConsumptionPowerSensor,
    EnphaseSiteEnergySensor as EnphaseSiteEnergySensor,
    SITE_LIFETIME_FLOW_BUCKET_LENGTH_KEYS as SITE_LIFETIME_FLOW_BUCKET_LENGTH_KEYS,
    _EnphaseSiteLifetimePowerSensor as _EnphaseSiteLifetimePowerSensor,
    _SiteConsumptionPowerRestoreData as _SiteConsumptionPowerRestoreData,
    _SiteEnergyRestoreData as _SiteEnergyRestoreData,
    _SiteLifetimePowerRestoreData as _SiteLifetimePowerRestoreData,
)
from .sensor_tariff import (
    EnphaseCurrentTariffRateSensor as EnphaseCurrentTariffRateSensor,
    EnphaseTariffBillingSensor as EnphaseTariffBillingSensor,
    EnphaseTariffExportRateValueSensor as EnphaseTariffExportRateValueSensor,
    EnphaseTariffRateSensor as EnphaseTariffRateSensor,
    EnphaseTariffRateValueSensor as EnphaseTariffRateValueSensor,
    _EnphaseTariffBaseSensor as _EnphaseTariffBaseSensor,
    _tariff_data_available as _tariff_data_available,
    _tariff_now as _tariff_now,
)

from .sensor_snapshot_helpers import parse_gateway_timestamp
from .scalar_helpers import coerce_optional_bool

_gateway_optional_bool = coerce_optional_bool
_gateway_parse_timestamp = parse_gateway_timestamp

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


def _retain_grid_profile_sensors(coord: EnphaseCoordinator) -> bool:
    if not getattr(coord, "grid_profile_controls_enabled", False):
        return False
    runtime = getattr(coord, "grid_profile_runtime", None)
    if runtime is None:
        return False
    if getattr(runtime, "installer_access_confirmed", False):
        return True
    if getattr(runtime, "support_state", None) == SUPPORT_READ_ONLY:
        return bool(runtime.current_profile_display())
    return bool(
        getattr(runtime, "installer_access_ever_confirmed", False)
        and getattr(runtime, "support_state", None) == SUPPORT_UNAVAILABLE
    )


_battery_last_reported_members = _battery_helpers.battery_last_reported_members
_battery_last_reported_snapshot = _battery_helpers.battery_last_reported_snapshot
_battery_optional_bool = _battery_helpers.battery_optional_bool
_battery_snapshot_last_reported = _battery_helpers.battery_snapshot_last_reported


def _ac_battery_status_fallback_serials_for_setup(
    coord: EnphaseCoordinator,
) -> set[str] | None:
    """Return AC Battery serials seeded by battery status for non-destructive setup."""

    if not ac_battery_entities_available(coord):
        return None
    details = getattr(coord, "ac_battery_status_summary", None)
    if (
        not isinstance(details, dict)
        or details.get("status_source") != "battery_status"
    ):
        return None
    iter_ac_batteries = getattr(coord, "iter_ac_battery_serials", None)
    if not callable(iter_ac_batteries):
        return None
    try:
        return {
            serial for sn in iter_ac_batteries() if sn and (serial := str(sn).strip())
        }
    except Exception:  # noqa: BLE001
        return None


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    ent_reg = er.async_get(hass)
    registry_setup = EnphaseSensorRegistrySetup(
        ent_reg,
        config_entry_id=entry.entry_id,
        site_id=str(coord.site_id),
    )
    microinverter_lifetime_energy_enabled = bool(
        entry.options.get(
            OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
            DEFAULT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
        )
    )
    microinverter_power_enabled = bool(
        entry.options.get(
            OPT_MICROINVERTER_POWER_ENABLED,
            DEFAULT_MICROINVERTER_POWER_ENABLED,
        )
    )
    registry_setup.sync_inverter_sensor_enabled_defaults(
        lifetime_energy_enabled=(
            microinverter_lifetime_energy_enabled
            if OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED in entry.options
            else None
        ),
        power_enabled=(
            microinverter_power_enabled
            if OPT_MICROINVERTER_POWER_ENABLED in entry.options
            else None
        ),
    )
    known_site_entity_keys = registry_setup.known_site_entity_keys
    known_type_keys = registry_setup.known_type_keys
    known_gateway_iq_router_keys = registry_setup.known_gateway_iq_router_keys
    _gateway_iq_router_entity_key = registry_setup.gateway_iq_router_entity_key
    _async_prune_removed_gateway_iq_router_entities = (
        registry_setup.prune_removed_gateway_iq_router_entities
    )
    _async_remove_site_sensor_entity = registry_setup.remove_site_sensor_entity
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
    last_entity_shape_signature: tuple[object, ...] | None = None
    last_inverter_telemetry_set: set[str] | None = None

    @callback
    def _async_sync_site_entities() -> None:
        site_entities: list[SensorEntity] = []
        site_has_battery = _site_has_battery(coord)
        gateway_available = _type_available(coord, "envoy")
        battery_device_available = _type_available(coord, "encharge")
        ac_battery_device_available = ac_battery_entities_available(coord)
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        battery_schedules_enabled = battery_scheduler_enabled(entry)
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
        battery_schedule_sensor_keys: tuple[str, ...] = (
            "battery_cfg_schedule_status",
            "battery_schedule_summary",
            "battery_cfg_schedules",
            "battery_dtg_schedules",
            "battery_rbd_schedules",
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
                    if int(bucket_length) > 0:  # type: ignore[arg-type]
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
        if _retain_grid_profile_sensors(coord):
            _add_site_entity(
                "current_grid_profile",
                EnphaseCurrentGridProfileSensor(coord),
            )
        elif getattr(
            getattr(coord, "grid_profile_runtime", None), "support_state", None
        ) not in {SUPPORT_UNKNOWN, SUPPORT_UNAVAILABLE}:
            _async_remove_site_sensor_entity("current_grid_profile")
        _async_remove_site_sensor_entity("grid_profile_status")
        _async_remove_site_sensor_entity("requested_grid_profile")
        _add_site_entity(
            "current_production_power",
            EnphaseCurrentPowerConsumptionSensor(coord),
        )
        if _site_lifetime_power_channel_present("consumption"):
            _add_site_entity(
                "site_consumption_power",
                EnphaseSiteConsumptionPowerSensor(coord),
            )
        else:
            _async_remove_site_sensor_entity("site_consumption_power")
        if _site_lifetime_power_channel_present(
            "grid_import"
        ) or _site_lifetime_power_channel_present("grid_export"):
            _add_site_entity("grid_power", EnphaseGridPowerSensor(coord))
        else:
            _async_remove_site_sensor_entity("grid_power")
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

        for record in router_records:
            router_key = str(record.get("key", "")).strip()
            if not router_key:
                continue
            current_router_keys.add(router_key)
            entity_key = _gateway_iq_router_entity_key(router_key)
            if entity_key in known_site_entity_keys:
                continue
            try:
                index = int(record.get("index", 0))  # type: ignore[call-overload]
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
        elif inventory_ready:
            _async_remove_site_sensor_entity("grid_mode")
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
                "battery_available_energy", EnphaseBatteryAvailableEnergySensor(coord)
            )
            _add_site_entity(
                "battery_available_power", EnphaseBatteryAvailablePowerSensor(coord)
            )
            _add_site_entity(
                "battery_last_reported",
                EnphaseBatteryLastReportedSensor(coord),
            )
            if battery_schedules_enabled:
                _add_site_entity(
                    "battery_cfg_schedule_status",
                    EnphaseBatteryCfgScheduleStatusSensor(coord),
                )
            else:
                for entity_key in battery_schedule_sensor_keys:
                    _async_remove_site_sensor_entity(entity_key)
            if battery_schedules_enabled and _battery_schedule_inventory_supported(
                coord
            ):
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
            elif battery_schedules_enabled and inventory_ready:
                for entity_key in (
                    "battery_schedule_summary",
                    "battery_cfg_schedules",
                    "battery_dtg_schedules",
                    "battery_rbd_schedules",
                ):
                    _async_remove_site_sensor_entity(entity_key)
        else:
            _async_remove_site_sensor_entity("battery_power")
            for entity_key in battery_schedule_sensor_keys:
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
        vpp_enabled = bool(
            entry.options.get(OPT_VPP_EVENTS_ENABLED, DEFAULT_VPP_EVENTS_ENABLED)
        )
        vpp_runtime = getattr(coord, "vpp_runtime", None)
        vpp_registered = any(
            _site_sensor_entity_registered(key) for key in VPP_SENSOR_KEYS
        )
        if (
            vpp_enabled
            and vpp_runtime is not None
            and vpp_runtime.enrollment_state != "unenrolled"
            and (vpp_runtime.available or vpp_registered)
        ):
            for key, entity in zip(
                VPP_SENSOR_KEYS,
                vpp_sensor_entities(coord),
                strict=True,
            ):
                _add_site_entity(key, entity)
        elif (
            not vpp_enabled
            or vpp_runtime is None
            or vpp_runtime.enrollment_state == "unenrolled"
        ):
            for key in VPP_SENSOR_KEYS:
                _async_remove_site_sensor_entity(key)
        if site_entities:
            async_add_entities(site_entities, update_before_add=False)

    @callback
    def _async_sync_type_inventory() -> None:
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
        active_charger_serials = active_charger_serials_for_cleanup(coord)
        if active_charger_serials is not None:
            registry_setup.prune_removed_charger_sensor_entities(active_charger_serials)
            registry_setup.remove_missing_charger_entities(active_charger_serials)
            known_serials.intersection_update(active_charger_serials)
            registry_setup.known_charger_serials.intersection_update(
                active_charger_serials
            )
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
            registry_setup.known_charger_serials.update(serials)

    @callback
    def _async_sync_batteries() -> None:
        active_battery_serials = active_battery_serials_for_cleanup(coord)
        if active_battery_serials is None:
            return
        current_serials = sorted(active_battery_serials)
        current_set = active_battery_serials

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
        active_ac_battery_serials = active_ac_battery_serials_for_cleanup(coord)
        cleanup_authoritative = active_ac_battery_serials is not None
        if active_ac_battery_serials is None:
            active_ac_battery_serials = _ac_battery_status_fallback_serials_for_setup(
                coord
            )
            if active_ac_battery_serials is None:
                return
        current_serials = sorted(active_ac_battery_serials)
        current_set = active_ac_battery_serials

        if cleanup_authoritative:
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
        active_inverter_serials = active_inverter_serials_for_cleanup(coord)
        if active_inverter_serials is None:
            return
        current_serials = sorted(active_inverter_serials)
        current_set = active_inverter_serials

        registry_setup.prune_inverter_registry_once(current_set)
        registry_setup.remove_missing_inverter_entities(current_set)

        serials = [
            sn
            for sn in current_serials
            if sn not in registry_setup.known_inverter_serials
        ]
        if serials:
            entities = [
                EnphaseInverterLifetimeEnergySensor(
                    coord,
                    sn,
                    enabled_default=microinverter_lifetime_energy_enabled,
                )
                for sn in serials
            ]
            async_add_entities(entities, update_before_add=False)
            registry_setup.known_inverter_serials.update(serials)
        telemetry_serials = [
            sn
            for sn in current_serials
            if sn not in registry_setup.known_inverter_telemetry_serials
            and isinstance(coord.inverter_data(sn), dict)
            and bool((coord.inverter_data(sn) or {}).get("telemetry"))
        ]
        if telemetry_serials:
            async_add_entities(
                [
                    EnphaseInverterTelemetrySensor(
                        coord,
                        sn,
                        enabled_default=microinverter_power_enabled,
                    )
                    for sn in telemetry_serials
                ],
                update_before_add=False,
            )
            registry_setup.known_inverter_telemetry_serials.update(telemetry_serials)

    @callback
    def _async_sync_topology() -> None:
        nonlocal last_entity_shape_signature
        nonlocal last_type_key_set
        nonlocal last_battery_serial_set
        nonlocal last_ac_battery_serial_set
        nonlocal last_charger_serial_set
        nonlocal last_inverter_serial_set
        nonlocal last_inverter_telemetry_set

        current_type_keys = {
            key for key in coord.inventory_view.iter_type_keys() if key
        }
        current_battery_serials = active_battery_serials_for_cleanup(coord)
        current_ac_battery_serials = active_ac_battery_serials_for_cleanup(coord)
        if current_ac_battery_serials is None:
            current_ac_battery_serials = _ac_battery_status_fallback_serials_for_setup(
                coord
            )
        current_charger_serials = active_charger_serials_for_cleanup(coord)
        if current_charger_serials is None:
            current_charger_serials = {sn for sn in coord.iter_serials() if sn}
        current_inverter_serials = active_inverter_serials_for_cleanup(coord)
        current_inverter_telemetry = (
            {
                serial
                for serial in current_inverter_serials
                if bool((coord.inverter_data(serial) or {}).get("telemetry"))
            }
            if current_inverter_serials is not None
            else None
        )

        # The dedicated topology signal also covers router membership, which is
        # intentionally absent from the cheap coordinator-update signature.
        _async_sync_site_entities()
        last_entity_shape_signature = _entity_shape_signature()
        if current_type_keys != last_type_key_set:
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
        if (
            current_inverter_serials != last_inverter_serial_set
            or current_inverter_telemetry != last_inverter_telemetry_set
        ):
            _async_sync_inverters()
            last_inverter_serial_set = current_inverter_serials
            last_inverter_telemetry_set = current_inverter_telemetry

    def _entity_shape_signature() -> tuple[object, ...]:
        """Return inexpensive state that controls site-level entity presence."""

        energy = getattr(coord, "energy", None)
        site_energy = (
            getattr(energy, "site_energy", None)
            if energy is not None
            else getattr(coord, "site_energy", None)
        )
        site_energy_meta = (
            getattr(energy, "site_energy_meta", None)
            if energy is not None
            else getattr(coord, "site_energy_meta", None)
        )
        bucket_lengths = (
            site_energy_meta.get("bucket_lengths")
            if isinstance(site_energy_meta, dict)
            else None
        )
        populated_bucket_keys = (
            frozenset(key for key, value in bucket_lengths.items() if value)
            if isinstance(bucket_lengths, dict)
            else frozenset()
        )
        gateway_meter_shape: tuple[bool | None, bool | None, bool | None]
        try:
            gateway_meter_shape = (
                _gateway_meter_member(coord, "production") is not None,
                _gateway_meter_member(coord, "consumption") is not None,
                bool(_gateway_dry_contact_members(coord)),
            )
        except Exception:  # noqa: BLE001
            gateway_meter_shape = (None, None, None)
        known_channels: tuple[bool, ...] = ()
        channel_known = getattr(
            getattr(coord, "discovery_snapshot", None),
            "site_energy_channel_known",
            None,
        )
        if callable(channel_known):
            values: list[bool] = []
            for flow_key in SITE_LIFETIME_FLOW_BUCKET_LENGTH_KEYS:
                try:
                    values.append(bool(channel_known(flow_key)))
                except Exception:  # noqa: BLE001
                    values.append(False)
            known_channels = tuple(values)
        vpp_runtime = getattr(coord, "vpp_runtime", None)
        return (
            bool(getattr(coord, "_devices_inventory_ready", False)),
            _site_has_battery(coord),
            getattr(coord, "battery_has_enpower", None),
            bool(getattr(coord, "include_inverters", True)),
            _type_available(coord, "envoy"),
            _type_available(coord, "encharge"),
            _type_available(coord, "ac_battery"),
            _type_available(coord, "microinverter"),
            _type_available(coord, "heatpump"),
            _type_available(coord, "enpower"),
            _heatpump_runtime_device_uid(coord),
            battery_scheduler_enabled(entry),
            bool(
                entry.options.get(
                    OPT_VPP_EVENTS_ENABLED,
                    DEFAULT_VPP_EVENTS_ENABLED,
                )
            ),
            getattr(vpp_runtime, "enrollment_state", "disabled"),
            bool(getattr(vpp_runtime, "available", False)),
            _battery_schedule_inventory_supported(coord),
            _grid_control_site_applicable(coord),
            getattr(coord, "tariff_billing", None) is not None,
            getattr(coord, "tariff_import_rate", None) is not None,
            getattr(coord, "tariff_export_rate", None) is not None,
            getattr(coord, "tariff_rates_last_refresh_utc", None) is not None,
            frozenset(site_energy) if isinstance(site_energy, dict) else frozenset(),
            populated_bucket_keys,
            known_channels,
            gateway_meter_shape,
        )

    @callback
    def _async_sync_capabilities() -> None:
        """Reconcile site entities only when their capability shape changes."""

        nonlocal last_entity_shape_signature
        signature = _entity_shape_signature()
        if signature == last_entity_shape_signature:
            return
        _async_sync_site_entities()
        last_entity_shape_signature = signature

    add_topology_listener = getattr(coord, "async_add_topology_listener", None)
    has_topology_listener = callable(add_topology_listener)
    if not has_topology_listener:
        add_topology_listener = getattr(coord, "async_add_listener", None)
    if callable(add_topology_listener):
        entry.async_on_unload(add_topology_listener(_async_sync_topology))
    add_coordinator_listener = getattr(coord, "async_add_listener", None)
    if has_topology_listener and callable(add_coordinator_listener):
        entry.async_on_unload(add_coordinator_listener(_async_sync_capabilities))
    # One-time migrations and retired-entity cleanup must not scan the registry on
    # every coordinator update.
    _async_prune_dry_contact_type_inventory_entities()
    _async_prune_blocked_type_inventory_entities({"encharge"})
    _async_remove_site_sensor_entity("current_power_consumption")
    _async_remove_site_sensor_entity("grid_import_power")
    _async_remove_site_sensor_entity("grid_export_power")
    _async_remove_site_sensor_entity("tariff_import_rate")
    _async_remove_site_sensor_entities_with_prefix("tariff_import_rate_")
    _async_remove_site_sensor_entity("tariff_export_rate")
    _async_remove_site_sensor_entities_with_prefix("tariff_export_rate_")
    registry_setup.prune_historical_charger_sensor_entities()
    registry_setup.prune_removed_site_entities()
    _async_sync_topology()


class _BaseEVSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
    def __init__(self, coord: EnphaseCoordinator, sn: str, key: str) -> None:
        super().__init__(coord, sn)
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{sn}_{key}"

    @property
    def native_value(self) -> Any:
        return self.data.get(self._key)


class EnphaseElectricalPhaseSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "electrical_phase"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_electrical_phase"

    @staticmethod
    def _friendly_phase_mode(raw: object) -> tuple[str | None, object | None]:
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
    def _as_bool(value: object) -> bool | None:
        if value is None:
            return None
        try:
            return bool(value)
        except Exception:  # noqa: BLE001
            return None

    @property
    def native_value(self) -> Any:
        friendly, _ = self._friendly_phase_mode(self.data.get("phase_mode"))
        return friendly

    @property
    def extra_state_attributes(self) -> Any:
        _, phase_raw = self._friendly_phase_mode(self.data.get("phase_mode"))
        return {
            "phase_mode_raw": phase_raw,
            PHASE_SWITCH_CONFIG_SETTING: self.data.get(PHASE_SWITCH_CONFIG_SETTING),
            "dlb_enabled": self._as_bool(self.data.get("dlb_enabled")),
            "dlb_active": self._as_bool(self.data.get("dlb_active")),
        }


@dataclass
class _LastSessionRestoreData(ExtraStoredData):  # type: ignore[misc]
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
    def from_dict(cls, data: dict[str, Any] | None) -> "_LastSessionRestoreData":
        if not isinstance(data, dict):
            return cls(None, None, None, None, None, None)

        def _as_float(val: Any) -> float | None:
            try:
                return float(val) if val is not None else None
            except Exception:  # noqa: BLE001
                return None

        def _as_int(val: Any) -> int | None:
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
class _PowerRestoreData(ExtraStoredData):  # type: ignore[misc]
    """Persist EV charger derived-power state without recorder attributes."""

    last_lifetime_kwh: float | None
    last_energy_ts: float | None
    last_sample_ts: float | None
    last_power_w: int | None
    last_window_seconds: float | None
    method: str | None
    last_reset_at: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "last_lifetime_kwh": self.last_lifetime_kwh,
            "last_energy_ts": self.last_energy_ts,
            "last_sample_ts": self.last_sample_ts,
            "last_power_w": self.last_power_w,
            "last_window_seconds": self.last_window_seconds,
            "method": self.method,
            "last_reset_at": self.last_reset_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "_PowerRestoreData":
        if not isinstance(data, dict):
            return cls(None, None, None, None, None, None, None)

        def _as_float(value: object) -> float | None:
            try:
                return float(value) if value is not None else None  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        def _as_int(value: object) -> int | None:
            try:
                return int(float(value)) if value is not None else None  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        method = data.get("method")
        return cls(
            last_lifetime_kwh=_as_float(data.get("last_lifetime_kwh")),
            last_energy_ts=_as_float(data.get("last_energy_ts")),
            last_sample_ts=_as_float(data.get("last_sample_ts")),
            last_power_w=_as_int(data.get("last_power_w")),
            last_window_seconds=_as_float(data.get("last_window_seconds")),
            method=str(method) if method not in (None, "") else None,
            last_reset_at=_as_float(data.get("last_reset_at")),
        )


class EnphaseEnergyTodaySensor(EnphaseBaseEntity, SensorEntity, RestoreEntity):  # type: ignore[misc]
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

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        # Preserve unique_id for continuity even though the semantics changed
        self._attr_unique_id = f"{DOMAIN}_{sn}_energy_today"
        self._last_session_kwh: float | None = None
        self._last_session_wh: float | None = None
        self._last_session_start: float | None = None
        self._last_session_end: float | None = None
        self._last_duration_min: int | None = None
        self._session_key: str | None = None
        self._last_context: dict[str, Any] | None = None
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
                    self._last_duration_min = int(attrs.get("session_duration_min"))  # type: ignore[arg-type]
                except Exception:
                    self._last_duration_min = None

    @staticmethod
    def _coerce_timestamp(value: object) -> float | None:
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
    def _coerce_energy(
        session_kwh: Any, session_wh: Any
    ) -> tuple[float | None, float | None]:
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

    def _extract_realtime_session(self, data: dict[str, Any]) -> dict[str, Any]:
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

    def _extract_history_session(self, data: dict[str, Any]) -> dict[str, Any] | None:
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

    def _pick_session_context(self, data: dict[str, Any]) -> dict[str, Any] | None:
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

    def _merge_history_context(self, context: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(context or {})
        history = self._extract_history_session(self.data)
        if not history:
            return merged

        def _as_float(value: Any) -> float | None:
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
    def native_value(self) -> Any:
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
    def extra_state_attributes(self) -> Any:
        merged_context = self._merge_history_context(self._last_context)
        return self._session_metadata_attributes(
            self.data,
            hass=self.hass,
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
        data: dict[str, Any],
        hass: HomeAssistant | None = None,
        *,
        context: dict[str, Any] | None = None,
        energy_kwh: float | None = None,
        energy_wh: float | None = None,
        duration_min: int | None = None,
        session_key: str | None = None,
    ) -> dict[str, object]:
        """Derive session metadata attributes from the coordinator payload."""
        result: dict[str, object] = {}

        def _localize(value: object) -> str | None:
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
                return dt_util.as_local(dt).isoformat(timespec="seconds")  # type: ignore[no-any-return]
            except Exception:  # noqa: BLE001
                return None

        def _as_bool(value: object) -> bool | None:
            if value is None:
                return None
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes", "y")
            return None

        def _as_int(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(float(value))
            except Exception:  # noqa: BLE001
                return None

        def _as_float(value: Any, *, precision: int | None = None) -> float | None:
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
                    preferred_unit = units.length_unit
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

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn, "connector_status")

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
    def extra_state_attributes(self) -> Any:
        def _clean(val: object) -> str | None:
            if val in (None, ""):
                return None
            if isinstance(val, str):
                cleaned = val.strip()
                return cleaned or None
            try:
                text = str(val)
            except Exception:  # noqa: BLE001
                return val  # type: ignore[return-value]
            return text.strip() or None

        return {
            "status_reason": _clean(self.data.get("connector_reason")),
            "connector_status_info": _clean(self.data.get("connector_status_info")),
        }


class EnphasePowerSensor(EnphaseBaseEntity, SensorEntity, RestoreEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_translation_key = "power"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.POWER

    _DEFAULT_WINDOW_S = 300  # 5 minutes
    _MIN_DELTA_KWH = 0.0005  # 0.5 Wh jitter guard
    _RESET_DROP_KWH = 0.25  # minimum backward delta treated as a meter reset
    _STATIC_MAX_WATTS = 19200  # IQ EV Charger 2 max continuous throughput (~80A @ 240V)

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
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
        last_extra = await self.async_get_last_extra_data()
        restored = _PowerRestoreData.from_dict(
            last_extra.as_dict() if last_extra is not None else None
        )
        self._last_lifetime_kwh = restored.last_lifetime_kwh
        self._last_energy_ts = restored.last_energy_ts
        self._last_sample_ts = restored.last_sample_ts
        if restored.last_power_w is not None:
            self._last_power_w = restored.last_power_w
        self._last_window_s = restored.last_window_seconds
        if restored.method is not None:
            self._last_method = restored.method
        self._last_reset_at = restored.last_reset_at

        last_state = await self.async_get_last_state()
        if not last_state:
            return
        attrs = last_state.attributes or {}
        if self._last_lifetime_kwh is None:
            self._last_lifetime_kwh = _restore_optional_float_attribute(
                attrs, "last_lifetime_kwh"
            )
        if self._last_energy_ts is None:
            self._last_energy_ts = _restore_optional_float_attribute(
                attrs, "last_energy_ts"
            )
        if self._last_sample_ts is None:
            self._last_sample_ts = _restore_optional_float_attribute(
                attrs, "last_sample_ts"
            )
        restored_power = restored.last_power_w
        if restored_power is None:
            restored_power = _restore_optional_int_value(attrs.get("last_power_w"))
        if restored_power is None:
            restored_power = restore_power_w(last_state)
        if restored_power is not None:
            self._last_power_w = restored_power
        if self._last_window_s is None:
            self._last_window_s = _restore_optional_float_attribute(
                attrs, "last_window_seconds"
            )
        if restored.method is None and attrs.get("method"):
            self._last_method = str(attrs.get("method"))
        if self._last_reset_at is None:
            self._last_reset_at = _restore_optional_float_attribute(
                attrs, "last_reset_at"
            )

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
                        self._last_energy_ts = float(attrs.get("last_ts"))  # type: ignore[arg-type]
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
    def _as_float(val: Any) -> float | None:
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(val: Any) -> int | None:
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _power_topology(cls, data: dict[str, Any]) -> str:
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
    def _three_phase_multiplier(cls, data: dict[str, Any]) -> float:
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
    def _is_actually_charging(data: dict[str, Any]) -> bool:
        if "actual_charging" in data:
            return bool(data.get("actual_charging"))
        return evse_power_is_actively_charging(
            data.get("connector_status"),
            data.get("charging"),
            suspended_by_evse=data.get("suspended_by_evse"),
        )

    def _resolve_max_throughput(
        self, data: dict[str, Any]
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

    def _apply_derived_snapshot(self, data: dict[str, Any]) -> bool:
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
    def native_value(self) -> Any:
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
        if delta_kwh <= self._MIN_DELTA_KWH:  # type: ignore[operator]
            return self._last_power_w

        window_s = _resolve_lifetime_power_window(
            sample_ts=sample_ts,
            previous_energy_ts=self._last_energy_ts,
            default_window_s=self._DEFAULT_WINDOW_S,
        )
        self._last_power_w = _energy_delta_to_power_w(
            delta_kwh,  # type: ignore[arg-type]
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
    def extra_state_attributes(self) -> Any:
        data = self.data
        actual_charging = self._is_actually_charging(data)
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
            "last_window_seconds": self._last_window_s,
            "method": self._last_method,
            "actual_charging": actual_charging,
        }

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        return _PowerRestoreData(
            last_lifetime_kwh=self._last_lifetime_kwh,
            last_energy_ts=self._last_energy_ts,
            last_sample_ts=self._last_sample_ts,
            last_power_w=self._last_power_w,
            last_window_seconds=self._last_window_s,
            method=self._last_method,
            last_reset_at=self._last_reset_at,
        )


class EnphaseChargingLevelSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "set_amps"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_suggested_display_precision = 0

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charging_amps"

    @property
    def available(self) -> bool:
        return super().available and evse_amp_control_applicable(self._coord, self._sn)

    _safe_limit_active = staticmethod(evse_safe_limit_active)

    _charging_active = staticmethod(evse_charging_active)

    _optional_bool = staticmethod(coerce_snapshot_bool)

    @property
    def native_value(self) -> Any:
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
    def _coerce_amp(value: object) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(float(str(value).strip()))
        except Exception:  # noqa: BLE001
            return None

    @classmethod
    def _safe_limit_amps(cls, data: dict[str, Any]) -> int:
        min_amp = cls._coerce_amp(data.get("min_amp"))
        if min_amp is not None and min_amp > 0:
            return min_amp
        return SAFE_LIMIT_AMPS

    @property
    def extra_state_attributes(self) -> Any:
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


class EnphaseLastReportedSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "last_reported"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_last_rpt"

    @property
    def available(self) -> bool:
        return bool(super().available and self.native_value is not None)

    @property
    def native_value(self) -> Any:
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
    def extra_state_attributes(self) -> Any:
        def _as_int(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(str(value).strip())
            except Exception:  # noqa: BLE001
                return None

        def _as_bool(value: object) -> bool | None:
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

        def _clean_text(value: object) -> str | None:
            if value in (None, ""):
                return None
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                return None
            return text or None

        return {
            "reporting_interval": _as_int(self.data.get("reporting_interval")),
            "connection": _clean_text(self.data.get("connection")),
            "is_connected": _as_bool(self.data.get("is_connected")),
        }


class EnphaseChargeModeSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "charge_mode"

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charge_mode"

    @property
    def native_value(self) -> Any:
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
    def _as_bool(value: object) -> bool | None:
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
    def extra_state_attributes(self) -> Any:
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


class EnphaseStormGuardStateSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "storm_guard_state"

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_storm_guard_state"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if bool(getattr(self._coord, "storm_guard_update_pending", False)):
            return True
        return self.data.get("storm_guard_state") is not None

    @property
    def native_value(self) -> Any:
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


class EnphaseChargerAuthenticationSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "charger_authentication"

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_charger_authentication"

    @property
    def available(self) -> bool:
        return super().available and self._coord.auth_settings_available

    @property
    def native_value(self) -> Any:
        required = self.data.get("auth_required")
        if required is True:
            return "enabled"
        if required is False:
            return "disabled"
        return None

    @staticmethod
    def _as_bool(value: object) -> bool | None:
        if value is None:
            return None
        try:
            return bool(value)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self) -> Any:
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


class EnphaseLifetimeEnergySensor(EnphaseBaseEntity, RestoreSensor):  # type: ignore[misc]
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

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
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
                    self._last_reset_value = float(attrs.get("last_reset_value"))  # type: ignore[arg-type]
            except Exception:
                self._last_reset_value = None
            reset_at_attr = attrs.get("last_reset_at")
            if isinstance(reset_at_attr, str):
                self._last_reset_at = reset_at_attr

    @property
    def native_value(self) -> Any:
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
    def extra_state_attributes(self) -> Any:
        return {
            "sampled_at_utc": self.data.get("sampled_at_utc"),
            "last_reset_value": self._last_reset_value,
            "last_reset_at": self._last_reset_at,
        }


class EnphaseStatusSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_status"

    @staticmethod
    def _normalize_status(value: object) -> str:
        if value is None:
            return None  # type: ignore[return-value]
        try:
            raw = str(value).strip()
        except Exception:  # noqa: BLE001
            return None  # type: ignore[return-value]
        if not raw:
            return None  # type: ignore[return-value]
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
            return None  # type: ignore[return-value]
        return " ".join(normalized_parts)

    @property
    def native_value(self) -> Any:
        return self._normalize_status(self.data.get("status"))

    @property
    def extra_state_attributes(self) -> Any:
        def _as_bool(value: object) -> bool | None:
            if value is None:
                return None
            try:
                return bool(value)
            except Exception:  # noqa: BLE001
                return None

        def _as_text(value: object) -> str | None:
            if value in (None, ""):
                return None
            try:
                text = str(value).strip()
            except Exception:  # noqa: BLE001
                return None
            return text or None

        def _localize(value: object) -> str | None:
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
                return dt_util.as_local(dt).isoformat(timespec="seconds")  # type: ignore[no-any-return]
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


class _TimestampFromIsoSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
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
    def native_value(self) -> Any:
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


class _TimestampFromEpochSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
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
    def native_value(self) -> Any:
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


class EnphaseTypeInventorySensor(CoordinatorEntity, SensorEntity):  # type: ignore[misc]
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
    def native_value(self) -> Any:
        bucket = self._coord.inventory_view.type_bucket(self._type_key) or {}
        try:
            count = int(cast(Any, bucket.get("count", 0)))
        except Exception:
            count = 0
        return count or self._fallback_count()

    @property
    def extra_state_attributes(self) -> Any:
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
    def device_info(self) -> Any:
        from homeassistant.helpers.entity import DeviceInfo

        info = self._coord.inventory_view.type_device_info(self._type_key)
        if info is not None:
            return info
        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:{self._type_key}")},
            manufacturer="Enphase",
        )


class EnphaseSiteLastUpdateSensor(_SiteBaseEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_successful_update"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "last_update", "Last Successful Update", type_key=None)

    @property
    def native_value(self) -> Any:
        return self._coord.last_success_utc

    @property
    def extra_state_attributes(self) -> Any:
        return self._cloud_diag_attrs(include_last_success=False)

    @property
    def device_info(self) -> Any:
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

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "latency_ms", "Cloud Latency", type_key=None)

    @property
    def native_value(self) -> Any:
        return self._coord.latency_ms

    @property
    def extra_state_attributes(self) -> Any:
        return {}

    @property
    def device_info(self) -> Any:
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)  # pragma: no cover


class _GridProfileSensor(_SiteBaseEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator, key: str, name: str) -> None:
        super().__init__(coord, key, name, type_key="envoy")

    @property
    def available(self) -> bool:
        runtime = self._coord.grid_profile_runtime
        return bool(
            super().available
            and (
                runtime.installer_access_confirmed
                or (
                    runtime.support_state == SUPPORT_READ_ONLY
                    and runtime.current_profile_display()
                )
            )
        )


class EnphaseCurrentGridProfileSensor(_GridProfileSensor):
    _attr_entity_category = None
    _attr_translation_key = "current_grid_profile"
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "current_grid_profile", "Grid Profile")

    @property
    def native_value(self) -> str | None:
        runtime = cast(GridProfileRuntime, self._coord.grid_profile_runtime)
        return runtime.current_profile_display()

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        runtime = cast(GridProfileRuntime, self._coord.grid_profile_runtime)
        return runtime.current_profile_attributes()


class EnphaseSiteLastErrorCodeSensor(_SiteBaseEntity):
    _attr_translation_key = "cloud_error_code"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(CLOUD_ERROR_CODE_STATES)

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "last_error_code", "Cloud Error Code", type_key=None)

    def _auth_block_is_active(self) -> bool:
        """Return True when auth is currently blocked without mutating coordinator state."""

        if getattr(self._coord, "_last_error", None) == "auth_blocked":
            return True
        blocked_until = getattr(self._coord, "_auth_blocked_until_utc", None)
        if isinstance(blocked_until, datetime):
            return blocked_until > dt_util.utcnow()  # type: ignore[no-any-return]
        return False

    @property
    def native_value(self) -> Any:
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
    def extra_state_attributes(self) -> Any:
        return {}

    @property
    def device_info(self) -> Any:
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
            "endpoint_failure_details",
        }
    )

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
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
    def icon(self) -> Any:
        if self.native_value == "degraded":
            return "mdi:cloud-alert"
        if self.native_value == "unknown":
            return "mdi:cloud-question"
        return "mdi:cloud-check"

    @property
    def extra_state_attributes(self) -> Any:
        metrics = self._metrics_snapshot
        metrics_available = metrics is not None
        if metrics is None:
            metrics = {}
        degraded_services = self._string_list(metrics.get("degraded_services"))
        degraded_endpoint_families = self._string_list(
            metrics.get("degraded_endpoint_families")
        )
        failure_details: dict[str, dict[str, str | None]] = {}
        raw_failure_details = metrics.get("endpoint_failure_details")
        if isinstance(raw_failure_details, dict):
            for raw_family, raw_detail in raw_failure_details.items():
                family = _gateway_clean_text(raw_family)
                if family is None or not isinstance(raw_detail, dict):
                    continue
                reason = _gateway_clean_text(raw_detail.get("reason"))
                retry_utc = _gateway_clean_text(raw_detail.get("retry_utc"))
                if reason is None:
                    continue
                parsed_retry = (
                    dt_util.parse_datetime(retry_utc) if retry_utc is not None else None
                )
                if parsed_retry is not None and parsed_retry.tzinfo is not None:
                    retry_utc = dt_util.as_utc(parsed_retry).isoformat()
                else:
                    retry_utc = None
                failure_details[family] = {
                    "reason": redact_text(
                        reason,
                        site_ids=(str(self._coord.site_id),),
                        max_length=160,
                    ),
                    "retry_utc": retry_utc,
                }
        attrs: dict[str, object] = {
            "degraded_services": degraded_services,
            "degraded_endpoint_families": degraded_endpoint_families,
            "degraded_service_count": len(degraded_services),
            "degraded_endpoint_family_count": len(degraded_endpoint_families),
            "metrics_available": metrics_available,
            "endpoint_failure_details": failure_details,
        }
        attrs.update(self._cloud_diag_attrs())
        return attrs

    @property
    def device_info(self) -> Any:
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)


class EnphaseSiteBackoffEndsSensor(_SiteBaseEntity):
    _attr_translation_key = "cloud_backoff_ends"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
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
    def extra_state_attributes(self) -> Any:
        return {}

    @property
    def device_info(self) -> Any:
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


class EnphaseStormAlertSensor(_SiteBaseEntity):
    _attr_translation_key = "storm_alert"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "storm_alert", "Storm Alert", type_key="envoy")

    @property
    def native_value(self) -> Any:
        active = self._coord.storm_alert_active
        if active is None:
            return None
        return "active" if active else "inactive"

    @property
    def extra_state_attributes(self) -> Any:
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

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
        value = self._coord.battery_aggregate_charge_pct
        if value is None:
            return None
        try:
            return round(float(value), 1)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self) -> Any:
        return {}


class EnphaseBatteryOverallStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_overall_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
        return self._coord.battery_aggregate_status

    @property
    def extra_state_attributes(self) -> Any:
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

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
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
    def __init__(self, coord: EnphaseCoordinator, schedule_type: str) -> None:
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
    def extra_state_attributes(self) -> Any:
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

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
        summary = self._coord.battery_status_summary
        value = summary.get("site_available_energy_kwh")
        if value is None:
            return None
        try:
            return round(float(cast(Any, value)), 2)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self) -> Any:
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

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
        summary = self._coord.battery_status_summary
        value = summary.get("site_available_power_kw")
        if value is None:
            return None
        try:
            return round(float(cast(Any, value)), 3)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self) -> Any:
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

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
        return _battery_last_reported_snapshot(self._coord).get("latest_reported")

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = _battery_last_reported_snapshot(self._coord)
        return {
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "without_last_report_count": snapshot.get("without_last_report_count"),
            "total_batteries": snapshot.get("total_batteries"),
        }


class EnphaseAcBatteryOverallStatusSensor(_SiteBaseEntity):
    _attr_translation_key = "ac_battery_overall_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
        return self._coord.ac_battery_aggregate_status

    @property
    def extra_state_attributes(self) -> Any:
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

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
        summary = self._coord.ac_battery_status_summary
        value = summary.get("power_w")
        if value is None:
            return None
        try:
            return round(float(cast(Any, value)), 3)
        except Exception:  # noqa: BLE001
            return None

    @property
    def extra_state_attributes(self) -> Any:
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

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
        return ac_battery_last_reported_snapshot(self._coord).get("latest_reported")

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = ac_battery_last_reported_snapshot(self._coord)
        return {
            "latest_reported_device": snapshot.get("latest_reported_device"),
            "without_last_report_count": snapshot.get("without_last_report_count"),
            "total_batteries": snapshot.get("total_batteries"),
        }


class EnphaseBatteryModeSensor(_SiteBaseEntity):
    _attr_translation_key = "battery_mode"
    _attr_icon = "mdi:battery"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "battery_mode", "Battery Mode", type_key="encharge")

    def _mode_raw(self) -> str | None:
        raw_mode = getattr(self._coord, "battery_grid_mode", None)
        if raw_mode is not None:
            return raw_mode  # type: ignore[no-any-return]
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
    def native_value(self) -> Any:
        display = getattr(self._coord, "battery_mode_display", None)
        if display is not None:
            return display
        return self._mode_raw()

    @property
    def extra_state_attributes(self) -> Any:
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


class EnphaseGridModeSensor(_SiteBaseEntity):
    _attr_translation_key = "grid_mode"

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
        mode = getattr(self._coord, "grid_mode", None)
        if mode in {"on_grid", "off_grid", "unknown"}:
            return mode
        return "unknown"

    @property
    def extra_state_attributes(self) -> Any:
        return {
            "source": getattr(self._coord, "grid_mode_source", None),
            "raw_states": getattr(self._coord, "grid_mode_raw_states", []),
            "grid_mode_status_supported": getattr(
                self._coord, "grid_mode_status_supported", None
            ),
            "grid_relay": getattr(self._coord, "grid_mode_status_raw", None),
            "grid_outage_context_supported": getattr(
                self._coord, "grid_outage_context_supported", None
            ),
            "is_grid_outage": getattr(self._coord, "grid_outage_is_grid_outage", None),
            "show_grid_connect": getattr(
                self._coord, "grid_outage_show_grid_connect", None
            ),
            "has_battery": getattr(self._coord, "grid_outage_has_battery", None),
            "is_sunlight_backup": getattr(
                self._coord, "grid_outage_is_sunlight_backup", None
            ),
        }

    @property
    def device_info(self) -> Any:
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

    def __init__(self, coord: EnphaseCoordinator) -> None:
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
    def native_value(self) -> Any:
        if self._coord.battery_profile_pending:
            return (
                self._coord.battery_profile_display
                or self._coord.battery_effective_profile_display
            )
        return self._coord.battery_effective_profile_display

    @property
    def extra_state_attributes(self) -> Any:
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
