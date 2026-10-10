"""Immutable cache and cooldown policy definitions for cloud endpoint families."""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    PRODUCTION_POWER_STALE_AFTER_S,
    GRID_CONTROL_CHECK_STALE_AFTER_S,
    GRID_MODE_STATUS_CACHE_TTL,
    GRID_MODE_STATUS_STALE_AFTER_S,
    GRID_OUTAGE_CONTEXT_CACHE_TTL,
    GRID_OUTAGE_CONTEXT_STALE_AFTER_S,
)
from .tariff import TARIFF_SUCCESS_TTL_S


@dataclass(frozen=True, slots=True)
class EndpointFamilyPolicy:
    """Coordinator policy for read-only Enlighten endpoint families."""

    success_ttl_s: float | None = None
    stale_after_s: float | None = None
    failure_backoff_schedule_s: tuple[float, ...] = ()
    max_backoff_s: float | None = None
    optional: bool = False
    suppress_after_failures: int | None = None
    support_state_on_success: bool = False


def build_endpoint_family_policies() -> dict[str, EndpointFamilyPolicy]:
    """Return cooldown/cache policies for read-only endpoint families."""

    return {
        "core_realtime": EndpointFamilyPolicy(
            failure_backoff_schedule_s=(60.0, 120.0, 300.0, 600.0),
            max_backoff_s=600.0,
        ),
        "current_power": EndpointFamilyPolicy(
            success_ttl_s=60.0,
            stale_after_s=PRODUCTION_POWER_STALE_AFTER_S,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "battery_status": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            stale_after_s=1800.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            support_state_on_success=True,
        ),
        "system_events": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            stale_after_s=21600.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "system_event_history": EndpointFamilyPolicy(
            success_ttl_s=900.0,
            stale_after_s=86400.0,
            failure_backoff_schedule_s=(900.0, 1800.0, 3600.0, 7200.0),
            max_backoff_s=7200.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "vpp_enrollment": EndpointFamilyPolicy(
            success_ttl_s=21600.0,
            stale_after_s=604800.0,
            failure_backoff_schedule_s=(900.0, 1800.0, 3600.0, 7200.0),
            max_backoff_s=7200.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "vpp_events": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            stale_after_s=3600.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "grid_control_check": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            stale_after_s=GRID_CONTROL_CHECK_STALE_AFTER_S,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "grid_mode_status": EndpointFamilyPolicy(
            success_ttl_s=GRID_MODE_STATUS_CACHE_TTL,
            stale_after_s=GRID_MODE_STATUS_STALE_AFTER_S,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "grid_outage_context": EndpointFamilyPolicy(
            success_ttl_s=GRID_OUTAGE_CONTEXT_CACHE_TTL,
            stale_after_s=GRID_OUTAGE_CONTEXT_STALE_AFTER_S,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "activation_grid_profile": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            stale_after_s=3600.0,
            failure_backoff_schedule_s=(3600.0, 21600.0, 86400.0),
            max_backoff_s=86400.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "dry_contact_settings": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            stale_after_s=900.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "battery_backup_history": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            support_state_on_success=True,
        ),
        "battery_settings": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            support_state_on_success=True,
        ),
        "battery_site_settings": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            support_state_on_success=True,
        ),
        "battery_schedules": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            support_state_on_success=True,
        ),
        "storm_guard": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            support_state_on_success=True,
        ),
        "storm_alert": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            support_state_on_success=True,
        ),
        "tariff": EndpointFamilyPolicy(
            success_ttl_s=TARIFF_SUCCESS_TTL_S,
            stale_after_s=86400.0,
            failure_backoff_schedule_s=(60.0, 60.0, 60.0, 60.0),
            max_backoff_s=60.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "tariff_dated_rates": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            stale_after_s=86400.0,
            failure_backoff_schedule_s=(3600.0, 21600.0, 86400.0),
            max_backoff_s=86400.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "inventory_topology": EndpointFamilyPolicy(
            success_ttl_s=21600.0,
            failure_backoff_schedule_s=(1800.0, 3600.0, 7200.0, 21600.0),
            max_backoff_s=21600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "hems_inventory": EndpointFamilyPolicy(
            success_ttl_s=None,
            stale_after_s=900.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "inverter_inventory": EndpointFamilyPolicy(
            success_ttl_s=21600.0,
            failure_backoff_schedule_s=(1800.0, 3600.0, 7200.0, 21600.0),
            max_backoff_s=21600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "inverter_status": EndpointFamilyPolicy(
            success_ttl_s=300.0,
            stale_after_s=1800.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "inverter_production": EndpointFamilyPolicy(
            success_ttl_s=600.0,
            stale_after_s=1800.0,
            failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
            max_backoff_s=3600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "inverter_dashboard_inventory": EndpointFamilyPolicy(
            success_ttl_s=21600.0,
            stale_after_s=86400.0,
            failure_backoff_schedule_s=(1800.0, 3600.0, 7200.0, 21600.0),
            max_backoff_s=21600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "inverter_parameter_catalog": EndpointFamilyPolicy(
            success_ttl_s=21600.0,
            stale_after_s=86400.0,
            failure_backoff_schedule_s=(1800.0, 3600.0, 7200.0, 21600.0),
            max_backoff_s=21600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
        "inverter_parameter_telemetry": EndpointFamilyPolicy(
            success_ttl_s=900.0,
            stale_after_s=1800.0,
            failure_backoff_schedule_s=(3600.0, 7200.0, 14400.0, 21600.0),
            max_backoff_s=21600.0,
            optional=True,
            suppress_after_failures=3,
            support_state_on_success=True,
        ),
    }
