from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback as ha_callback

from .const import BASE_URL, DOMAIN
from .runtime_data import get_runtime_data

HEALTH_CAPTURE_ERRORS = (RuntimeError, TypeError, ValueError, AttributeError)
_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])


def callback(func: _CallbackT) -> _CallbackT:
    """Apply Home Assistant's callback marker with its identity type preserved."""

    return cast(_CallbackT, ha_callback(func))


SYSTEM_HEALTH_INFO_KEYS = (
    "site_count",
    "site_id",
    "site_name",
    "site_ids",
    "site_names",
    "can_reach_server",
    "last_success",
    "latency_ms",
    "last_error",
    "scheduler_available",
    "scheduler_last_error",
    "scheduler_failures",
    "scheduler_backoff_active",
    "scheduler_backoff_ends_utc",
    "auth_settings_available",
    "auth_settings_last_error",
    "auth_settings_failures",
    "auth_settings_backoff_active",
    "auth_settings_backoff_ends_utc",
    "session_history_available",
    "session_history_last_error",
    "session_history_failures",
    "session_history_backoff_active",
    "session_history_backoff_ends_utc",
    "site_energy_available",
    "site_energy_last_error",
    "site_energy_failures",
    "site_energy_backoff_active",
    "site_energy_backoff_ends_utc",
    "tariff_available",
    "tariff_service_status",
    "tariff_last_error",
    "tariff_failures",
    "tariff_backoff_active",
    "tariff_backoff_ends_utc",
    "degraded_endpoint_families",
    "degraded_services",
    "firmware_catalog_last_fetch_utc",
    "firmware_catalog_last_success_utc",
    "firmware_catalog_last_error",
    "firmware_catalog_using_stale",
    "firmware_catalog_generated_at",
    "firmware_catalog_source_age_seconds",
    "last_failure_status",
    "last_failure_description",
    "backoff_active",
    "network_errors",
    "http_errors",
    "phase_timings",
    "session_cache_ttl_s",
    "sites",
)


@callback
def async_register(
    hass: HomeAssistant, register: system_health.RegisterSystemHealth
) -> None:
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, object]:
    entries = hass.config_entries.async_entries(DOMAIN)
    site_infos: list[dict[str, object]] = []

    for entry in entries:
        entry_site_id = entry.data.get("site_id")
        metrics: dict[str, object] = {"site_id": entry_site_id}
        try:
            coord = get_runtime_data(entry).coordinator
        except RuntimeError:
            coord = None
        if coord is not None:
            collect_site_metrics = getattr(coord, "collect_site_metrics", None)
            if callable(collect_site_metrics):
                try:
                    metrics = cast(dict[str, object], collect_site_metrics())
                except HEALTH_CAPTURE_ERRORS:
                    metrics = {"site_id": entry_site_id}
        if metrics.get("site_id") is None and entry_site_id is not None:
            metrics["site_id"] = entry_site_id
        site_infos.append(metrics)

    primary = site_infos[0] if site_infos else {}
    can_reach_server = await system_health.async_check_can_reach_url(hass, BASE_URL)
    return {
        "site_count": len(site_infos),
        "site_id": primary.get("site_id"),
        "site_name": primary.get("site_name"),
        "site_ids": [info.get("site_id") for info in site_infos if info.get("site_id")],
        "site_names": [
            info.get("site_name") for info in site_infos if info.get("site_name")
        ],
        "can_reach_server": can_reach_server,
        "last_success": primary.get("last_success"),
        "latency_ms": primary.get("latency_ms"),
        "last_error": primary.get("last_error"),
        "scheduler_available": primary.get("scheduler_available"),
        "scheduler_last_error": primary.get("scheduler_last_error"),
        "scheduler_failures": primary.get("scheduler_failures"),
        "scheduler_backoff_active": primary.get("scheduler_backoff_active"),
        "scheduler_backoff_ends_utc": primary.get("scheduler_backoff_ends_utc"),
        "auth_settings_available": primary.get("auth_settings_available"),
        "auth_settings_last_error": primary.get("auth_settings_last_error"),
        "auth_settings_failures": primary.get("auth_settings_failures"),
        "auth_settings_backoff_active": primary.get("auth_settings_backoff_active"),
        "auth_settings_backoff_ends_utc": primary.get("auth_settings_backoff_ends_utc"),
        "session_history_available": primary.get("session_history_available"),
        "session_history_last_error": primary.get("session_history_last_error"),
        "session_history_failures": primary.get("session_history_failures"),
        "session_history_backoff_active": primary.get("session_history_backoff_active"),
        "session_history_backoff_ends_utc": primary.get(
            "session_history_backoff_ends_utc"
        ),
        "site_energy_available": primary.get("site_energy_available"),
        "site_energy_last_error": primary.get("site_energy_last_error"),
        "site_energy_failures": primary.get("site_energy_failures"),
        "site_energy_backoff_active": primary.get("site_energy_backoff_active"),
        "site_energy_backoff_ends_utc": primary.get("site_energy_backoff_ends_utc"),
        "tariff_available": primary.get("tariff_available"),
        "tariff_service_status": primary.get("tariff_service_status"),
        "tariff_last_error": primary.get("tariff_last_error"),
        "tariff_failures": primary.get("tariff_failures"),
        "tariff_backoff_active": primary.get("tariff_backoff_active"),
        "tariff_backoff_ends_utc": primary.get("tariff_backoff_ends_utc"),
        "degraded_endpoint_families": primary.get("degraded_endpoint_families", []),
        "degraded_services": primary.get("degraded_services", []),
        "firmware_catalog_last_fetch_utc": primary.get(
            "firmware_catalog_last_fetch_utc"
        ),
        "firmware_catalog_last_success_utc": primary.get(
            "firmware_catalog_last_success_utc"
        ),
        "firmware_catalog_last_error": primary.get("firmware_catalog_last_error"),
        "firmware_catalog_using_stale": primary.get("firmware_catalog_using_stale"),
        "firmware_catalog_generated_at": primary.get("firmware_catalog_generated_at"),
        "firmware_catalog_source_age_seconds": primary.get(
            "firmware_catalog_source_age_seconds"
        ),
        "last_failure_status": primary.get("last_failure_status"),
        "last_failure_description": primary.get("last_failure_description"),
        "backoff_active": primary.get("backoff_active"),
        "network_errors": primary.get("network_errors"),
        "http_errors": primary.get("http_errors"),
        "phase_timings": primary.get("phase_timings", {}),
        "session_cache_ttl_s": primary.get("session_cache_ttl_s"),
        "sites": site_infos,
    }
