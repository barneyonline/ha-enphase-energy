"""Collect coordinator diagnostics and synchronize Home Assistant repair issues."""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, cast

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    BATTERY_PROFILE_PENDING_TIMEOUT_S,
    DEFAULT_DEGRADED_SERVICE_REPAIR_ISSUES,
    DOMAIN,
    ISSUE_BATTERY_PROFILE_PENDING,
    ISSUE_CLOUD_ERRORS,
    ISSUE_DNS_RESOLUTION,
    ISSUE_NETWORK_UNREACHABLE,
    ISSUE_RATE_LIMITED,
    ISSUE_REAUTH_REQUIRED,
    ISSUE_AUTH_SETTINGS_UNAVAILABLE,
    ISSUE_AUTH_BLOCKED,
    ISSUE_HEMS_AUTH_DEGRADED,
    ISSUE_TOO_MANY_ACTIVE_SESSIONS,
    ISSUE_SCHEDULER_UNAVAILABLE,
    ISSUE_SESSION_HISTORY_UNAVAILABLE,
    ISSUE_SITE_ENERGY_UNAVAILABLE,
    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
)
from .coordinator_refresh_metrics import (
    refresh_performance_history_summary,
    refresh_performance_summary,
)
from .log_redaction import redact_text
from .tariff import TARIFF_ENDPOINT_FAMILY

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator


_DEGRADED_SERVICE_REPAIR_ISSUES: tuple[tuple[str, str], ...] = (
    ("_scheduler_issue_reported", ISSUE_SCHEDULER_UNAVAILABLE),
    ("_session_history_issue_reported", ISSUE_SESSION_HISTORY_UNAVAILABLE),
    ("_site_energy_issue_reported", ISSUE_SITE_ENERGY_UNAVAILABLE),
    ("_auth_settings_issue_reported", ISSUE_AUTH_SETTINGS_UNAVAILABLE),
    ("_hems_auth_issue_reported", ISSUE_HEMS_AUTH_DEGRADED),
)
_DEGRADED_SERVICE_REPAIR_ISSUE_IDS = frozenset(
    issue_id for _flag_attr, issue_id in _DEGRADED_SERVICE_REPAIR_ISSUES
)


class CoordinatorDiagnostics:
    """Diagnostics, payload health, and repair-issue helpers for the coordinator."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        if self._entry_issue_suffix() is not None:
            self.clear_legacy_degraded_service_repair_issues()
        if not self.degraded_service_repair_issues_enabled:
            self.clear_degraded_service_repair_issues()

    def _delete_issue(self, issue_id: str) -> None:
        coord = self.coordinator
        ir.async_delete_issue(coord.hass, DOMAIN, issue_id)

    def _entry_issue_suffix(self) -> str | None:
        config_entry = getattr(self.coordinator, "config_entry", None)
        entry_id = getattr(config_entry, "entry_id", None)
        if not entry_id:
            return None
        suffix = "".join(
            char.lower() if char.isalnum() else "_" for char in str(entry_id).strip()
        ).strip("_")
        return suffix or None

    def _repair_issue_id(self, issue_id: str) -> str:
        if issue_id not in _DEGRADED_SERVICE_REPAIR_ISSUE_IDS:
            return issue_id
        suffix = self._entry_issue_suffix()
        if suffix is None:
            return issue_id
        return f"{issue_id}_{suffix}"

    def _delete_reported_issue(self, issue_id: str) -> None:
        registry_issue_id = self._repair_issue_id(issue_id)
        self._delete_issue(registry_issue_id)
        if registry_issue_id != issue_id:
            self._delete_issue(issue_id)

    def clear_legacy_degraded_service_repair_issues(self) -> None:
        """Clear pre-scoped degraded-service repair issues after upgrade."""

        for issue_id in _DEGRADED_SERVICE_REPAIR_ISSUE_IDS:
            self._delete_issue(issue_id)

    def _create_site_metrics_issue(
        self,
        issue_id: str,
        *,
        severity: ir.IssueSeverity,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        coord = self.coordinator
        registry_issue_id = self._repair_issue_id(issue_id)
        metrics, base_placeholders = self.issue_context()
        # Repair issues store the current diagnostic snapshot so users can share
        # actionable context without enabling debug logging first.
        issue_placeholders = dict(base_placeholders)
        if placeholders:
            issue_placeholders.update(placeholders)
        ir.async_create_issue(
            coord.hass,
            DOMAIN,
            registry_issue_id,
            is_fixable=False,
            severity=severity,
            translation_key=issue_id,
            translation_placeholders=issue_placeholders,
            data={"site_metrics": metrics},
        )
        if registry_issue_id != issue_id:
            self._delete_issue(issue_id)

    def _clear_reported_issue(self, flag_attr: str, issue_id: str) -> None:
        coord = self.coordinator
        if not getattr(coord, flag_attr, False):
            return
        self._delete_reported_issue(issue_id)
        setattr(coord, flag_attr, False)

    @property
    def degraded_service_repair_issues_enabled(self) -> bool:
        """Return whether optional degraded-service repair issues should be raised."""

        config_entry = getattr(self.coordinator, "config_entry", None)
        options = getattr(config_entry, "options", {}) if config_entry else {}
        if not isinstance(options, dict):
            options = dict(options or {})
        return bool(
            options.get(
                OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
                DEFAULT_DEGRADED_SERVICE_REPAIR_ISSUES,
            )
        )

    def clear_degraded_service_repair_issues(self) -> None:
        """Clear optional degraded-service repair issues regardless of local flags."""

        coord = self.coordinator
        for flag_attr, issue_id in _DEGRADED_SERVICE_REPAIR_ISSUES:
            self._delete_reported_issue(issue_id)
            setattr(coord, flag_attr, False)

    def _degraded_service_issue_suppressed(self, flag_attr: str, issue_id: str) -> bool:
        if issue_id not in _DEGRADED_SERVICE_REPAIR_ISSUE_IDS:
            return False
        if self.degraded_service_repair_issues_enabled:
            return False
        self._delete_reported_issue(issue_id)
        setattr(self.coordinator, flag_attr, False)
        return True

    def _report_flagged_issue(
        self,
        flag_attr: str,
        issue_id: str,
        *,
        severity: ir.IssueSeverity,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        coord = self.coordinator
        if self._degraded_service_issue_suppressed(flag_attr, issue_id):
            return
        if getattr(coord, flag_attr, False):
            return
        self._create_site_metrics_issue(
            issue_id,
            severity=severity,
            placeholders=placeholders,
        )
        setattr(coord, flag_attr, True)

    def collect_site_metrics(self) -> dict[str, object]:
        """Return a snapshot of site-level metrics for diagnostics."""

        coord = self.coordinator

        def _iso(value: datetime | None) -> str | None:
            if not value:
                return None
            try:
                return value.isoformat()
            except Exception:
                return str(value)

        def _safe_bool_call(name: str) -> bool:
            func = getattr(coord, name, None)
            if not callable(func):
                return False
            try:
                return bool(func())
            except Exception:
                return False

        def _safe_attr(name: str, default: object = None) -> object:
            try:
                return getattr(coord, name)
            except Exception:
                return default

        backoff_until = coord._backoff_until or 0.0
        backoff_active = bool(backoff_until and backoff_until > time.monotonic())
        scheduler_backoff_active = coord._scheduler_backoff_active()
        type_keys = coord.inventory_view.iter_type_keys()
        type_counts: dict[str, int] = {}
        for key in type_keys:
            bucket = coord.inventory_view.type_bucket(key)
            if not bucket:
                continue
            try:
                type_counts[key] = int(str(bucket.get("count", 0)))
            except Exception:
                type_counts[key] = 0

        def _coordinator_available() -> bool:
            return bool(getattr(coord, "last_update_success", False))

        def _parsed_float(value: object) -> float | None:
            if value is None:
                return None
            try:
                return float(str(value))
            except Exception:
                return None

        def _type_available_for_entities(type_key: str) -> bool:
            inventory_view = getattr(coord, "inventory_view", None)
            checker = getattr(inventory_view, "has_type_for_entities", None)
            if not callable(checker):
                return False
            try:
                return bool(checker(type_key))
            except Exception:
                return False

        def _site_sensor_base_available(type_key: str | None) -> bool:
            if type_key is not None and not _type_available_for_entities(type_key):
                return False
            if coord.last_success_utc is not None:
                # Site diagnostic entities remain useful with stale data after a
                # prior successful refresh.
                return True
            return _coordinator_available()

        def _battery_write_access_confirmed() -> bool:
            try:
                return bool(coord.battery_write_access_confirmed)
            except Exception:
                owner = getattr(coord, "_battery_user_is_owner", None)
                installer = getattr(coord, "_battery_user_is_installer", None)
                return owner is True or installer is True

        def _cfg_schedule_edit_available() -> bool:
            if getattr(coord, "charge_from_grid_schedule_available", False):
                return True
            if not getattr(coord, "charge_from_grid_control_available", False):
                return False
            if not getattr(coord, "charge_from_grid_schedule_supported", False):
                return False
            if getattr(coord, "_battery_cfg_schedule_id", None) is not None:
                start_time = getattr(coord, "battery_charge_from_grid_start_time", None)
                end_time = getattr(coord, "battery_charge_from_grid_end_time", None)
                if start_time is not None and end_time is not None:
                    return True
                begin = getattr(coord, "_battery_charge_begin_time", None)
                end = getattr(coord, "_battery_charge_end_time", None)
                return begin is not None and end is not None
            return False

        def _dtg_schedule_edit_available() -> bool:
            if getattr(coord, "discharge_to_grid_schedule_available", False):
                return True
            if not getattr(coord, "discharge_to_grid_schedule_supported", False):
                return False
            start_time = getattr(coord, "battery_discharge_to_grid_start_time", None)
            end_time = getattr(coord, "battery_discharge_to_grid_end_time", None)
            if start_time is not None and end_time is not None:
                return True
            begin = getattr(coord, "_battery_dtg_begin_time", None)
            end = getattr(coord, "_battery_dtg_end_time", None)
            if begin is None:
                begin = getattr(coord, "_battery_dtg_control_begin_time", None)
            if end is None:
                end = getattr(coord, "_battery_dtg_control_end_time", None)
            return begin is not None and end is not None

        def _rbd_schedule_edit_available() -> bool:
            if getattr(coord, "restrict_battery_discharge_schedule_available", False):
                return True
            if not getattr(
                coord, "restrict_battery_discharge_schedule_supported", False
            ):
                return False
            start_time = getattr(
                coord, "battery_restrict_battery_discharge_start_time", None
            )
            end_time = getattr(
                coord, "battery_restrict_battery_discharge_end_time", None
            )
            if start_time is not None and end_time is not None:
                return True
            begin = getattr(coord, "_battery_rbd_begin_time", None)
            end = getattr(coord, "_battery_rbd_end_time", None)
            if begin is None:
                begin = getattr(coord, "_battery_rbd_control_begin_time", None)
            if end is None:
                end = getattr(coord, "_battery_rbd_control_end_time", None)
            return begin is not None and end is not None

        battery_type_available = _type_available_for_entities("encharge")
        ac_battery_type_available = _type_available_for_entities("ac_battery")
        battery_write_access = _battery_write_access_confirmed()
        battery_sensor_base_available = _site_sensor_base_available("encharge")
        ac_battery_sensor_base_available = _site_sensor_base_available("ac_battery")
        battery_status_summary = coord.battery_status_summary
        ac_battery_status_summary = coord.ac_battery_status_summary
        battery_aggregate_charge = coord.battery_aggregate_charge_pct
        battery_aggregate_status = coord.battery_aggregate_status
        ac_battery_aggregate_status = coord.ac_battery_aggregate_status
        battery_available_energy = _parsed_float(
            battery_status_summary.get("site_available_energy_kwh")
        )
        battery_available_power = _parsed_float(
            battery_status_summary.get("site_available_power_kw")
        )
        ac_battery_power = _parsed_float(ac_battery_status_summary.get("power_w"))
        endpoint_family_health = self.endpoint_family_health_diagnostics()
        tariff_health = endpoint_family_health.get(TARIFF_ENDPOINT_FAMILY)
        tariff_service_status = "unknown"
        tariff_available = None
        tariff_failures = None
        tariff_last_error = None
        tariff_backoff_active = None
        tariff_backoff_ends_utc = None
        if isinstance(tariff_health, dict):
            tariff_failures = int(tariff_health.get("consecutive_failures") or 0)
            tariff_last_error = tariff_health.get("last_error")
            tariff_backoff_active = bool(tariff_health.get("cooldown_active"))
            tariff_backoff_ends_utc = tariff_health.get("next_retry_utc")
            tariff_degraded = (
                tariff_backoff_active
                or tariff_failures > 0
                or bool(tariff_health.get("suppressed"))
            )
            tariff_service_status = "degraded" if tariff_degraded else "available"
            tariff_available = not tariff_degraded

        hems_auth_backoff_remaining_s = coord._hems_auth_backoff_remaining_s()
        metrics: dict[str, object] = {
            "site_id": coord.site_id,
            "site_name": coord.site_name,
            "last_success": _iso(coord.last_success_utc),
            "last_error": getattr(coord, "_last_error", None),
            "last_failure": _iso(coord.last_failure_utc),
            "last_failure_status": getattr(coord, "last_failure_status", None),
            "last_failure_description": getattr(
                coord, "last_failure_description", None
            ),
            "last_failure_source": getattr(coord, "last_failure_source", None),
            "last_failure_response": getattr(coord, "last_failure_response", None),
            "last_failure_endpoint": getattr(coord, "last_failure_endpoint", None),
            "payload_using_stale": bool(getattr(coord, "payload_using_stale", False)),
            "payload_failure_kind": getattr(coord, "payload_failure_kind", None),
            "latency_ms": coord.latency_ms,
            "backoff_active": backoff_active,
            "backoff_until_monotonic": coord._backoff_until,
            "backoff_ends_utc": _iso(coord.backoff_ends_utc),
            "network_errors": getattr(coord, "_network_errors", 0),
            "http_errors": getattr(coord, "_http_errors", 0),
            "rate_limit_hits": getattr(coord, "_rate_limit_hits", 0),
            "dns_errors": getattr(coord, "_dns_failures", 0),
            "phase_timings": coord.phase_timings,
            "refresh_performance": refresh_performance_summary(
                coord.phase_timings,
                latency_ms=coord.latency_ms,
                cloud_calls=getattr(coord, "_last_refresh_cloud_calls", None),
                steady_cloud_calls=getattr(
                    coord, "_last_steady_refresh_cloud_calls", None
                ),
                fast_cloud_calls=getattr(coord, "_last_fast_refresh_cloud_calls", None),
            ),
            "refresh_performance_history": refresh_performance_history_summary(
                getattr(coord, "_refresh_performance_history", [])
            ),
            "bootstrap_phase_timings": coord.bootstrap_phase_timings,
            "setup_phase_timings": coord.setup_phase_timings,
            "setup_milestones": coord.setup_milestones,
            "startup_power_phase_timings": coord.startup_power_phase_timings,
            "warmup_phase_timings": coord.warmup_phase_timings,
            "warmup_in_progress": getattr(coord, "_warmup_in_progress", False),
            "warmup_last_error": getattr(coord, "_warmup_last_error", None),
            "type_device_keys": type_keys,
            "type_device_counts": type_counts,
            "payload_health": self.payload_health_diagnostics(),
            "endpoint_family_health": endpoint_family_health,
            "tariff_available": tariff_available,
            "tariff_service_status": tariff_service_status,
            "tariff_failures": tariff_failures,
            "tariff_last_error": tariff_last_error,
            "tariff_backoff_active": tariff_backoff_active,
            "tariff_backoff_ends_utc": tariff_backoff_ends_utc,
            "auth_refresh_suspended_active": coord._auth_refresh_suspended_active(),
            "auth_refresh_suspended_until": _iso(
                getattr(coord, "_auth_refresh_suspended_until_utc", None)
            ),
            "auth_refresh_rejected_count": getattr(
                coord, "_auth_refresh_rejected_count", 0
            ),
            "auth_refresh_recent_success_active": _safe_bool_call(
                "_auth_refresh_recent_success_active"
            ),
            "auth_refresh_last_attempt_utc": _iso(
                getattr(coord, "_auth_refresh_last_attempt_utc", None)
            ),
            "auth_refresh_last_success_utc": _iso(
                getattr(coord, "_auth_refresh_last_success_utc", None)
            ),
            "auth_refresh_last_failure_reason": getattr(
                coord, "_auth_refresh_last_failure_reason", None
            ),
            "last_unauthorized_request": getattr(
                getattr(coord, "client", None),
                "last_unauthorized_request",
                None,
            ),
            "hems_auth_backoff_remaining_s": hems_auth_backoff_remaining_s,
            "hems_auth_circuit_active": hems_auth_backoff_remaining_s is not None,
            "hems_auth_backoff_until": _iso(
                getattr(coord, "_hems_auth_backoff_ends_utc", None)
            ),
            "hems_auth_failure_count": getattr(coord, "_hems_auth_failure_count", 0),
            "hems_auth_last_failure_utc": _iso(
                getattr(coord, "_hems_auth_last_failure_utc", None)
            ),
            "hems_auth_last_success_utc": _iso(
                getattr(coord, "_hems_auth_last_success_utc", None)
            ),
            "hems_auth_last_endpoint": getattr(coord, "_hems_auth_last_endpoint", None),
            "hems_auth_last_status": getattr(coord, "_hems_auth_last_status", None),
            "hems_auth_last_reason": getattr(coord, "_hems_auth_last_reason", None),
            "auth_blocked_active": coord._auth_block_active(),
            "auth_blocked_until": _iso(getattr(coord, "_auth_blocked_until_utc", None)),
            "auth_block_reason": getattr(coord, "_auth_block_reason", None),
            "inverters_enabled": bool(getattr(coord, "include_inverters", True)),
            "inverters_count": len(getattr(coord, "_inverter_data", {}) or {}),
            "inverters_summary_counts": dict(
                getattr(coord, "_inverter_summary_counts", {}) or {}
            ),
            "inverters_model_counts": dict(
                getattr(coord, "_inverter_model_counts", {}) or {}
            ),
            "session_cache_ttl_s": getattr(coord, "_session_history_cache_ttl", None),
            "scheduler_available": coord.scheduler_available,
            "scheduler_failures": getattr(coord, "_scheduler_failures", 0),
            "scheduler_last_error": getattr(coord, "_scheduler_last_error", None),
            "scheduler_last_failure": _iso(
                getattr(coord, "_scheduler_last_failure_utc", None)
            ),
            "scheduler_backoff_active": scheduler_backoff_active,
            "scheduler_backoff_ends_utc": _iso(
                getattr(coord, "_scheduler_backoff_ends_utc", None)
            ),
            "auth_settings_available": coord.auth_settings_available,
            "auth_settings_failures": getattr(coord, "_auth_settings_failures", 0),
            "auth_settings_last_error": getattr(
                coord, "_auth_settings_last_error", None
            ),
            "auth_settings_last_failure": _iso(
                getattr(coord, "_auth_settings_last_failure_utc", None)
            ),
            "auth_settings_backoff_active": coord._auth_settings_backoff_active(),
            "auth_settings_backoff_ends_utc": _iso(
                getattr(coord, "_auth_settings_backoff_ends_utc", None)
            ),
            "battery_profile": getattr(coord, "_battery_profile", None),
            "battery_profile_label": coord._battery_profile_label(
                getattr(coord, "_battery_profile", None)
            ),
            "battery_live_profile": coord.battery_live_profile,
            "battery_live_profile_label": coord._battery_profile_label(
                coord.battery_live_profile
            ),
            "battery_live_profile_raw": getattr(
                coord, "_battery_live_profile_label", None
            ),
            "battery_live_profile_sample_utc": _iso(
                getattr(coord, "_battery_live_profile_sample_utc", None)
            ),
            "battery_backup_percentage": getattr(
                coord, "_battery_backup_percentage", None
            ),
            "battery_supports_mqtt": getattr(coord, "_battery_supports_mqtt", None),
            "battery_operation_mode_sub_type": getattr(
                coord, "_battery_operation_mode_sub_type", None
            ),
            "battery_profile_polling_interval_s": getattr(
                coord, "_battery_polling_interval_s", None
            ),
            "battery_cfg_control_show": getattr(
                coord, "_battery_cfg_control_show", None
            ),
            "battery_cfg_control_enabled": getattr(
                coord, "_battery_cfg_control_enabled", None
            ),
            "battery_cfg_control_schedule_supported": getattr(
                coord, "_battery_cfg_control_schedule_supported", None
            ),
            "battery_cfg_control_force_schedule_supported": getattr(
                coord, "_battery_cfg_control_force_schedule_supported", None
            ),
            "battery_profile_evse_device": getattr(
                coord, "_battery_profile_evse_device", None
            ),
            "battery_use_battery_for_self_consumption": getattr(
                coord, "_battery_use_battery_for_self_consumption", None
            ),
            "battery_profile_pending": coord.battery_profile_pending,
            "battery_pending_profile": getattr(coord, "_battery_pending_profile", None),
            "battery_pending_reserve": getattr(coord, "_battery_pending_reserve", None),
            "battery_pending_sub_type": getattr(
                coord, "_battery_pending_sub_type", None
            ),
            "battery_backend_profile_update_pending": getattr(
                coord, "_battery_backend_profile_update_pending", None
            ),
            "battery_backend_not_pending_observed_at": _iso(
                getattr(coord, "_battery_backend_not_pending_observed_at", None)
            ),
            "battery_pending_requested_at": _iso(
                getattr(coord, "_battery_pending_requested_at", None)
            ),
            "battery_pending_age_s": coord.battery_pending_age_seconds,
            "battery_pending_timeout_s": int(BATTERY_PROFILE_PENDING_TIMEOUT_S),
            "battery_profile_options": coord.battery_profile_option_labels,
            "battery_show_charge_from_grid": getattr(
                coord, "_battery_show_charge_from_grid", None
            ),
            "battery_type_available_for_entities": battery_type_available,
            "battery_write_access_confirmed": battery_write_access,
            "battery_controls_available": coord.battery_controls_available,
            "battery_profile_selection_available": (
                coord.battery_profile_selection_available
            ),
            "battery_reserve_editable": _safe_attr("battery_reserve_editable", False),
            "battery_shutdown_level_available": getattr(
                coord, "battery_shutdown_level_available", None
            ),
            "charge_from_grid_control_available": (
                coord.charge_from_grid_control_available
            ),
            "charge_from_grid_schedule_supported": (
                coord.charge_from_grid_schedule_supported
            ),
            "charge_from_grid_schedule_available": (
                coord.charge_from_grid_schedule_available
            ),
            "charge_from_grid_force_schedule_supported": (
                coord.charge_from_grid_force_schedule_supported
            ),
            "charge_from_grid_force_schedule_available": (
                coord.charge_from_grid_force_schedule_available
            ),
            "discharge_to_grid_schedule_supported": (
                coord.discharge_to_grid_schedule_supported
            ),
            "discharge_to_grid_schedule_available": (
                coord.discharge_to_grid_schedule_available
            ),
            "restrict_battery_discharge_schedule_supported": (
                coord.restrict_battery_discharge_schedule_supported
            ),
            "restrict_battery_discharge_schedule_available": (
                coord.restrict_battery_discharge_schedule_available
            ),
            "battery_overall_charge_sensor_available": (
                battery_sensor_base_available and battery_aggregate_charge is not None
            ),
            "battery_overall_status_sensor_available": (
                battery_sensor_base_available and battery_aggregate_status is not None
            ),
            "battery_cfg_schedule_status_sensor_available": (
                battery_sensor_base_available
                and coord.charge_from_grid_control_available
            ),
            "battery_available_energy_sensor_available": (
                battery_sensor_base_available and battery_available_energy is not None
            ),
            "battery_available_power_sensor_available": (
                battery_sensor_base_available and battery_available_power is not None
            ),
            "battery_reserve_number_available": (
                _coordinator_available()
                and battery_type_available
                and battery_write_access
                and bool(_safe_attr("battery_reserve_editable", False))
            ),
            "battery_shutdown_level_number_available": (
                _coordinator_available()
                and battery_type_available
                and battery_write_access
                and coord.battery_shutdown_level_available
            ),
            "battery_cfg_schedule_limit_number_available": (
                _coordinator_available()
                and battery_type_available
                and battery_write_access
                and _cfg_schedule_edit_available()
                and coord.battery_cfg_schedule_limit is not None
            ),
            "battery_dtg_schedule_limit_number_available": (
                _coordinator_available()
                and battery_type_available
                and battery_write_access
                and _dtg_schedule_edit_available()
                and coord.battery_dtg_schedule_limit is not None
            ),
            "battery_rbd_schedule_limit_number_available": (
                _coordinator_available()
                and battery_type_available
                and battery_write_access
                and _rbd_schedule_edit_available()
                and coord.battery_rbd_schedule_limit is not None
            ),
            "charge_from_grid_switch_available": (
                _coordinator_available()
                and battery_type_available
                and battery_write_access
                and coord.charge_from_grid_control_available
            ),
            "charge_from_grid_schedule_switch_available": (
                _coordinator_available()
                and battery_type_available
                and battery_write_access
                and coord.charge_from_grid_force_schedule_available
            ),
            "discharge_to_grid_schedule_switch_available": (
                _coordinator_available()
                and battery_type_available
                and battery_write_access
                and coord.discharge_to_grid_schedule_available
            ),
            "restrict_battery_discharge_schedule_switch_available": (
                _coordinator_available()
                and battery_type_available
                and battery_write_access
                and coord.restrict_battery_discharge_schedule_available
            ),
            "battery_show_savings_mode": getattr(
                coord, "_battery_show_savings_mode", None
            ),
            "battery_show_ai_optimisation_mode": getattr(
                coord, "_battery_show_ai_optimisation_mode", None
            ),
            "battery_is_emea": getattr(coord, "_battery_is_emea", None),
            "battery_show_storm_guard": getattr(
                coord, "_battery_show_storm_guard", None
            ),
            "battery_show_production": getattr(coord, "_battery_show_production", None),
            "battery_show_consumption": getattr(
                coord, "_battery_show_consumption", None
            ),
            "battery_show_full_backup": getattr(
                coord, "_battery_show_full_backup", None
            ),
            "battery_show_backup_percentage": getattr(
                coord, "_battery_show_battery_backup_percentage", None
            ),
            "battery_has_encharge": getattr(coord, "_battery_has_encharge", None),
            "battery_has_acb": getattr(coord, "_battery_has_acb", None),
            "battery_has_enpower": getattr(coord, "_battery_has_enpower", None),
            "battery_country_code": getattr(coord, "_battery_country_code", None),
            "battery_region": getattr(coord, "_battery_region", None),
            "battery_locale": getattr(coord, "_battery_locale", None),
            "battery_timezone": getattr(coord, "_battery_timezone", None),
            "battery_feature_details": getattr(coord, "_battery_feature_details", None),
            "battery_user_is_owner": getattr(coord, "_battery_user_is_owner", None),
            "battery_user_is_installer": getattr(
                coord, "_battery_user_is_installer", None
            ),
            "battery_site_status_code": getattr(
                coord, "_battery_site_status_code", None
            ),
            "battery_site_status_text": getattr(
                coord, "_battery_site_status_text", None
            ),
            "battery_site_status_severity": getattr(
                coord, "_battery_site_status_severity", None
            ),
            "battery_charging_modes_enabled": getattr(
                coord, "_battery_is_charging_modes_enabled", None
            ),
            "battery_status_aggregate_charge_pct": getattr(
                coord, "_battery_aggregate_charge_pct", None
            ),
            "battery_status_aggregate_state": getattr(
                coord, "_battery_aggregate_status", None
            ),
            "battery_status_storage_count": len(
                getattr(coord, "_battery_storage_data", {}) or {}
            ),
            "battery_status_storage_order": list(
                getattr(coord, "_battery_storage_order", []) or []
            ),
            "battery_status_details": dict(
                getattr(coord, "_battery_aggregate_status_details", {}) or {}
            ),
            "ac_battery_type_available_for_entities": ac_battery_type_available,
            "ac_battery_overall_status_sensor_available": (
                ac_battery_sensor_base_available
                and ac_battery_aggregate_status is not None
            ),
            "ac_battery_power_sensor_available": (
                ac_battery_sensor_base_available and ac_battery_power is not None
            ),
            "ac_battery_last_reported_sensor_available": (
                ac_battery_sensor_base_available
                and ac_battery_status_summary.get("latest_reported_utc") is not None
            ),
            "ac_battery_sleep_switch_available": (
                _coordinator_available()
                and ac_battery_type_available
                and battery_write_access
                and getattr(coord, "_battery_has_acb", None) is True
                and coord.ac_battery_sleep_state is not None
            ),
            "ac_battery_target_soc_select_available": (
                _coordinator_available()
                and ac_battery_type_available
                and battery_write_access
                and getattr(coord, "_battery_has_acb", None) is True
            ),
            "ac_battery_status_aggregate_state": getattr(
                coord, "_ac_battery_aggregate_status", None
            ),
            "ac_battery_count": len(getattr(coord, "_ac_battery_data", {}) or {}),
            "ac_battery_selected_sleep_min_soc": coord.ac_battery_selected_sleep_min_soc,
            "ac_battery_sleep_state": coord.ac_battery_sleep_state,
            "ac_battery_control_pending": coord.ac_battery_control_pending,
            "ac_battery_status_details": {
                "battery_count": ac_battery_status_summary.get("battery_count"),
                "sleep_state": ac_battery_status_summary.get("sleep_state"),
                "selected_sleep_min_soc": ac_battery_status_summary.get(
                    "selected_sleep_min_soc"
                ),
                "worst_status": ac_battery_status_summary.get("worst_status"),
                "power_w": ac_battery_status_summary.get("power_w"),
                "latest_reported_utc": ac_battery_status_summary.get(
                    "latest_reported_utc"
                ),
            },
            "ac_battery_last_command": getattr(coord, "_ac_battery_last_command", None),
            "battery_backup_history_count": len(
                getattr(coord, "_battery_backup_history_events", []) or []
            ),
            "battery_write_in_progress": bool(
                getattr(coord, "_battery_profile_write_lock", None)
                and coord._battery_profile_write_lock.locked()
            ),
            "battery_dtg_control": coord.battery_dtg_control,
            "battery_cfg_control": coord.battery_cfg_control,
            "battery_rbd_control": coord.battery_rbd_control,
            "battery_system_task": coord.battery_system_task,
            "battery_grid_mode": getattr(coord, "_battery_grid_mode", None),
            "battery_mode_display": coord.battery_mode_display,
            "battery_charge_from_grid_allowed": coord.battery_charge_from_grid_allowed,
            "battery_discharge_to_grid_allowed": coord.battery_discharge_to_grid_allowed,
            "battery_hide_charge_from_grid": getattr(
                coord, "_battery_hide_charge_from_grid", None
            ),
            "battery_envoy_supports_vls": getattr(
                coord, "_battery_envoy_supports_vls", None
            ),
            "battery_charge_from_grid": getattr(
                coord, "_battery_charge_from_grid", None
            ),
            "battery_charge_from_grid_schedule_enabled": getattr(
                coord, "_battery_charge_from_grid_schedule_enabled", None
            ),
            "battery_charge_begin_time": getattr(
                coord, "_battery_charge_begin_time", None
            ),
            "battery_charge_end_time": getattr(coord, "_battery_charge_end_time", None),
            "battery_cfg_schedule_limit": getattr(
                coord, "_battery_cfg_schedule_limit", None
            ),
            "battery_cfg_schedule_enabled": getattr(
                coord, "_battery_cfg_schedule_enabled", None
            ),
            "battery_dtg_schedule_enabled": getattr(
                coord, "_battery_dtg_schedule_enabled", None
            ),
            "battery_dtg_begin_time": getattr(coord, "_battery_dtg_begin_time", None),
            "battery_dtg_end_time": getattr(coord, "_battery_dtg_end_time", None),
            "battery_dtg_schedule_limit": getattr(
                coord, "_battery_dtg_schedule_limit", None
            ),
            "battery_dtg_schedule_status": getattr(
                coord, "_battery_dtg_schedule_status", None
            ),
            "battery_rbd_schedule_enabled": getattr(
                coord, "_battery_rbd_schedule_enabled", None
            ),
            "battery_rbd_begin_time": getattr(coord, "_battery_rbd_begin_time", None),
            "battery_rbd_end_time": getattr(coord, "_battery_rbd_end_time", None),
            "battery_rbd_schedule_limit": getattr(
                coord, "_battery_rbd_schedule_limit", None
            ),
            "battery_rbd_schedule_status": getattr(
                coord, "_battery_rbd_schedule_status", None
            ),
            "battery_schedules_payload": getattr(
                coord, "_battery_schedules_payload", None
            ),
            "battery_accepted_itc_disclaimer": getattr(
                coord, "_battery_accepted_itc_disclaimer", None
            ),
            "battery_very_low_soc": getattr(coord, "_battery_very_low_soc", None),
            "battery_very_low_soc_min": getattr(
                coord, "_battery_very_low_soc_min", None
            ),
            "battery_very_low_soc_max": getattr(
                coord, "_battery_very_low_soc_max", None
            ),
            "battery_settings_write_in_progress": bool(
                getattr(coord, "_battery_settings_write_lock", None)
                and coord._battery_settings_write_lock.locked()
            ),
            "storm_guard_state": getattr(coord, "_storm_guard_state", None),
            "storm_evse_enabled": getattr(coord, "_storm_evse_enabled", None),
            "storm_alert_active": getattr(coord, "_storm_alert_active", None),
            "storm_alert_critical_override": getattr(
                coord, "_storm_alert_critical_override", None
            ),
            "storm_alert_count": len(getattr(coord, "_storm_alerts", []) or []),
            "evse_feature_flags_available": bool(
                getattr(coord, "_evse_feature_flags_payload", None)
            ),
            "evse_feature_flag_site_keys": sorted(
                str(key)
                for key in getattr(coord, "_evse_site_feature_flags", {}).keys()
            ),
            "evse_feature_flag_charger_count": len(
                getattr(coord, "_evse_feature_flags_by_serial", {}) or {}
            ),
            "pricing_edits_enabled": coord.pricing_edits_enabled,
            "grid_control_supported": coord.grid_control_supported,
            "grid_toggle_allowed": coord.grid_toggle_allowed,
            "grid_toggle_pending": coord.grid_toggle_pending,
            "grid_toggle_blocked_reasons": coord.grid_toggle_blocked_reasons,
            "grid_control_disable": coord.grid_control_disable,
            "grid_control_active_download": coord.grid_control_active_download,
            "grid_control_sunlight_backup_system_check": coord.grid_control_sunlight_backup_system_check,
            "grid_control_grid_outage_check": coord.grid_control_grid_outage_check,
            "grid_control_user_initiated_toggle": coord.grid_control_user_initiated_toggle,
            "grid_control_fetch_failures": getattr(
                coord, "_grid_control_check_failures", 0
            ),
            "grid_control_data_stale": coord.grid_control_supported is None,
            "grid_mode_source": coord.grid_mode_source,
            "grid_mode_status_supported": coord.grid_mode_status_supported,
            "grid_mode_status_raw": coord.grid_mode_status_raw,
            "grid_mode_status_fetch_failures": getattr(
                coord, "_grid_mode_status_failures", 0
            ),
            "grid_mode_status_data_stale": coord.grid_mode_status_supported is None,
            "grid_outage_context_supported": coord.grid_outage_context_supported,
            "grid_outage_is_grid_outage": coord.grid_outage_is_grid_outage,
            "grid_outage_show_grid_connect": coord.grid_outage_show_grid_connect,
            "grid_outage_has_battery": coord.grid_outage_has_battery,
            "grid_outage_is_sunlight_backup": coord.grid_outage_is_sunlight_backup,
            "grid_outage_context_fetch_failures": getattr(
                coord, "_grid_outage_context_failures", 0
            ),
            "grid_outage_context_data_stale": (
                coord.grid_outage_context_supported is None
            ),
            "dry_contact_settings_supported": coord.dry_contact_settings_supported,
            "dry_contact_settings_contact_count": len(
                getattr(coord, "_dry_contact_settings_entries", []) or []
            ),
            "dry_contact_settings_unmatched_count": len(
                getattr(coord, "_dry_contact_unmatched_settings", []) or []
            ),
            "dry_contact_settings_fetch_failures": getattr(
                coord, "_dry_contact_settings_failures", 0
            ),
            "dry_contact_settings_data_stale": coord.dry_contact_settings_supported
            is None,
            "hems_devices_data_stale": bool(
                getattr(coord, "_hems_devices_using_stale", False)
            ),
        }

        grid_last_success = getattr(
            coord, "_grid_control_check_last_success_mono", None
        )
        if isinstance(grid_last_success, (int, float)):
            age = time.monotonic() - float(grid_last_success)
            if age >= 0:
                metrics["grid_control_last_success_age_s"] = round(age, 1)

        grid_mode_last_success = getattr(
            coord, "_grid_mode_status_last_success_mono", None
        )
        if isinstance(grid_mode_last_success, (int, float)):
            age = time.monotonic() - float(grid_mode_last_success)
            if age >= 0:
                metrics["grid_mode_status_last_success_age_s"] = round(age, 1)

        grid_outage_last_success = getattr(
            coord, "_grid_outage_context_last_success_mono", None
        )
        if isinstance(grid_outage_last_success, (int, float)):
            age = time.monotonic() - float(grid_outage_last_success)
            if age >= 0:
                metrics["grid_outage_context_last_success_age_s"] = round(age, 1)

        dry_contacts_last_success = getattr(
            coord, "_dry_contact_settings_last_success_mono", None
        )
        if isinstance(dry_contacts_last_success, (int, float)):
            age = time.monotonic() - float(dry_contacts_last_success)
            if age >= 0:
                metrics["dry_contact_settings_last_success_age_s"] = round(age, 1)

        hems_last_success = getattr(coord, "_hems_devices_last_success_mono", None)
        if isinstance(hems_last_success, (int, float)):
            age = time.monotonic() - float(hems_last_success)
            if age >= 0:
                metrics["hems_devices_last_success_age_s"] = round(age, 1)

        metrics["hems_devices_last_success_utc"] = _iso(
            getattr(coord, "_hems_devices_last_success_utc", None)
        )

        session_manager = getattr(coord, "session_history", None)
        if session_manager is not None:
            metrics["session_history_available"] = getattr(
                session_manager, "service_available", None
            )
            metrics["session_history_failures"] = getattr(
                session_manager, "service_failures", None
            )
            metrics["session_history_last_error"] = getattr(
                session_manager, "service_last_error", None
            )
            metrics["session_history_last_failure"] = _iso(
                getattr(session_manager, "service_last_failure_utc", None)
            )
            metrics["session_history_backoff_active"] = getattr(
                session_manager, "service_backoff_active", None
            )
            metrics["session_history_backoff_ends_utc"] = _iso(
                getattr(session_manager, "service_backoff_ends_utc", None)
            )

        energy_manager = getattr(coord, "energy", None)
        if energy_manager is not None:
            metrics["site_energy_available"] = getattr(
                energy_manager, "service_available", None
            )
            metrics["site_energy_failures"] = getattr(
                energy_manager, "service_failures", None
            )
            metrics["site_energy_last_error"] = getattr(
                energy_manager, "service_last_error", None
            )
            metrics["site_energy_last_failure"] = _iso(
                getattr(energy_manager, "service_last_failure_utc", None)
            )
            metrics["site_energy_backoff_active"] = getattr(
                energy_manager, "service_backoff_active", None
            )
            metrics["site_energy_backoff_ends_utc"] = _iso(
                getattr(energy_manager, "service_backoff_ends_utc", None)
            )

        evse_timeseries = getattr(coord, "evse_timeseries", None)
        if evse_timeseries is not None:
            metrics["evse_timeseries_available"] = getattr(
                evse_timeseries, "service_available", None
            )
            metrics["evse_timeseries_failures"] = getattr(
                evse_timeseries, "service_failures", None
            )
            metrics["evse_timeseries_last_error"] = getattr(
                evse_timeseries, "service_last_error", None
            )
            metrics["evse_timeseries_last_failure"] = _iso(
                getattr(evse_timeseries, "service_last_failure_utc", None)
            )
            metrics["evse_timeseries_backoff_active"] = getattr(
                evse_timeseries, "service_backoff_active", None
            )
            metrics["evse_timeseries_backoff_ends_utc"] = _iso(
                getattr(evse_timeseries, "service_backoff_ends_utc", None)
            )

        site_energy_age = None
        site_flows: dict[str, object] = {}
        site_meta: dict[str, object] = {}
        if energy_manager is not None:
            site_energy_age = getattr(energy_manager, "site_energy_cache_age", None)
            raw_flows = getattr(energy_manager, "site_energy", None)
            raw_meta = getattr(energy_manager, "site_energy_meta", None)
            if isinstance(raw_flows, dict):
                site_flows = cast(dict[str, object], raw_flows)
            if isinstance(raw_meta, dict):
                site_meta = cast(dict[str, object], raw_meta)
        if site_flows or site_energy_age is not None or site_meta:
            site_last_report = site_meta.get("last_report_date")
            metrics["site_energy"] = {
                "flows": sorted(list(site_flows.keys())),
                "cache_age_s": (
                    round(site_energy_age, 3) if site_energy_age is not None else None
                ),
                "start_date": site_meta.get("start_date"),
                "last_report_date": _iso(
                    site_last_report if isinstance(site_last_report, datetime) else None
                ),
                "update_pending": site_meta.get("update_pending"),
                "interval_minutes": site_meta.get("interval_minutes"),
            }

        if evse_timeseries is not None:
            metrics["evse_timeseries"] = evse_timeseries.diagnostics()

        def _endpoint_family_degraded(state: object) -> bool:
            if not isinstance(state, dict):
                return False
            degraded = state.get("degraded")
            if isinstance(degraded, bool):
                return degraded
            try:
                failures = int(state.get("consecutive_failures") or 0)
            except (TypeError, ValueError):
                failures = 0
            return (
                bool(state.get("cooldown_active"))
                or failures > 0
                or bool(state.get("suppressed"))
            )

        degraded_endpoint_families = [
            family
            for family, state in endpoint_family_health.items()
            if _endpoint_family_degraded(state)
        ]
        metrics["degraded_endpoint_families"] = degraded_endpoint_families
        metrics["endpoint_failure_details"] = {
            family: {
                "reason": state.get("last_error"),
                "retry_utc": state.get("next_retry_utc"),
            }
            for family, state in endpoint_family_health.items()
            if isinstance(state, dict)
            and isinstance(state.get("last_error"), str)
            and bool(state.get("last_error"))
        }

        degraded_services = [
            service
            for service, available_key in (
                ("scheduler", "scheduler_available"),
                ("auth_settings", "auth_settings_available"),
                ("session_history", "session_history_available"),
                ("site_energy", "site_energy_available"),
                ("evse_timeseries", "evse_timeseries_available"),
            )
            if metrics.get(available_key) is False
        ]
        if tariff_service_status == "degraded":
            degraded_services.append(TARIFF_ENDPOINT_FAMILY)
        if metrics.get("hems_auth_circuit_active"):
            degraded_services.append("hems_auth")
        degraded_services.extend(
            family
            for family in degraded_endpoint_families
            if family not in degraded_services
        )
        metrics["degraded_services"] = degraded_services

        firmware_catalog_manager = getattr(coord, "firmware_catalog_manager", None)
        status_snapshot = getattr(firmware_catalog_manager, "status_snapshot", None)
        if callable(status_snapshot):
            try:
                status = status_snapshot()
            except Exception:  # noqa: BLE001
                status = {}
            if isinstance(status, dict):
                metrics["firmware_catalog_last_fetch_utc"] = status.get(
                    "last_fetch_utc"
                )
                metrics["firmware_catalog_last_success_utc"] = status.get(
                    "last_success_utc"
                )
                metrics["firmware_catalog_last_error"] = status.get("last_error")
                metrics["firmware_catalog_using_stale"] = status.get("using_stale")
                metrics["firmware_catalog_generated_at"] = status.get(
                    "catalog_generated_at"
                )
                metrics["firmware_catalog_source_age_seconds"] = status.get(
                    "catalog_source_age_seconds"
                )

        return metrics

    def issue_translation_placeholders(
        self, metrics: dict[str, object]
    ) -> dict[str, str]:
        coord = self.coordinator
        placeholders: dict[str, str] = {"site_id": str(coord.site_id)}
        site_name = metrics.get("site_name")
        if site_name:
            placeholders["site_name"] = str(site_name)
        last_error = metrics.get("last_error") or metrics.get(
            "last_failure_description"
        )
        if last_error:
            placeholders["last_error"] = str(last_error)
        status = metrics.get("last_failure_status")
        if status is not None:
            placeholders["last_status"] = str(status)
        blocked_until = metrics.get("auth_blocked_until")
        if blocked_until:
            placeholders["blocked_until"] = str(blocked_until)
        hems_backoff_until = metrics.get("hems_auth_backoff_until")
        if hems_backoff_until:
            placeholders["hems_backoff_until"] = str(hems_backoff_until)
            placeholders["backoff_until"] = str(hems_backoff_until)
        hems_failure_count = metrics.get("hems_auth_failure_count")
        if hems_failure_count is not None:
            placeholders["hems_failure_count"] = str(hems_failure_count)
            placeholders["failure_count"] = str(hems_failure_count)
        hems_reason = metrics.get("hems_auth_last_reason")
        if hems_reason:
            placeholders["hems_reason"] = str(hems_reason)
            placeholders["reason"] = str(hems_reason)
        backoff_ends = metrics.get("backoff_ends_utc")
        if backoff_ends:
            placeholders["backoff_ends"] = str(backoff_ends)
        return placeholders

    def issue_context(self) -> tuple[dict[str, object], dict[str, str]]:
        metrics = self.collect_site_metrics()
        return metrics, self.issue_translation_placeholders(metrics)

    def payload_health_state(self, name: str) -> dict[str, object]:
        coord = self.coordinator
        state = coord._payload_health.get(name)
        if state is None:
            state = {
                "available": True,
                "using_stale": False,
                "failures": 0,
                "last_success_utc": None,
                "last_success_mono": None,
                "last_failure_utc": None,
                "last_error": None,
                "last_payload_signature": None,
            }
            coord._payload_health[name] = state
        return cast(dict[str, object], state)

    def mark_payload_endpoint_success(
        self,
        name: str,
        *,
        success_mono: float | None = None,
        success_utc: datetime | None = None,
    ) -> None:
        state = self.payload_health_state(name)
        state["available"] = True
        state["using_stale"] = False
        state["failures"] = 0
        state["last_error"] = None
        state["last_failure_utc"] = None
        state["last_payload_signature"] = None
        state["last_success_mono"] = (
            success_mono if success_mono is not None else time.monotonic()
        )
        state["last_success_utc"] = (
            success_utc if success_utc is not None else dt_util.utcnow()
        )

    def note_payload_endpoint_failure(
        self,
        name: str,
        *,
        error: str,
        signature: dict[str, object] | None = None,
        using_stale: bool = False,
    ) -> None:
        state = self.payload_health_state(name)
        state["available"] = False
        state["using_stale"] = using_stale
        state["failures"] = int(str(state.get("failures", 0) or 0)) + 1
        state["last_error"] = error
        state["last_failure_utc"] = dt_util.utcnow()
        state["last_payload_signature"] = (
            dict(signature) if isinstance(signature, dict) else None
        )

    def payload_endpoint_reusable(self, name: str, stale_after_s: float) -> bool:
        state = self.payload_health_state(name)
        last_success_mono = state.get("last_success_mono")
        if not isinstance(last_success_mono, (int, float)):
            return False
        try:
            age = time.monotonic() - float(last_success_mono)
        except Exception:
            return False
        if age < 0:
            return True
        return age < max(1.0, float(stale_after_s))

    def status_stale_window_s(self) -> float:
        coord = self.coordinator
        return float(max(1, 2 * coord._slow_interval_floor()))

    def payload_health_diagnostics(self) -> dict[str, object]:
        """Return diagnostics-safe payload health details."""

        coord = self.coordinator

        def _signature_copy(signature: object) -> dict[str, object] | None:
            if not isinstance(signature, dict):
                return None
            out = dict(signature)
            endpoint = out.get("endpoint")
            if endpoint is not None:
                out["endpoint"] = redact_text(endpoint, site_ids=(coord.site_id,))
            return out

        out: dict[str, object] = {}
        payload_health = getattr(coord, "_payload_health", {})
        if not isinstance(payload_health, dict):
            payload_health = {}
        for name, state in payload_health.items():
            last_success_age_s = None
            last_success_mono = state.get("last_success_mono")
            if isinstance(last_success_mono, (int, float)):
                try:
                    age = time.monotonic() - float(last_success_mono)
                except Exception:
                    age = None
                if age is not None and age >= 0:
                    last_success_age_s = round(age, 3)
            last_success_utc = state.get("last_success_utc")
            last_failure_utc = state.get("last_failure_utc")
            out[name] = {
                "available": bool(state.get("available", True)),
                "using_stale": bool(state.get("using_stale", False)),
                "failures": int(state.get("failures", 0) or 0),
                "last_error": state.get("last_error"),
                "last_success_utc": (
                    last_success_utc.isoformat()
                    if isinstance(last_success_utc, datetime)
                    else None
                ),
                "last_success_age_s": last_success_age_s,
                "last_failure_utc": (
                    last_failure_utc.isoformat()
                    if isinstance(last_failure_utc, datetime)
                    else None
                ),
                "last_payload_signature": _signature_copy(
                    state.get("last_payload_signature")
                ),
            }
        try:
            out["summary_v2"] = coord.summary.diagnostics()
        except Exception:  # noqa: BLE001
            pass
        session_history = getattr(coord, "session_history", None)
        if session_history is not None:
            last_failure_utc = getattr(
                session_history, "service_last_failure_utc", None
            )
            out["session_history"] = {
                "available": getattr(session_history, "service_available", None),
                "using_stale": getattr(session_history, "service_using_stale", None),
                "failures": getattr(session_history, "service_failures", None),
                "last_error": getattr(session_history, "service_last_error", None),
                "last_failure_utc": (
                    last_failure_utc.isoformat()
                    if isinstance(last_failure_utc, datetime)
                    else None
                ),
                "last_payload_signature": _signature_copy(
                    getattr(
                        session_history,
                        "_service_last_payload_signature",
                        None,
                    )
                ),
                "cache_state_counts": _signature_copy(
                    getattr(
                        session_history,
                        "cache_state_counts",
                        lambda: None,
                    )()
                ),
            }
        evse_timeseries = getattr(coord, "evse_timeseries", None)
        if evse_timeseries is not None:
            try:
                out["evse_timeseries"] = evse_timeseries.diagnostics()
            except Exception:  # noqa: BLE001
                pass
        return out

    def endpoint_family_health_diagnostics(self) -> dict[str, object]:
        """Return diagnostics-safe endpoint family health details."""

        coord = self.coordinator

        def _iso(value: datetime | None) -> str | None:
            if not value:
                return None
            try:
                return value.isoformat()
            except Exception:
                return str(value)

        out: dict[str, object] = {}
        health_map = getattr(coord, "_endpoint_family_health", {})
        if not isinstance(health_map, dict):
            return out
        for family, state in health_map.items():
            if not isinstance(family, str):
                continue
            if state is None:
                continue
            support_state = getattr(state, "support_state", "unknown")
            family_health: dict[str, object] = {
                "family": family,
                "request_count": int(getattr(state, "request_count", 0) or 0),
                "last_request_utc": _iso(getattr(state, "last_request_utc", None)),
                "consecutive_failures": int(
                    getattr(state, "consecutive_failures", 0) or 0
                ),
                "last_status": getattr(state, "last_status", None),
                "last_success_utc": _iso(getattr(state, "last_success_utc", None)),
                "last_failure_utc": _iso(getattr(state, "last_failure_utc", None)),
                "next_retry_utc": _iso(getattr(state, "next_retry_utc", None)),
                "cooldown_active": bool(getattr(state, "cooldown_active", False)),
                "support_state": support_state,
                "suppressed": support_state == "suppressed",
                "last_error": getattr(state, "last_error", None),
            }
            degraded = getattr(state, "degraded", None)
            if isinstance(degraded, bool):
                family_health.update(
                    {
                        "degraded": degraded,
                        "partial_success": bool(
                            getattr(state, "partial_success", False)
                        ),
                        "successful_items": getattr(state, "successful_items", None),
                        "total_items": getattr(state, "total_items", None),
                        "using_cached_data": bool(
                            getattr(state, "using_cached_data", False)
                        ),
                        "cache_stale": bool(getattr(state, "cache_stale", False)),
                    }
                )
            out[family] = family_health
        return out

    def sync_session_history_issue(self) -> None:
        coord = self.coordinator
        manager = getattr(coord, "session_history", None)
        if manager is None:
            return
        available = getattr(manager, "service_available", True)
        has_current_day_unavailable = False
        if available and hasattr(manager, "get_cache_view"):
            data = getattr(coord, "data", None)
            if isinstance(data, dict) and data:
                try:
                    day_ref = dt_util.now()
                except Exception:
                    day_ref = datetime.now()
                try:
                    day_local_default = dt_util.as_local(day_ref)
                except Exception:
                    if day_ref.tzinfo is None:
                        day_ref = day_ref.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
                    day_local_default = day_ref
                for sn, payload in data.items():
                    if not isinstance(payload, dict):
                        continue
                    try:
                        history_day = coord._session_history_day(
                            payload, day_local_default
                        )
                    except Exception:
                        history_day = day_local_default
                    try:
                        view = manager.get_cache_view(
                            sn, history_day.strftime("%Y-%m-%d")
                        )
                    except Exception:
                        continue
                    if bool(getattr(view, "has_valid_cache", False)):
                        continue
                    if getattr(view, "state", None) == "unavailable" and (
                        getattr(view, "last_error", None)
                        or getattr(view, "cache_age", None) is not None
                    ):
                        has_current_day_unavailable = True
                        break
        if available and not has_current_day_unavailable:
            self._clear_reported_issue(
                "_session_history_issue_reported",
                ISSUE_SESSION_HISTORY_UNAVAILABLE,
            )
            return
        self._report_flagged_issue(
            "_session_history_issue_reported",
            ISSUE_SESSION_HISTORY_UNAVAILABLE,
            severity=ir.IssueSeverity.WARNING,
        )

    def sync_site_energy_issue(self) -> None:
        coord = self.coordinator
        energy = getattr(coord, "energy", None)
        if energy is None:
            return
        available = getattr(energy, "service_available", True)
        if available:
            self._clear_reported_issue(
                "_site_energy_issue_reported",
                ISSUE_SITE_ENERGY_UNAVAILABLE,
            )
            return
        self._report_flagged_issue(
            "_site_energy_issue_reported",
            ISSUE_SITE_ENERGY_UNAVAILABLE,
            severity=ir.IssueSeverity.WARNING,
        )

    def sync_battery_profile_pending_issue(self) -> None:
        coord = self.coordinator
        pending_profile = getattr(coord, "_battery_pending_profile", None)
        requested_at = getattr(coord, "_battery_pending_requested_at", None)
        age_s = coord.battery_pending_age_seconds
        pending_overdue = bool(
            pending_profile
            and requested_at is not None
            and age_s is not None
            and age_s >= int(BATTERY_PROFILE_PENDING_TIMEOUT_S)
        )
        if not pending_overdue:
            self._clear_reported_issue(
                "_battery_profile_issue_reported",
                ISSUE_BATTERY_PROFILE_PENDING,
            )
            return
        placeholders = {
            "pending_timeout_minutes": str(int(BATTERY_PROFILE_PENDING_TIMEOUT_S // 60))
        }
        if age_s is not None:
            placeholders["pending_age_minutes"] = str(max(1, age_s // 60))
        self._report_flagged_issue(
            "_battery_profile_issue_reported",
            ISSUE_BATTERY_PROFILE_PENDING,
            severity=ir.IssueSeverity.WARNING,
            placeholders=placeholders,
        )

    def clear_reauth_issue(self) -> None:
        self._delete_issue(ISSUE_REAUTH_REQUIRED)
        self._delete_issue(ISSUE_AUTH_BLOCKED)
        self._delete_issue(ISSUE_TOO_MANY_ACTIVE_SESSIONS)
        self.coordinator._auth_block_issue_reported = False

    def create_reauth_issue(self) -> None:
        self.clear_auth_block_issue()
        self._create_site_metrics_issue(
            ISSUE_REAUTH_REQUIRED,
            severity=ir.IssueSeverity.ERROR,
        )

    def clear_auth_block_issue(self) -> None:
        self._clear_reported_issue("_auth_block_issue_reported", ISSUE_AUTH_BLOCKED)
        self._delete_issue(ISSUE_TOO_MANY_ACTIVE_SESSIONS)

    def clear_hems_auth_degraded_issue(self) -> None:
        self._clear_reported_issue(
            "_hems_auth_issue_reported",
            ISSUE_HEMS_AUTH_DEGRADED,
        )

    def create_hems_auth_degraded_issue(self) -> None:
        coord = self.coordinator
        if self._degraded_service_issue_suppressed(
            "_hems_auth_issue_reported",
            ISSUE_HEMS_AUTH_DEGRADED,
        ):
            return
        placeholders: dict[str, str] = {}
        backoff_until = coord._format_auth_blocked_until(
            getattr(coord, "_hems_auth_backoff_ends_utc", None)
        )
        if backoff_until:
            placeholders["backoff_until"] = backoff_until
        placeholders["failure_count"] = str(
            int(getattr(coord, "_hems_auth_failure_count", 0) or 0)
        )
        reason = getattr(coord, "_hems_auth_last_reason", None)
        if reason:
            placeholders["reason"] = str(reason)
        # Backoff expiry and failure count change as repeated HEMS auth failures
        # escalate, so refresh the existing repair issue instead of keeping stale
        # placeholders from the first failure.
        self._create_site_metrics_issue(
            ISSUE_HEMS_AUTH_DEGRADED,
            severity=ir.IssueSeverity.WARNING,
            placeholders=placeholders,
        )
        coord._hems_auth_issue_reported = True

    def create_auth_block_issue(self) -> None:
        coord = self.coordinator
        issue_id = (
            ISSUE_TOO_MANY_ACTIVE_SESSIONS
            if getattr(coord, "_auth_block_reason", None) == "too_many_active_sessions"
            else ISSUE_AUTH_BLOCKED
        )
        stale_issue_id = (
            ISSUE_AUTH_BLOCKED
            if issue_id == ISSUE_TOO_MANY_ACTIVE_SESSIONS
            else ISSUE_TOO_MANY_ACTIVE_SESSIONS
        )
        placeholders: dict[str, str] = {}
        blocked_until = coord._format_auth_blocked_until(
            getattr(coord, "_auth_blocked_until_utc", None)
        )
        if blocked_until:
            placeholders["blocked_until"] = blocked_until
        self._delete_issue(ISSUE_REAUTH_REQUIRED)
        self._delete_issue(stale_issue_id)
        coord._auth_block_issue_reported = False
        self._report_flagged_issue(
            "_auth_block_issue_reported",
            issue_id,
            severity=ir.IssueSeverity.ERROR,
            placeholders=placeholders,
        )

    def clear_network_issue(self) -> None:
        self._clear_reported_issue(
            "_network_issue_reported",
            ISSUE_NETWORK_UNREACHABLE,
        )

    def report_network_issue(self) -> None:
        self._report_flagged_issue(
            "_network_issue_reported",
            ISSUE_NETWORK_UNREACHABLE,
            severity=ir.IssueSeverity.WARNING,
        )

    def clear_cloud_issue(self) -> None:
        self._clear_reported_issue(
            "_cloud_issue_reported",
            ISSUE_CLOUD_ERRORS,
        )

    def report_cloud_issue(self) -> None:
        self._report_flagged_issue(
            "_cloud_issue_reported",
            ISSUE_CLOUD_ERRORS,
            severity=ir.IssueSeverity.WARNING,
        )

    def clear_dns_issue(self) -> None:
        self._clear_reported_issue(
            "_dns_issue_reported",
            ISSUE_DNS_RESOLUTION,
        )

    def report_dns_issue(self) -> None:
        self._report_flagged_issue(
            "_dns_issue_reported",
            ISSUE_DNS_RESOLUTION,
            severity=ir.IssueSeverity.WARNING,
        )

    def create_rate_limited_issue(self) -> None:
        placeholders: dict[str, str] = {}
        backoff_ends = self.coordinator._format_auth_blocked_until(
            getattr(self.coordinator, "backoff_ends_utc", None)
        )
        if backoff_ends:
            placeholders["backoff_ends"] = backoff_ends
        self._report_flagged_issue(
            "_rate_limit_issue_reported",
            ISSUE_RATE_LIMITED,
            severity=ir.IssueSeverity.WARNING,
            placeholders=placeholders,
        )

    def clear_rate_limited_issue(self) -> None:
        coord = self.coordinator
        if getattr(coord, "_rate_limit_issue_reported", False):
            self._delete_issue(ISSUE_RATE_LIMITED)
            coord._rate_limit_issue_reported = False
            coord._rate_limit_issue_clear_checked = True
            return
        if getattr(coord, "_rate_limit_issue_clear_checked", False):
            return
        self._delete_issue(ISSUE_RATE_LIMITED)
        coord._rate_limit_issue_clear_checked = True

    def clear_scheduler_issue(self) -> None:
        self._clear_reported_issue(
            "_scheduler_issue_reported",
            ISSUE_SCHEDULER_UNAVAILABLE,
        )

    def report_scheduler_issue(self) -> None:
        self._report_flagged_issue(
            "_scheduler_issue_reported",
            ISSUE_SCHEDULER_UNAVAILABLE,
            severity=ir.IssueSeverity.WARNING,
        )

    def clear_auth_settings_issue(self) -> None:
        self._clear_reported_issue(
            "_auth_settings_issue_reported",
            ISSUE_AUTH_SETTINGS_UNAVAILABLE,
        )

    def report_auth_settings_issue(self) -> None:
        self._report_flagged_issue(
            "_auth_settings_issue_reported",
            ISSUE_AUTH_SETTINGS_UNAVAILABLE,
            severity=ir.IssueSeverity.WARNING,
        )
