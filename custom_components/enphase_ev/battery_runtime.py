"""Coordinate Enphase battery controls, schedules, caches, and diagnostics."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import time as dt_time, timedelta
from datetime import timezone as _tz
from http import HTTPStatus
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .ac_battery_runtime import AcBatteryRuntime
from .battery_schedule_editor import (
    battery_schedule_overlap_message,
    battery_schedule_overlap_placeholders,
    battery_schedule_overlap_record,
)
from .battery_runtime_dry_contact import (
    copy_dry_contact_settings_entry,
    dry_contact_identity_candidates,
    dry_contact_identity_map,
    dry_contact_match_conflicts,
    dry_contact_member_dedupe_key,
    dry_contact_member_is_dry_contact,
    dry_contact_settings_looks_like_entry,
    match_dry_contact_settings,
    normalize_dry_contact_schedule_windows,
    parse_dry_contact_settings_payload,
)
from .const import (
    BATTERY_BACKUP_HISTORY_CACHE_TTL,
    BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL,
    BATTERY_MIN_SOC_FALLBACK,
    BATTERY_PROFILE_DEFAULT_RESERVE,
    BATTERY_PROFILE_PENDING_TIMEOUT_S,
    BATTERY_PROFILE_WRITE_DEBOUNCE_S,
    BATTERY_SETTINGS_CACHE_TTL,
    BATTERY_SETTINGS_WRITE_DEBOUNCE_S,
    BATTERY_SITE_SETTINGS_CACHE_TTL,
    DRY_CONTACT_SETTINGS_CACHE_TTL,
    DRY_CONTACT_SETTINGS_FAILURE_CACHE_TTL,
    DRY_CONTACT_SETTINGS_STALE_AFTER_S,
    DOMAIN,
    FAST_TOGGLE_POLL_HOLD_S,
    GRID_CONTROL_CHECK_CACHE_TTL,
    GRID_CONTROL_CHECK_STALE_AFTER_S,
    SAVINGS_OPERATION_MODE_SUBTYPE,
    STORM_ALERT_CACHE_TTL,
    STORM_ALERT_INACTIVE_STATUSES,
    STORM_GUARD_CACHE_TTL,
    STORM_GUARD_PENDING_HOLD_S,
)
from .device_types import member_is_retired, sanitize_member
from .labels import battery_profile_label as translated_battery_profile_label
from .log_redaction import redact_identifier, redact_site_id, redact_text
from .parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_float,
    coerce_optional_text,
    parse_inverter_last_report,
)
from .runtime_helpers import coerce_int, coerce_optional_int
from .service_validation import raise_translated_service_validation
from .state_models import BatteryControlCapability

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)

_PROFILE_RECOVERY_RESERVE_RESTORE_DELAY_S = 75.0
_LIVE_BATTERY_MODE_PROFILE_ALIASES = {
    "self-consumption": "self-consumption",
    "self consumption": "self-consumption",
    "self_consumption": "self-consumption",
    "full backup": "backup_only",
    "full-backup": "backup_only",
    "backup": "backup_only",
    "backup only": "backup_only",
    "backup_only": "backup_only",
    "savings": "cost_savings",
    "cost savings": "cost_savings",
    "cost_savings": "cost_savings",
    "ai optimisation": "ai_optimisation",
    "ai optimization": "ai_optimisation",
    "ai_optimisation": "ai_optimisation",
    "ai_optimization": "ai_optimisation",
}


class BatteryRuntime:
    """Battery profile selection and pending-state helpers."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self._ac_battery_runtime = AcBatteryRuntime(self)

    @property
    def battery_state(self) -> object:
        """Return the explicit battery state bag when available."""

        return getattr(self.coordinator, "battery_state", self.coordinator)

    def _normalize_battery_sub_type(self, value: object) -> str | None:
        coord = self.coordinator
        func = getattr(coord, "normalize_battery_sub_type", None)
        if callable(func):
            return func(value)
        func = getattr(coord, "_normalize_battery_sub_type", None)
        if callable(func):
            return func(value)
        return None

    def _sync_battery_profile_pending_issue(self) -> None:
        coord = self.coordinator
        func = getattr(coord, "sync_battery_profile_pending_issue", None)
        if callable(func):
            func()
            return
        func = getattr(coord, "_sync_battery_profile_pending_issue", None)
        if callable(func):
            func()

    def _coerce_int(self, value: object, *, default: int = 0) -> int:
        return coerce_int(value, default=default)

    def _coerce_optional_bool(self, value: object) -> bool | None:
        return coerce_optional_bool(value)

    def _coerce_optional_text(self, value: object) -> str | None:
        return coerce_optional_text(value)

    def _coerce_optional_int(self, value: object) -> int | None:
        return coerce_optional_int(value)

    def _coerce_optional_float(self, value: object) -> float | None:
        return coerce_optional_float(value)

    def _coerce_optional_kwh(self, value: object) -> float | None:
        func = getattr(self.coordinator, "_coerce_optional_kwh", None)
        if callable(func):
            return func(value)
        return None

    def _parse_percent_value(self, value: object) -> float | None:
        func = getattr(self.coordinator, "_parse_percent_value", None)
        if callable(func):
            return func(value)
        return None

    def _normalize_battery_status_text(self, value: object) -> str | None:
        func = getattr(self.coordinator, "_normalize_battery_status_text", None)
        if callable(func):
            return func(value)
        return None

    def _battery_status_severity_value(self, status: str | None) -> int:
        func = getattr(self.coordinator, "_battery_status_severity_value", None)
        if callable(func):
            return func(status)
        return 0

    def _battery_storage_key(self, payload: dict[str, object]) -> str | None:
        func = getattr(self.coordinator, "_battery_storage_key", None)
        if callable(func):
            return func(payload)
        return None

    def _normalize_battery_id(self, value: object) -> str | None:
        func = getattr(self.coordinator, "_normalize_battery_id", None)
        if callable(func):
            return func(value)
        return None

    def parse_ac_battery_devices_page(self, html_text: object) -> None:
        self._ac_battery_runtime.parse_ac_battery_devices_page(html_text)

    def parse_ac_battery_show_stat_data(
        self,
        serial: str,
        battery_id: str | None,
        html_text: object,
    ) -> dict[str, object]:
        return self._ac_battery_runtime.parse_ac_battery_show_stat_data(
            serial, battery_id, html_text
        )

    def _refresh_ac_battery_summary(self) -> None:
        self._ac_battery_runtime.refresh_ac_battery_summary()

    async def async_refresh_ac_battery_devices(self, *, force: bool = False) -> None:
        await self._ac_battery_runtime.async_refresh_ac_battery_devices(force=force)

    async def async_refresh_ac_battery_telemetry(self, *, force: bool = False) -> None:
        await self._ac_battery_runtime.async_refresh_ac_battery_telemetry(force=force)

    async def async_refresh_ac_battery_events(self, *, force: bool = False) -> None:
        await self._ac_battery_runtime.async_refresh_ac_battery_events(force=force)

    async def async_set_ac_battery_sleep_mode(self, enabled: bool) -> None:
        await self._ac_battery_runtime.async_set_ac_battery_sleep_mode(enabled)

    async def async_set_ac_battery_target_soc(self, value: int) -> None:
        await self._ac_battery_runtime.async_set_ac_battery_target_soc(value)

    def _refresh_cached_topology(self) -> None:
        func = getattr(self.coordinator, "_refresh_cached_topology", None)
        if callable(func):
            func()

    def _normalize_battery_grid_mode(self, value: object) -> str | None:
        func = getattr(self.coordinator, "_normalize_battery_grid_mode", None)
        if callable(func):
            return func(value)
        return None

    def _normalize_minutes_of_day(self, value: object) -> int | None:
        func = getattr(self.coordinator, "_normalize_minutes_of_day", None)
        if callable(func):
            return func(value)
        return None

    def _copy_dry_contact_settings_entry(
        self, entry: dict[str, object]
    ) -> dict[str, object]:
        return copy_dry_contact_settings_entry(entry)

    def _dry_contact_settings_looks_like_entry(self, entry: object) -> bool:
        return dry_contact_settings_looks_like_entry(entry)

    def _normalize_dry_contact_schedule_windows(
        self, windows: object
    ) -> list[dict[str, object]]:
        return normalize_dry_contact_schedule_windows(
            windows, self._coerce_optional_text
        )

    def _dry_contact_identity_candidates(
        self,
        value: dict[str, object],
    ) -> list[tuple[str, str]]:
        return dry_contact_identity_candidates(value, self._coerce_optional_text)

    def _dry_contact_identity_map(self, value: dict[str, object]) -> dict[str, str]:
        return dry_contact_identity_map(value, self._coerce_optional_text)

    @staticmethod
    def _dry_contact_member_dedupe_key(
        identities: dict[str, str], index: int
    ) -> tuple[tuple[str, str], ...]:
        return dry_contact_member_dedupe_key(identities, index)

    @staticmethod
    def _dry_contact_match_conflicts(
        member_identities: dict[str, str],
        entry_identities: dict[str, str],
    ) -> bool:
        return dry_contact_match_conflicts(member_identities, entry_identities)

    def _dry_contact_member_is_dry_contact(self, member: object) -> bool:
        return dry_contact_member_is_dry_contact(member, self._coerce_optional_text)

    def _dry_contact_members_for_settings(self) -> list[dict[str, object]]:
        members: list[dict[str, object]] = []
        seen_keys: set[tuple[tuple[str, str], ...]] = set()

        def _append_member(raw_member: object) -> None:
            if not isinstance(raw_member, dict):
                return
            if member_is_retired(raw_member):
                return
            sanitized = sanitize_member(raw_member)
            if not sanitized:
                return
            identities = self._dry_contact_identity_map(sanitized)
            dedupe_key = self._dry_contact_member_dedupe_key(identities, len(members))
            if dedupe_key in seen_keys:
                return
            seen_keys.add(dedupe_key)
            members.append(sanitized)

        envoy_bucket = self.coordinator.inventory_view.type_bucket("envoy")
        if envoy_bucket is None:
            envoy_bucket = {}
        envoy_members = (
            envoy_bucket.get("devices") if isinstance(envoy_bucket, dict) else None
        )
        if isinstance(envoy_members, list):
            for member in envoy_members:
                if self._dry_contact_member_is_dry_contact(member):
                    _append_member(member)

        dry_bucket = self.coordinator.inventory_view.type_bucket("dry_contact")
        if dry_bucket is None:
            dry_bucket = {}
        dry_members = (
            dry_bucket.get("devices") if isinstance(dry_bucket, dict) else None
        )
        if isinstance(dry_members, list):
            for member in dry_members:
                _append_member(member)

        return members

    def _match_dry_contact_settings(
        self,
        members: list[dict[str, object]],
        *,
        settings_entries: list[dict[str, object]],
    ) -> tuple[list[dict[str, object] | None], list[dict[str, object]]]:
        return match_dry_contact_settings(
            members,
            settings_entries=settings_entries,
            coerce_text=self._coerce_optional_text,
        )

    def _current_schedule_window_from_coordinator(
        self,
    ) -> tuple[int | None, int | None]:
        coord = self.coordinator
        func = getattr(coord, "current_charge_from_grid_schedule_window", None)
        if callable(func):
            return func()
        func = getattr(coord, "_current_charge_from_grid_schedule_window", None)
        if callable(func):
            return func()
        return self.current_charge_from_grid_schedule_window()

    @staticmethod
    def normalize_battery_profile_key(value: object) -> str | None:
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        if normalized in (
            "ai_optimisation",
            "ai_optimization",
            "ai optimization",
            "ai-optimization",
        ):
            return "ai_optimisation"
        return normalized or None

    def normalize_live_battery_mode_profile(self, value: object) -> str | None:
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        if not normalized:
            return None
        profile = _LIVE_BATTERY_MODE_PROFILE_ALIASES.get(normalized)
        if profile == "cost_savings":
            # Enphase can report both Cost Savings and AI Optimization as the
            # same live savings mode, so preserve the selected profile when known.
            for attr in ("_battery_pending_profile", "_battery_profile"):
                candidate = getattr(self.battery_state, attr, None)
                if candidate in {"cost_savings", "ai_optimisation"}:
                    return candidate
        return profile or self.normalize_battery_profile_key(normalized)

    @staticmethod
    def battery_profile_label(
        profile: str | None, hass: object | None = None
    ) -> str | None:
        return translated_battery_profile_label(profile, hass=hass)

    @staticmethod
    def _normalize_pending_sub_type(
        coordinator: EnphaseCoordinator, profile: str, sub_type: str | None
    ) -> str | None:
        if profile not in {"cost_savings", "ai_optimisation"}:
            return None
        func = getattr(coordinator, "normalize_battery_sub_type", None)
        if callable(func):
            return func(sub_type)
        func = getattr(coordinator, "_normalize_battery_sub_type", None)
        if callable(func):
            return func(sub_type)
        return None

    def clear_battery_pending(self, *, clear_optimistic: bool = True) -> None:
        state = self.battery_state
        state._battery_pending_profile = None
        state._battery_pending_reserve = None
        state._battery_pending_sub_type = None
        state._battery_pending_requested_at = None
        state._battery_pending_requested_mono = None
        state._battery_pending_require_exact_settings = True
        state._battery_pending_authoritative_confirmation_required = False
        state._battery_backend_profile_update_pending = None
        state._battery_backend_not_pending_observed_at = None
        if clear_optimistic:
            self.clear_battery_optimistic_profile()
        self._sync_battery_profile_pending_issue()

    def clear_battery_optimistic_profile(self) -> None:
        state = self.battery_state
        state._battery_optimistic_profile = None
        state._battery_optimistic_reserve = None
        state._battery_optimistic_sub_type = None
        state._battery_optimistic_until_mono = None

    def set_battery_optimistic_profile(
        self, profile: str, reserve: int | None, sub_type: str | None
    ) -> None:
        state = self.battery_state
        normalized_profile = self.normalize_battery_profile_key(profile)
        if not normalized_profile:
            self.clear_battery_optimistic_profile()
            return
        state._battery_optimistic_profile = normalized_profile
        state._battery_optimistic_reserve = reserve
        state._battery_optimistic_sub_type = self._normalize_pending_sub_type(
            self.coordinator,
            normalized_profile,
            sub_type,
        )
        state._battery_optimistic_until_mono = (
            time.monotonic() + BATTERY_PROFILE_PENDING_TIMEOUT_S
        )

    def optimistic_battery_profile(self) -> str | None:
        state = self.battery_state
        profile = getattr(state, "_battery_optimistic_profile", None)
        until = getattr(state, "_battery_optimistic_until_mono", None)
        if profile is None or until is None:
            return None
        try:
            expired = time.monotonic() >= float(until)
        except Exception:  # noqa: BLE001
            expired = True
        if expired:
            self.clear_battery_optimistic_profile()
            return None
        return profile

    def optimistic_battery_reserve(self) -> int | None:
        if self.optimistic_battery_profile() is None:
            return None
        return self._coerce_optional_int(
            getattr(self.battery_state, "_battery_optimistic_reserve", None)
        )

    def optimistic_battery_sub_type(self) -> str | None:
        if self.optimistic_battery_profile() is None:
            return None
        return self._normalize_battery_sub_type(
            getattr(self.battery_state, "_battery_optimistic_sub_type", None)
        )

    def _read_profile_is_stale_after_write(self, profile: str | None) -> bool:
        optimistic_profile = self.optimistic_battery_profile()
        return (
            optimistic_profile is not None
            and profile is not None
            and profile != optimistic_profile
        )

    def _note_authoritative_profile_read(self, profile: str | None) -> None:
        state = self.battery_state
        if profile is not None:
            state._battery_profile_authoritative_seen = True
            state._battery_profile_authoritative_seen_mono = time.monotonic()
        optimistic_profile = self.optimistic_battery_profile()
        if (
            optimistic_profile is not None
            and profile == optimistic_profile
            and getattr(state, "_battery_pending_profile", None) is None
        ):
            self.clear_battery_optimistic_profile()

    def _profile_authoritative_read_is_fresh(self) -> bool:
        state = self.battery_state
        last_seen = getattr(state, "_battery_profile_authoritative_seen_mono", None)
        if last_seen is None:
            return False
        try:
            age = time.monotonic() - float(last_seen)
        except Exception:  # noqa: BLE001
            return False
        return age <= self._battery_profile_refresh_cache_ttl_seconds(
            BATTERY_SETTINGS_CACHE_TTL
        )

    def _settings_profile_is_stale_after_authoritative_read(
        self, profile: str | None
    ) -> bool:
        if profile is None:
            return False
        state = self.battery_state
        return (
            bool(getattr(state, "_battery_profile_authoritative_seen", False))
            and self._profile_authoritative_read_is_fresh()
            and getattr(state, "_battery_profile", None) is not None
            and profile != getattr(state, "_battery_profile", None)
        )

    def _pending_requires_authoritative_confirmation(self) -> bool:
        return bool(
            getattr(
                self.battery_state,
                "_battery_pending_authoritative_confirmation_required",
                False,
            )
        )

    def _pending_authoritative_confirmation_received(self) -> bool:
        if not self._pending_requires_authoritative_confirmation():
            return True
        state = self.battery_state
        pending_requested_mono = getattr(state, "_battery_pending_requested_mono", None)
        authoritative_seen_mono = getattr(
            state, "_battery_profile_authoritative_seen_mono", None
        )
        if pending_requested_mono is None or authoritative_seen_mono is None:
            return False
        try:
            return float(authoritative_seen_mono) >= float(pending_requested_mono)
        except Exception:  # noqa: BLE001
            return False

    def confirm_battery_pending(self) -> None:
        state = self.battery_state
        pending_profile = getattr(state, "_battery_pending_profile", None)
        if pending_profile and (
            getattr(state, "_battery_profile", None) == pending_profile
            or getattr(state, "_battery_live_profile", None) == pending_profile
        ):
            pending_reserve = getattr(state, "_battery_pending_reserve", None)
            pending_sub_type = self._normalize_battery_sub_type(
                getattr(state, "_battery_pending_sub_type", None)
            )
            state._battery_profile = pending_profile
            if pending_reserve is not None:
                normalized_reserve = self.normalize_battery_reserve_for_profile(
                    pending_profile,
                    pending_reserve,
                )
                state._battery_backup_percentage = normalized_reserve
                self.remember_battery_reserve(pending_profile, normalized_reserve)
            if pending_profile in {"cost_savings", "ai_optimisation"}:
                state._battery_operation_mode_sub_type = pending_sub_type
            else:
                state._battery_operation_mode_sub_type = None
        self.clear_battery_pending()

    def clear_battery_settings_write_pending(self) -> None:
        self.battery_state._battery_settings_last_write_mono = None

    @staticmethod
    def _schedule_toggle_target_attr(schedule_type: str) -> str:
        return f"_battery_{str(schedule_type).lower()}_toggle_target_enabled"

    def _set_schedule_family_toggle_target(
        self, schedule_type: str, enabled: bool | None
    ) -> None:
        attr_name = self._schedule_toggle_target_attr(schedule_type)
        state = self.battery_state
        setattr(state, attr_name, enabled)
        if state is not self.coordinator:
            setattr(self.coordinator, attr_name, enabled)

    def _clear_schedule_family_toggle_target(self, schedule_type: str) -> None:
        self._set_schedule_family_toggle_target(schedule_type, None)

    def set_battery_pending(
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None,
        require_exact_settings: bool = True,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        state._battery_pending_profile = profile
        state._battery_pending_reserve = reserve
        state._battery_pending_sub_type = self._normalize_pending_sub_type(
            coord, profile, sub_type
        )
        state._battery_pending_requested_at = dt_util.utcnow()
        state._battery_pending_requested_mono = time.monotonic()
        state._battery_pending_require_exact_settings = bool(require_exact_settings)
        state._battery_pending_authoritative_confirmation_required = True
        state._battery_backend_profile_update_pending = None
        state._battery_backend_not_pending_observed_at = None
        self.set_battery_optimistic_profile(profile, reserve, sub_type)
        self._sync_battery_profile_pending_issue()

    def _backend_not_pending_clear_grace_seconds(self) -> int:
        polling_interval = self._coerce_optional_int(
            getattr(self.battery_state, "_battery_polling_interval_s", None)
        )
        if polling_interval is None or polling_interval <= 0:
            return FAST_TOGGLE_POLL_HOLD_S
        return max(int(polling_interval), FAST_TOGGLE_POLL_HOLD_S)

    def _battery_profile_refresh_cache_ttl_seconds(self, default_ttl: float) -> float:
        current_interval = None
        update_interval = getattr(self.coordinator, "update_interval", None)
        total_seconds = getattr(update_interval, "total_seconds", None)
        if callable(total_seconds):
            try:
                current_interval = float(total_seconds())
            except Exception:
                current_interval = None
        if current_interval is None or current_interval <= 0:
            slow_interval = self._coerce_optional_int(
                getattr(self.coordinator, "_configured_slow_poll_interval", None)
            )
            if slow_interval is not None and slow_interval > 0:
                current_interval = float(slow_interval)
        polling_interval = self._coerce_optional_int(
            getattr(self.battery_state, "_battery_polling_interval_s", None)
        )
        if polling_interval is not None and polling_interval > 0:
            if current_interval is None or current_interval <= 0:
                current_interval = float(polling_interval)
            else:
                current_interval = max(current_interval, float(polling_interval))
        if current_interval is None or current_interval <= 0:
            return float(default_ttl)
        return min(float(default_ttl), current_interval)

    def battery_status_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._battery_status_cache_until:
            if now < state._battery_status_cache_until:
                return False
        fetcher = getattr(coord.client, "battery_status", None)
        if not callable(fetcher):
            return False
        return coord._endpoint_family_should_run("battery_status", force=force)

    def battery_backup_history_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._battery_backup_history_cache_until:
            if now < state._battery_backup_history_cache_until:
                return False
        fetcher = getattr(coord.client, "battery_backup_history", None)
        if not callable(fetcher):
            return False
        return coord._endpoint_family_should_run("battery_backup_history", force=force)

    def battery_settings_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        state = self.battery_state
        pending_profile = getattr(state, "_battery_pending_profile", None)
        now = time.monotonic()
        if not force and not pending_profile and state._battery_settings_cache_until:
            if now < state._battery_settings_cache_until:
                return False
        fetcher = getattr(coord.client, "battery_settings_details", None)
        if not callable(fetcher):
            return False
        return coord._endpoint_family_should_run(
            "battery_settings",
            force=force or bool(pending_profile),
        )

    def battery_schedules_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        fetcher = getattr(coord.client, "battery_schedules", None)
        if not callable(fetcher):
            return False
        return coord._endpoint_family_should_run("battery_schedules", force=force)

    def battery_site_settings_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._battery_site_settings_cache_until:
            if now < state._battery_site_settings_cache_until:
                return False
        fetcher = getattr(coord.client, "battery_site_settings", None)
        if not callable(fetcher):
            return False
        return coord._endpoint_family_should_run("battery_site_settings", force=force)

    def grid_control_check_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._grid_control_check_cache_until:
            if now < state._grid_control_check_cache_until:
                return False
        state_present = any(
            getattr(state, attr, None) is not None
            for attr in (
                "_grid_control_supported",
                "_grid_control_disable",
                "_grid_control_active_download",
                "_grid_control_sunlight_backup_system_check",
                "_grid_control_grid_outage_check",
                "_grid_control_user_initiated_toggle",
            )
        )
        fetcher = getattr(coord.client, "grid_control_check", None)
        if not callable(fetcher):
            return state_present
        family = "grid_control_check"
        if coord._endpoint_family_should_run(family, force=force):
            return True
        return (
            state_present
            and coord._endpoint_family_state(family).cooldown_active
            and not coord._endpoint_family_can_use_stale(family)
        )

    def dry_contact_settings_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._dry_contact_settings_cache_until:
            if now < state._dry_contact_settings_cache_until:
                return False
        state_present = (
            getattr(state, "_dry_contact_settings_supported", None) is not None
        )
        fetcher = getattr(coord.client, "dry_contacts_settings", None)
        if not callable(fetcher):
            return state_present
        family = "dry_contact_settings"
        if coord._endpoint_family_should_run(family, force=force):
            return True
        return (
            state_present
            and coord._endpoint_family_state(family).cooldown_active
            and not coord._endpoint_family_can_use_stale(family)
        )

    def storm_guard_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        state = self.battery_state
        pending_profile = getattr(state, "_battery_pending_profile", None)
        now = time.monotonic()
        if not force and not pending_profile and state._storm_guard_cache_until:
            if now < state._storm_guard_cache_until:
                return False
        fetcher = getattr(coord.client, "storm_guard_profile", None)
        if not callable(fetcher):
            return False
        return coord._endpoint_family_should_run(
            "storm_guard",
            force=force or bool(pending_profile),
        )

    def storm_alert_refresh_due(self, *, force: bool = False) -> bool:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        if not force and state._storm_alert_cache_until:
            if now < state._storm_alert_cache_until:
                return False
        fetcher = getattr(coord.client, "storm_guard_alert", None)
        if not callable(fetcher):
            return False
        return coord._endpoint_family_should_run("storm_alert", force=force)

    def ac_battery_devices_refresh_due(self, *, force: bool = False) -> bool:
        return self._ac_battery_runtime.ac_battery_devices_refresh_due(force=force)

    def _battery_control_state_settling(self) -> bool:
        coord = self.coordinator
        state = self.battery_state
        if coord.battery_settings_write_pending:
            return True
        if coord.battery_cfg_schedule_pending:
            return True
        if coord.battery_dtg_schedule_pending:
            return True
        if coord.battery_rbd_schedule_pending:
            return True
        return getattr(state, "_battery_cfg_pending_expires_mono", None) is not None

    def _battery_control_refresh_success_ttl_seconds(self, default_ttl: float) -> float:
        if self._battery_control_state_settling():
            return 0.0
        return self._battery_profile_refresh_cache_ttl_seconds(default_ttl)

    def sync_backend_battery_profile_pending(self, value: object) -> None:
        state = self.battery_state
        backend_pending = self._coerce_optional_bool(value)
        if backend_pending is not None:
            state._battery_backend_profile_update_pending = backend_pending
        if backend_pending is None:
            return
        if backend_pending:
            state._battery_backend_not_pending_observed_at = None
            return
        if getattr(state, "_battery_pending_profile", None) is None:
            state._battery_backend_not_pending_observed_at = None
            return
        if (
            self.effective_profile_matches_pending()
            and self._pending_authoritative_confirmation_received()
        ):
            self.confirm_battery_pending()
            return
        now = dt_util.utcnow()
        first_observed = getattr(
            state, "_battery_backend_not_pending_observed_at", None
        )
        if first_observed is None:
            state._battery_backend_not_pending_observed_at = now
            return
        pending_age = self.coordinator.battery_pending_age_seconds
        if pending_age is None:
            return
        if pending_age >= self._backend_not_pending_clear_grace_seconds():
            # A locally accepted write still needs a matching profile read before
            # it is treated as successful; otherwise the pending timeout can
            # surface the unconfirmed change.
            if self._pending_requires_authoritative_confirmation():
                return
            self.clear_battery_pending()

    def effective_profile_matches_pending(self) -> bool:
        state = self.battery_state
        pending_profile = getattr(state, "_battery_pending_profile", None)
        if not pending_profile:
            return False
        effective_profile = getattr(state, "_battery_profile", None)
        if effective_profile is None:
            effective_profile = getattr(state, "_battery_live_profile", None)
        if effective_profile != pending_profile:
            return False
        if not getattr(state, "_battery_pending_require_exact_settings", True):
            return True
        pending_reserve = getattr(state, "_battery_pending_reserve", None)
        if (
            pending_reserve is not None
            and getattr(state, "_battery_backup_percentage", None) != pending_reserve
        ):
            return False
        if pending_profile not in {"cost_savings", "ai_optimisation"}:
            return True
        pending_subtype = self._normalize_battery_sub_type(
            getattr(state, "_battery_pending_sub_type", None)
        )
        effective_subtype = self._normalize_battery_sub_type(
            getattr(state, "_battery_operation_mode_sub_type", None)
        )
        if pending_subtype == SAVINGS_OPERATION_MODE_SUBTYPE:
            return effective_subtype == SAVINGS_OPERATION_MODE_SUBTYPE
        if pending_subtype is None:
            return effective_subtype != SAVINGS_OPERATION_MODE_SUBTYPE
        return pending_subtype == effective_subtype

    def remember_battery_reserve(
        self, profile: str | None, reserve: int | None
    ) -> None:
        if not profile or reserve is None:
            return
        normalized = self.normalize_battery_profile_key(profile)
        if not normalized or normalized not in BATTERY_PROFILE_DEFAULT_RESERVE:
            return
        self.battery_state._battery_profile_reserve_memory[normalized] = int(reserve)

    def remember_previous_battery_reserves(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        for profile, reserve in value.items():
            normalized = self.normalize_battery_profile_key(profile)
            parsed_reserve = self._coerce_optional_int(reserve)
            if normalized is None or parsed_reserve is None:
                continue
            self.remember_battery_reserve(normalized, parsed_reserve)

    def target_reserve_for_profile(self, profile: str) -> int:
        remembered = self.battery_state._battery_profile_reserve_memory.get(profile)
        if remembered is not None:
            return self.normalize_battery_reserve_for_profile(profile, remembered)
        default = BATTERY_PROFILE_DEFAULT_RESERVE.get(profile, 20)
        return self.normalize_battery_reserve_for_profile(profile, default)

    def current_savings_sub_type(self) -> str | None:
        selected_subtype = self.coordinator.battery_selected_operation_mode_sub_type
        if selected_subtype == SAVINGS_OPERATION_MODE_SUBTYPE:
            return SAVINGS_OPERATION_MODE_SUBTYPE
        return None

    def target_operation_mode_sub_type(self, profile: str) -> str | None:
        normalized_profile = self.normalize_battery_profile_key(profile)
        if normalized_profile not in {"cost_savings", "ai_optimisation"}:
            return None
        current_sub_type = self.current_savings_sub_type()
        if normalized_profile == "ai_optimisation":
            return current_sub_type or SAVINGS_OPERATION_MODE_SUBTYPE
        return current_sub_type

    def normalize_battery_reserve_for_profile(self, profile: str, reserve: int) -> int:
        if profile == "backup_only":
            return 100
        min_reserve = self.battery_reserve_min_bound()
        max_reserve = self.battery_reserve_max_bound()
        bounded = max(min_reserve, min(max_reserve, int(reserve)))
        return bounded

    def battery_min_soc_floor(self) -> int:
        value = self._coerce_int(
            getattr(self.battery_state, "_battery_very_low_soc_min", None),
            default=None,
        )
        if value is None:
            return BATTERY_MIN_SOC_FALLBACK
        return max(0, min(100, int(value)))

    def battery_reserve_min_bound(self) -> int:
        value = self._coerce_int(
            getattr(self.battery_state, "_battery_backup_percentage_min", None),
            default=None,
        )
        if value is None:
            return self.battery_min_soc_floor()
        return max(0, min(100, int(value)))

    def battery_reserve_max_bound(self) -> int:
        value = self._coerce_int(
            getattr(self.battery_state, "_battery_backup_percentage_max", None),
            default=None,
        )
        if value is None:
            return 100
        return max(0, min(100, int(value)))

    def _parse_battery_control_capability(
        self, raw_control: object
    ) -> BatteryControlCapability | None:
        if not isinstance(raw_control, dict):
            return None
        return BatteryControlCapability(
            show=self._coerce_optional_bool(raw_control.get("show")),
            enabled=self._coerce_optional_bool(raw_control.get("enabled")),
            locked=self._coerce_optional_bool(raw_control.get("locked")),
            show_day_schedule=self._coerce_optional_bool(
                raw_control.get("showDaySchedule")
            ),
            schedule_supported=self._coerce_optional_bool(
                raw_control.get("scheduleSupported")
            ),
            force_schedule_supported=self._coerce_optional_bool(
                raw_control.get("forceScheduleSupported")
            ),
            force_schedule_opted=self._coerce_optional_bool(
                raw_control.get("forceScheduleOpted")
            ),
        )

    def _apply_battery_control_state(
        self, attr_name: str, raw_control: object
    ) -> BatteryControlCapability | None:
        state = self.battery_state
        control = self._parse_battery_control_capability(raw_control)
        setattr(state, attr_name, control)
        if attr_name == "_battery_cfg_control":
            if control is None:
                state._battery_cfg_control_show = None
                state._battery_cfg_control_enabled = None
                state._battery_cfg_control_schedule_supported = None
                state._battery_cfg_control_force_schedule_supported = None
            else:
                state._battery_cfg_control_show = control.show
                state._battery_cfg_control_enabled = control.enabled
                state._battery_cfg_control_schedule_supported = (
                    control.schedule_supported
                )
                state._battery_cfg_control_force_schedule_supported = (
                    control.force_schedule_supported
                )
        return control

    def _apply_battery_capability_blocks(
        self,
        data: dict[str, object],
        *,
        clear_missing_controls: bool = False,
    ) -> None:
        state = self.battery_state
        for schedule_type in ("dtg", "rbd"):
            raw_control = data.get(f"{schedule_type}Control")
            if isinstance(raw_control, dict):
                start = self._normalize_minutes_of_day(raw_control.get("startTime"))
                end = self._normalize_minutes_of_day(raw_control.get("endTime"))
                setattr(
                    state,
                    self._battery_schedule_control_start_attr(schedule_type),
                    start,
                )
                setattr(
                    state,
                    self._battery_schedule_control_end_attr(schedule_type),
                    end,
                )
            elif clear_missing_controls:
                setattr(
                    state,
                    self._battery_schedule_control_start_attr(schedule_type),
                    None,
                )
                setattr(
                    state,
                    self._battery_schedule_control_end_attr(schedule_type),
                    None,
                )
        if "dtgControl" in data or clear_missing_controls:
            self._apply_battery_control_state(
                "_battery_dtg_control", data.get("dtgControl")
            )
        if "cfgControl" in data or clear_missing_controls:
            self._apply_battery_control_state(
                "_battery_cfg_control", data.get("cfgControl")
            )
        if "rbdControl" in data or clear_missing_controls:
            self._apply_battery_control_state(
                "_battery_rbd_control", data.get("rbdControl")
            )
        if "systemTask" in data:
            state._battery_system_task = self._coerce_optional_bool(
                data.get("systemTask")
            )

    def _apply_battery_user_details(self, user_details: object) -> None:
        if not isinstance(user_details, dict):
            return
        state = self.battery_state
        owner = self._coerce_optional_bool(user_details.get("isOwner"))
        installer = self._coerce_optional_bool(user_details.get("isInstaller"))
        if owner is not None:
            state._battery_user_is_owner = owner
        if installer is not None:
            state._battery_user_is_installer = installer

    def _apply_battery_permission_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        self._apply_battery_capability_blocks(data)
        self._apply_battery_user_details(data.get("userDetails"))

    def _assert_battery_system_not_busy(
        self,
        unavailable_message: str = "Battery updates are unavailable.",
        *,
        unavailable_key: str = "battery_updates_unavailable",
    ) -> None:
        if self.coordinator.battery_system_task is True:
            self._raise_validation(
                unavailable_key,
                message=unavailable_message,
            )

    def _assert_battery_profile_feature_writable(
        self,
        unavailable_message: str,
        *,
        unavailable_key: str = "battery_profile_unavailable",
    ) -> None:
        coord = self.coordinator
        self._assert_battery_system_not_busy(
            unavailable_message,
            unavailable_key=unavailable_key,
        )
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            self._raise_validation(
                "battery_profile_update_not_permitted",
                message="Battery profile updates are not permitted for this account.",
            )

    def _assert_battery_settings_feature_writable(
        self,
        unavailable_message: str,
        *,
        unavailable_key: str = "battery_settings_unavailable",
    ) -> None:
        coord = self.coordinator
        self._assert_battery_system_not_busy(
            unavailable_message,
            unavailable_key=unavailable_key,
        )
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            self._raise_validation(
                "battery_settings_update_not_permitted",
                message="Battery settings updates are not permitted for this account.",
            )

    def assert_battery_profile_write_allowed(self) -> None:
        coord = self.coordinator
        state = self.battery_state
        lock = getattr(state, "_battery_profile_write_lock", None)
        if lock is not None and lock.locked():
            self._raise_validation(
                "battery_profile_update_in_progress",
                message="Another battery profile update is already in progress.",
            )
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            self._raise_validation(
                "battery_profile_update_not_permitted",
                message="Battery profile updates are not permitted for this account.",
            )
        self._assert_battery_system_not_busy(
            "Battery profile updates are unavailable.",
            unavailable_key="battery_profile_updates_unavailable",
        )

        now = time.monotonic()
        last = getattr(state, "_battery_profile_last_write_mono", None)
        if (
            last is not None
            and now >= last
            and (now - last) < BATTERY_PROFILE_WRITE_DEBOUNCE_S
        ):
            self._raise_validation(
                "battery_profile_update_debounced",
                message=(
                    "Battery profile update requested too quickly. Please wait and "
                    "try again."
                ),
            )

    async def async_ensure_battery_write_access_confirmed(
        self,
        *,
        denied_message: str = "Battery updates are not permitted for this account.",
        denied_key: str = "battery_updates_not_permitted",
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        if coord.battery_write_access_confirmed:
            return
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            self._raise_validation(denied_key, message=denied_message)
        fetcher = getattr(coord.client, "battery_site_settings", None)
        if callable(fetcher):
            try:
                payload = await fetcher()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Battery write-access refresh failed: %s",
                    redact_text(err, site_ids=(coord.site_id,)),
                )
            else:
                # The raw permission payload can contain site identifiers and is
                # retained for diagnostics only after coordinator redaction.
                redacted_payload = coord.redact_battery_payload(payload)
                if isinstance(redacted_payload, dict):
                    state._battery_site_settings_payload = redacted_payload
                else:
                    state._battery_site_settings_payload = {"value": redacted_payload}
                self._apply_battery_permission_payload(payload)
        if coord.battery_write_access_confirmed:
            return
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            self._raise_validation(denied_key, message=denied_message)
        self._raise_validation(
            "battery_write_access_unconfirmed",
            message="Battery write access could not be confirmed. Refresh and try again.",
        )

    async def async_assert_battery_profile_write_allowed(self) -> None:
        self.assert_battery_profile_write_allowed()
        await self.async_ensure_battery_write_access_confirmed()
        self.assert_battery_profile_write_allowed()

    def assert_battery_settings_write_allowed(self) -> None:
        coord = self.coordinator
        state = self.battery_state
        lock = getattr(state, "_battery_settings_write_lock", None)
        if lock is not None and lock.locked():
            self._raise_validation(
                "battery_settings_update_in_progress",
                message="Another battery settings update is already in progress.",
            )
        owner = coord.battery_user_is_owner
        installer = coord.battery_user_is_installer
        if owner is False and installer is False:
            self._raise_validation(
                "battery_settings_update_not_permitted",
                message="Battery settings updates are not permitted for this account.",
            )
        self._assert_battery_system_not_busy(
            "Battery settings updates are unavailable.",
            unavailable_key="battery_settings_updates_unavailable",
        )
        now = time.monotonic()
        last = getattr(state, "_battery_settings_last_write_mono", None)
        if (
            last is not None
            and now >= last
            and (now - last) < BATTERY_SETTINGS_WRITE_DEBOUNCE_S
        ):
            self._raise_validation(
                "battery_settings_update_debounced",
                message=(
                    "Battery settings update requested too quickly. Please wait and "
                    "try again."
                ),
            )

    async def async_assert_battery_settings_write_allowed(self) -> None:
        self.assert_battery_settings_write_allowed()
        await self.async_ensure_battery_write_access_confirmed()
        self.assert_battery_settings_write_allowed()

    def current_charge_from_grid_schedule_window(self) -> tuple[int, int]:
        state = self.battery_state
        normalize = getattr(self.coordinator, "normalize_minutes_of_day", None)
        if not callable(normalize):
            normalize = getattr(self.coordinator, "_normalize_minutes_of_day", None)
        if not callable(normalize):

            def normalize(value: object) -> int | None:
                if value is None:
                    return None
                try:
                    minutes = int(str(value).strip())
                except Exception:
                    return None
                if minutes < 0 or minutes >= 24 * 60:
                    return None
                return minutes

        begin = normalize(getattr(state, "_battery_charge_begin_time", None))
        end = normalize(getattr(state, "_battery_charge_end_time", None))
        if begin is None:
            begin = 120
        if end is None:
            end = 300
        return begin, end

    def clear_cfg_settings_pending(self) -> None:
        state = self.battery_state
        state._battery_cfg_pending_charge_from_grid = None
        state._battery_cfg_pending_schedule_enabled = None
        state._battery_cfg_pending_begin_time = None
        state._battery_cfg_pending_end_time = None
        state._battery_cfg_pending_expires_mono = None

    def set_cfg_settings_pending_from_payload(self, payload: dict[str, object]) -> None:
        if not isinstance(payload, dict):
            return
        state = self.battery_state
        pending = False
        if "chargeFromGrid" in payload:
            state._battery_cfg_pending_charge_from_grid = self._coerce_optional_bool(
                payload.get("chargeFromGrid")
            )
            pending = True
        if "chargeFromGridScheduleEnabled" in payload:
            state._battery_cfg_pending_schedule_enabled = self._coerce_optional_bool(
                payload.get("chargeFromGridScheduleEnabled")
            )
            pending = True
        if "chargeBeginTime" in payload:
            state._battery_cfg_pending_begin_time = self._normalize_minutes_of_day(
                payload.get("chargeBeginTime")
            )
            pending = True
        if "chargeEndTime" in payload:
            state._battery_cfg_pending_end_time = self._normalize_minutes_of_day(
                payload.get("chargeEndTime")
            )
            pending = True
        if pending:
            state._battery_cfg_pending_expires_mono = (
                time.monotonic() + FAST_TOGGLE_POLL_HOLD_S
            )
        else:
            self.clear_cfg_settings_pending()

    def sync_cfg_settings_pending(self) -> None:
        state = self.battery_state
        expires_at = getattr(state, "_battery_cfg_pending_expires_mono", None)
        if expires_at is None:
            return
        if time.monotonic() >= float(expires_at):
            self.clear_cfg_settings_pending()
            return

        pending_pairs = (
            ("_battery_cfg_pending_charge_from_grid", "_battery_charge_from_grid"),
            (
                "_battery_cfg_pending_schedule_enabled",
                "_battery_charge_from_grid_schedule_enabled",
            ),
            ("_battery_cfg_pending_begin_time", "_battery_charge_begin_time"),
            ("_battery_cfg_pending_end_time", "_battery_charge_end_time"),
        )
        all_match = True
        for pending_attr, state_attr in pending_pairs:
            pending_value = getattr(state, pending_attr, None)
            if pending_value is None:
                continue
            if getattr(state, state_attr, None) != pending_value:
                setattr(state, state_attr, pending_value)
                all_match = False
        if all_match:
            self.clear_cfg_settings_pending()

    @staticmethod
    def _battery_schedule_label(schedule_type: str) -> str:
        labels = {
            "cfg": "Charge from grid",
            "dtg": "Discharge to grid",
            "rbd": "Restrict battery discharge",
        }
        return labels.get(str(schedule_type).lower(), "Battery schedule")

    @staticmethod
    def _battery_schedule_start_attr(schedule_type: str) -> str:
        if str(schedule_type).lower() == "cfg":
            return "_battery_charge_begin_time"
        return f"_battery_{str(schedule_type).lower()}_begin_time"

    @staticmethod
    def _battery_schedule_end_attr(schedule_type: str) -> str:
        if str(schedule_type).lower() == "cfg":
            return "_battery_charge_end_time"
        return f"_battery_{str(schedule_type).lower()}_end_time"

    @staticmethod
    def _battery_schedule_control_start_attr(schedule_type: str) -> str:
        return f"_battery_{str(schedule_type).lower()}_control_begin_time"

    @staticmethod
    def _battery_schedule_control_end_attr(schedule_type: str) -> str:
        return f"_battery_{str(schedule_type).lower()}_control_end_time"

    @staticmethod
    def _battery_schedule_id_attr(schedule_type: str) -> str:
        return f"_battery_{str(schedule_type).lower()}_schedule_id"

    @staticmethod
    def _battery_schedule_days_attr(schedule_type: str) -> str:
        return f"_battery_{str(schedule_type).lower()}_schedule_days"

    @staticmethod
    def _battery_schedule_timezone_attr(schedule_type: str) -> str:
        return f"_battery_{str(schedule_type).lower()}_schedule_timezone"

    @staticmethod
    def _battery_schedule_limit_attr(schedule_type: str) -> str:
        return f"_battery_{str(schedule_type).lower()}_schedule_limit"

    @staticmethod
    def _battery_schedule_status_attr(schedule_type: str) -> str:
        return f"_battery_{str(schedule_type).lower()}_schedule_status"

    @staticmethod
    def _battery_schedule_enabled_attr(schedule_type: str) -> str:
        return f"_battery_{str(schedule_type).lower()}_schedule_enabled"

    def _normalize_schedule_minutes(self, value: object) -> int | None:
        normalize = getattr(self.coordinator, "normalize_minutes_of_day", None)
        if not callable(normalize):
            normalize = getattr(self.coordinator, "_normalize_minutes_of_day", None)
        if callable(normalize):
            normalized = normalize(value)
            if normalized is not None:
                return normalized
        if isinstance(value, str):
            text = value.strip()
            if ":" in text:
                hour_text, minute_text, *_rest = text.split(":")
                try:
                    hours = int(hour_text)
                    minutes = int(minute_text)
                except ValueError:
                    return None
                if 0 <= hours < 24 and 0 <= minutes < 60:
                    return hours * 60 + minutes
                return None
        if value is None:
            return None
        try:
            minutes = int(str(value).strip())
        except Exception:
            return None
        if minutes < 0 or minutes >= 24 * 60:
            return None
        return minutes

    def current_battery_schedule_window(
        self, schedule_type: str
    ) -> tuple[int | None, int | None]:
        state = self.battery_state
        begin = self._normalize_schedule_minutes(
            getattr(state, self._battery_schedule_start_attr(schedule_type), None)
        )
        end = self._normalize_schedule_minutes(
            getattr(state, self._battery_schedule_end_attr(schedule_type), None)
        )
        return begin, end

    def current_battery_schedule_or_control_window(
        self, schedule_type: str
    ) -> tuple[int | None, int | None]:
        begin, end = self.current_battery_schedule_window(schedule_type)
        if begin is not None and end is not None:
            return begin, end
        state = self.battery_state
        control_begin = self._normalize_schedule_minutes(
            getattr(
                state, self._battery_schedule_control_start_attr(schedule_type), None
            )
        )
        control_end = self._normalize_schedule_minutes(
            getattr(state, self._battery_schedule_control_end_attr(schedule_type), None)
        )
        return (
            begin if begin is not None else control_begin,
            end if end is not None else control_end,
        )

    def battery_itc_disclaimer_value(self) -> str:
        current = getattr(self.battery_state, "_battery_accepted_itc_disclaimer", None)
        if current:
            return current
        return dt_util.utcnow().isoformat()

    async def _async_validate_cfg_schedule_commit(self) -> None:
        """Best-effort mirror of the current CFG validation step."""

        validator = getattr(self.coordinator.client, "validate_battery_schedule", None)
        if not callable(validator):
            return
        try:
            result = await validator("cfg")
        except aiohttp.ClientResponseError as err:
            if err.status not in {
                HTTPStatus.NOT_FOUND,
                HTTPStatus.METHOD_NOT_ALLOWED,
            }:
                _LOGGER.debug(
                    "Ignoring CFG schedule validation failure on site %s: %s",
                    redact_site_id(self.coordinator.site_id),
                    redact_text(err, site_ids=(self.coordinator.site_id,)),
                )
            return
        except Exception as err:  # noqa: BLE001 - validation preflight is optional
            _LOGGER.debug(
                "Ignoring CFG schedule validation exception on site %s: %s",
                redact_site_id(self.coordinator.site_id),
                redact_text(err, site_ids=(self.coordinator.site_id,)),
            )
            return
        if not isinstance(result, dict):
            return
        valid = self._coerce_optional_bool(result.get("valid"))
        if valid is None and "isValid" in result:
            valid = self._coerce_optional_bool(result.get("isValid"))
        if valid is not False:
            return
        message = self._coerce_optional_text(result.get("message"))
        if message:
            self._raise_validation(
                "battery_schedule_validation_rejected_detail",
                placeholders={"message": message},
                message=message,
            )
        self._raise_validation(
            "battery_schedule_validation_rejected",
            message="Battery schedule validation was rejected by Enphase.",
        )

    async def _async_accept_charge_from_grid_disclaimer(self) -> None:
        """Acknowledge the ITC disclaimer before enabling charge from grid."""

        accepter = getattr(
            self.coordinator.client, "accept_battery_settings_disclaimer", None
        )
        if not callable(accepter):
            return
        try:
            await accepter("itc")
        except aiohttp.ClientResponseError as err:
            if err.status in {
                HTTPStatus.NOT_FOUND,
                HTTPStatus.METHOD_NOT_ALLOWED,
            }:
                _LOGGER.debug(
                    "Ignoring missing battery disclaimer endpoint on site %s",
                    redact_site_id(self.coordinator.site_id),
                )
                return
            raise

    def _cfg_schedule_commit_payload(
        self,
        *,
        charge_from_grid_enabled: bool | None = None,
        schedule_enabled: bool | None = None,
    ) -> dict[str, object]:
        """Return the lean CFG commit payload used for schedule commits."""

        coord = self.coordinator
        if schedule_enabled is True:
            charge_from_grid_enabled = True
        if charge_from_grid_enabled is None:
            charge_from_grid_enabled = getattr(
                coord, "battery_charge_from_grid_enabled", None
            )
        if charge_from_grid_enabled is None:
            charge_from_grid_enabled = getattr(
                self.battery_state, "_battery_charge_from_grid", None
            )
        enabled = bool(charge_from_grid_enabled)
        payload: dict[str, object] = {"chargeFromGrid": enabled}
        if enabled:
            payload["acceptedItcDisclaimer"] = self.battery_itc_disclaimer_value()
        return payload

    async def _async_commit_cfg_schedule_write(
        self,
        *,
        charge_from_grid_enabled: bool | None = None,
        schedule_enabled: bool | None = None,
    ) -> None:
        """Commit a pending CFG schedule change after validation."""

        coord = self.coordinator
        payload = self._cfg_schedule_commit_payload(
            charge_from_grid_enabled=charge_from_grid_enabled,
            schedule_enabled=schedule_enabled,
        )
        await self._async_validate_cfg_schedule_commit()
        try:
            await coord.client.set_battery_settings(payload, schedule_type="cfg")
        except aiohttp.ClientResponseError as err:
            if err.status != HTTPStatus.FORBIDDEN:
                self.raise_schedule_update_validation_error(err)
                raise
            try:
                await coord.client.set_battery_settings_compat(
                    payload,
                    schedule_type="cfg",
                    include_source=False,
                    merged_payload=True,
                    strip_devices=True,
                )
            except aiohttp.ClientResponseError as compat_err:
                self.raise_schedule_update_validation_error(compat_err)
                raise

    async def async_commit_cfg_schedule_write(
        self,
        *,
        charge_from_grid_enabled: bool | None = None,
        schedule_enabled: bool | None = None,
    ) -> None:
        """Commit a pending CFG schedule change after validation."""

        await self._async_commit_cfg_schedule_write(
            charge_from_grid_enabled=charge_from_grid_enabled,
            schedule_enabled=schedule_enabled,
        )

    def _schedule_overlap_record(
        self,
        *,
        start_time: object,
        end_time: object,
        days: list[int],
        exclude_schedule_id: str | None = None,
    ):
        return battery_schedule_overlap_record(
            self.coordinator,
            start_time=start_time,
            end_time=end_time,
            days=days,
            exclude_schedule_id=exclude_schedule_id,
        )

    def _raise_schedule_overlap_validation_error(
        self,
        *,
        start_time: object,
        end_time: object,
        days: list[int],
        exclude_schedule_id: str | None = None,
    ) -> None:
        overlapping = self._schedule_overlap_record(
            start_time=start_time,
            end_time=end_time,
            days=days,
            exclude_schedule_id=exclude_schedule_id,
        )
        if overlapping is not None:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="battery_schedule_overlap",
                translation_placeholders=battery_schedule_overlap_placeholders(
                    overlapping,
                    hass=getattr(self.coordinator, "hass", None),
                ),
                message=battery_schedule_overlap_message(
                    overlapping,
                    hass=getattr(self.coordinator, "hass", None),
                ),
            )

    def _raise_validation(
        self,
        key: str,
        *,
        placeholders: dict[str, object] | None = None,
        message: str | None = None,
    ) -> None:
        raise_translated_service_validation(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders=placeholders,
            message=message,
        )

    def _schedule_label_placeholders(self, schedule_type: str) -> dict[str, object]:
        schedule_label = self._battery_schedule_label(schedule_type)
        return {
            "schedule_label": schedule_label,
            "schedule_label_lower": schedule_label.lower(),
        }

    @staticmethod
    def _is_already_processed_profile_cancel_error(
        err: aiohttp.ClientResponseError,
    ) -> bool:
        if err.status != HTTPStatus.CONFLICT:
            return False
        raw_message = getattr(err, "message", None)
        if not isinstance(raw_message, str) or not raw_message.strip():
            return False
        try:
            parsed = json.loads(raw_message)
        except ValueError:
            return "ALREADY_PROCESSED" in raw_message.upper()
        error_block = parsed.get("error")
        if not isinstance(error_block, dict):
            return False
        status_value = error_block.get("status")
        return (
            isinstance(status_value, str)
            and status_value.strip().upper() == "ALREADY_PROCESSED"
        )

    def raise_schedule_update_validation_error(
        self, err: aiohttp.ClientResponseError
    ) -> None:
        if err.status == HTTPStatus.FORBIDDEN:
            self._raise_validation(
                "schedule_update_forbidden",
                message="Schedule update was rejected by Enphase (HTTP 403 Forbidden).",
            )
        if err.status == HTTPStatus.UNAUTHORIZED:
            self._raise_validation(
                "schedule_update_unauthorized",
                message=(
                    "Schedule update could not be authenticated. Reauthenticate and "
                    "try again."
                ),
            )
        if err.status == HTTPStatus.CONFLICT:
            backend_status: str | None = None
            backend_message: str | None = None
            raw_message = getattr(err, "message", None)
            if isinstance(raw_message, str) and raw_message.strip():
                try:
                    parsed = json.loads(raw_message)
                except ValueError:
                    backend_message = raw_message.strip()
                else:
                    error_block = parsed.get("error")
                    if isinstance(error_block, dict):
                        status_value = error_block.get("status")
                        if isinstance(status_value, str) and status_value.strip():
                            backend_status = status_value.strip().upper()
                        message_value = error_block.get("message")
                        if isinstance(message_value, str) and message_value.strip():
                            backend_message = message_value.strip()

            if backend_status == "CONFLICTING_SCHEDULE_DTG":
                self._raise_validation(
                    "schedule_conflict_dtg",
                    message=(
                        "Schedule conflicts with the existing discharge-to-grid "
                        "schedule. Adjust or disable that schedule first."
                    ),
                )
            if backend_status == "CONFLICTING_SCHEDULE_RBD":
                self._raise_validation(
                    "schedule_conflict_rbd",
                    message=(
                        "Schedule conflicts with the existing "
                        "restrict-battery-discharge schedule. Adjust or disable that "
                        "schedule first."
                    ),
                )
            if backend_status == "CONFLICTING_SCHEDULE_CFG":
                self._raise_validation(
                    "schedule_conflict_cfg",
                    message=(
                        "Schedule conflicts with the existing charge-from-grid "
                        "schedule. Adjust or disable that schedule first."
                    ),
                )
            if backend_message:
                if not backend_message.endswith("."):
                    backend_message = f"{backend_message}."
                self._raise_validation(
                    "schedule_update_conflict_detail",
                    placeholders={"message": backend_message},
                    message=(
                        "Schedule update conflicts with an existing battery "
                        f"schedule: {backend_message}"
                    ),
                )
            self._raise_validation(
                "schedule_update_conflict",
                message="Schedule update conflicts with an existing battery schedule.",
            )

    async def async_update_battery_schedule(
        self,
        schedule_id: str,
        *,
        schedule_type: str = "CFG",
        start_time: str,
        end_time: str,
        limit: int | None,
        days: list[int],
        timezone: str,
        is_enabled: bool | None = None,
        is_deleted: bool | None = None,
        apply_settings: bool = True,
    ) -> None:
        normalized_schedule_type = str(schedule_type).lower()
        self._raise_schedule_overlap_validation_error(
            start_time=start_time,
            end_time=end_time,
            days=days,
            exclude_schedule_id=str(schedule_id),
        )
        try:
            await self.coordinator.client.update_battery_schedule(
                schedule_id,
                schedule_type=schedule_type,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                days=days,
                timezone=timezone,
                is_enabled=is_enabled,
                is_deleted=is_deleted,
            )
        except aiohttp.ClientResponseError as err:
            self.raise_schedule_update_validation_error(err)
            raise
        if (
            apply_settings
            and normalized_schedule_type in {"cfg", "dtg", "rbd"}
            and not is_deleted
        ):
            if normalized_schedule_type == "cfg":
                await self.async_commit_cfg_schedule_write(schedule_enabled=is_enabled)
            else:
                await self.async_apply_schedule_family_settings(
                    normalized_schedule_type,
                    start_time=start_time,
                    end_time=end_time,
                    enabled=is_enabled,
                )

    async def async_create_battery_schedule(
        self,
        *,
        schedule_type: str,
        start_time: str,
        end_time: str,
        limit: int | None,
        days: list[int],
        timezone: str,
        is_enabled: bool | None = None,
    ) -> None:
        normalized_schedule_type = str(schedule_type).lower()
        self._raise_schedule_overlap_validation_error(
            start_time=start_time,
            end_time=end_time,
            days=days,
        )
        try:
            await self.coordinator.client.create_battery_schedule(
                schedule_type=schedule_type,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                days=days,
                timezone=timezone,
                is_enabled=is_enabled,
            )
        except aiohttp.ClientResponseError as err:
            self.raise_schedule_update_validation_error(err)
            raise
        if normalized_schedule_type in {"cfg", "dtg", "rbd"}:
            if normalized_schedule_type == "cfg":
                await self.async_commit_cfg_schedule_write(schedule_enabled=is_enabled)
            else:
                await self.async_apply_schedule_family_settings(
                    normalized_schedule_type,
                    start_time=start_time,
                    end_time=end_time,
                    enabled=is_enabled,
                )

    async def async_delete_battery_schedule(
        self,
        schedule_id: str,
        *,
        schedule_type: str = "cfg",
        start_time: object | None = None,
        end_time: object | None = None,
        enabled: bool | None = None,
    ) -> None:
        normalized_schedule_type = str(schedule_type).lower()
        try:
            await self.coordinator.client.delete_battery_schedule(
                schedule_id,
                schedule_type=normalized_schedule_type,
            )
        except aiohttp.ClientResponseError as err:
            self.raise_schedule_update_validation_error(err)
            raise
        if normalized_schedule_type in {"cfg", "dtg", "rbd"}:
            await self.async_apply_schedule_family_settings(
                normalized_schedule_type,
                start_time=start_time,
                end_time=end_time,
                enabled=enabled,
            )

    def _schedule_family_effective_enabled(
        self, schedule_type: str, enabled: bool | None
    ) -> bool:
        if enabled is not None:
            return bool(enabled)

        coord = self.coordinator
        effective = getattr(coord, "_battery_schedule_effective_enabled", None)
        if callable(effective):
            resolved = effective(schedule_type)
            if resolved is not None:
                return bool(resolved)

        state = self.battery_state
        normalized_schedule_type = str(schedule_type).lower()
        if normalized_schedule_type == "cfg":
            resolved = getattr(coord, "battery_charge_from_grid_schedule_enabled", None)
            if resolved is None:
                resolved = getattr(
                    state, "_battery_charge_from_grid_schedule_enabled", None
                )
            if resolved is None:
                resolved = getattr(
                    coord, "battery_cfg_control_force_schedule_opted", None
                )
        else:
            resolved = getattr(
                state,
                self._battery_schedule_enabled_attr(normalized_schedule_type),
                None,
            )
            if resolved is None:
                control = getattr(
                    coord, f"battery_{normalized_schedule_type}_control", None
                )
                if isinstance(control, dict):
                    resolved = self._coerce_optional_bool(control.get("enabled"))
        return bool(resolved) if resolved is not None else False

    def _schedule_family_settings_payload(
        self,
        schedule_type: str,
        *,
        start_minutes: int | None,
        end_minutes: int | None,
        enabled: bool,
    ) -> dict[str, object] | None:
        normalized_schedule_type = str(schedule_type).lower()
        coord = self.coordinator
        if normalized_schedule_type == "cfg":
            include_window = start_minutes is not None and end_minutes is not None
            if enabled and not include_window:
                return None
            if include_window and start_minutes == end_minutes:
                self._raise_validation(
                    "charge_from_grid_schedule_times_different",
                    message=(
                        "Charge-from-grid schedule start and end times must be "
                        "different."
                    ),
                )
            charge_from_grid_enabled = getattr(
                coord, "battery_charge_from_grid_enabled", None
            )
            if charge_from_grid_enabled is None:
                charge_from_grid_enabled = getattr(
                    self.battery_state, "_battery_charge_from_grid", None
                )
            payload: dict[str, object] = {
                "chargeFromGrid": (True if enabled else bool(charge_from_grid_enabled)),
                "chargeFromGridScheduleEnabled": bool(enabled),
                "acceptedItcDisclaimer": self.battery_itc_disclaimer_value(),
            }
            if include_window:
                payload["chargeBeginTime"] = start_minutes
                payload["chargeEndTime"] = end_minutes
            cfg_control = coord.battery_cfg_control
            if isinstance(cfg_control, dict):
                cfg_payload: dict[str, object] = {}
                field_map = {
                    "show": "show",
                    "enabled": "enabled",
                    "locked": "locked",
                    "show_day_schedule": "showDaySchedule",
                    "schedule_supported": "scheduleSupported",
                    "force_schedule_supported": "forceScheduleSupported",
                }
                for source_key, target_key in field_map.items():
                    value = cfg_control.get(source_key)
                    if value is not None:
                        cfg_payload[target_key] = value
                cfg_payload["forceScheduleOpted"] = bool(enabled)
                payload["cfgControl"] = cfg_payload
            return payload

        if normalized_schedule_type not in {"dtg", "rbd"}:
            return None
        return {
            f"{normalized_schedule_type}Control": self._schedule_family_control_payload(
                normalized_schedule_type,
                enabled=enabled,
                current_start=start_minutes,
                current_end=end_minutes,
            )
        }

    async def async_apply_schedule_family_settings(
        self,
        schedule_type: str,
        *,
        start_time: object | None = None,
        end_time: object | None = None,
        enabled: bool | None = None,
    ) -> None:
        normalized_schedule_type = str(schedule_type).lower()
        if normalized_schedule_type not in {"cfg", "dtg", "rbd"}:
            return

        coord = self.coordinator
        start_minutes = self._normalize_schedule_minutes(start_time)
        end_minutes = self._normalize_schedule_minutes(end_time)
        effective_enabled = self._schedule_family_effective_enabled(
            normalized_schedule_type, enabled
        )
        omit_missing_cfg_disable_window = (
            normalized_schedule_type == "cfg"
            and effective_enabled is False
            and start_time is None
            and end_time is None
        )
        if (
            start_minutes is None or end_minutes is None
        ) and not omit_missing_cfg_disable_window:
            current_start, current_end = self._current_battery_schedule_window_for_type(
                normalized_schedule_type
            )
            if start_minutes is None:
                start_minutes = current_start
            if end_minutes is None:
                end_minutes = current_end

        payload = self._schedule_family_settings_payload(
            normalized_schedule_type,
            start_minutes=start_minutes,
            end_minutes=end_minutes,
            enabled=effective_enabled,
        )
        if payload is None:
            return

        try:
            await coord.client.set_battery_settings(
                payload,
                schedule_type=normalized_schedule_type,
            )
        except aiohttp.ClientResponseError as err:
            if err.status != HTTPStatus.FORBIDDEN:
                self.raise_schedule_update_validation_error(err)
                raise
            await coord.client.set_battery_settings_compat(
                payload,
                schedule_type=normalized_schedule_type,
                include_source=False,
                merged_payload=True,
                strip_devices=True,
            )

    async def async_apply_battery_profile(
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None = None,
        require_exact_pending_match: bool = True,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        normalized_profile = self.normalize_battery_profile_key(profile)
        if not normalized_profile:
            self._raise_validation(
                "battery_profile_unavailable",
                message="Battery profile is unavailable.",
            )
        await self.async_assert_battery_profile_write_allowed()
        normalized_reserve = self.normalize_battery_reserve_for_profile(
            normalized_profile, reserve
        )
        normalized_sub_type = (
            coord.normalize_battery_sub_type(sub_type)
            if normalized_profile in {"cost_savings", "ai_optimisation"}
            else None
        )
        if (
            normalized_profile == "ai_optimisation"
            and normalized_sub_type != SAVINGS_OPERATION_MODE_SUBTYPE
        ):
            normalized_sub_type = SAVINGS_OPERATION_MODE_SUBTYPE
        async with state._battery_profile_write_lock:
            state._battery_profile_last_write_mono = time.monotonic()
            try:
                await coord.client.set_battery_profile(
                    profile=normalized_profile,
                    battery_backup_percentage=normalized_reserve,
                    operation_mode_sub_type=normalized_sub_type,
                    devices=None,
                )
            except aiohttp.ClientResponseError as err:
                if err.status == HTTPStatus.FORBIDDEN:
                    owner = coord.battery_user_is_owner
                    installer = coord.battery_user_is_installer
                    if owner is False and installer is False:
                        self._raise_validation(
                            "battery_profile_update_not_permitted",
                            message=(
                                "Battery profile updates are not permitted for this "
                                "account."
                            ),
                        )
                    self._raise_validation(
                        "battery_profile_update_forbidden",
                        message=(
                            "Battery profile update was rejected by Enphase "
                            "(HTTP 403 Forbidden)."
                        ),
                    )
                if err.status == HTTPStatus.UNAUTHORIZED:
                    self._raise_validation(
                        "battery_profile_update_unauthorized",
                        message=(
                            "Battery profile update could not be authenticated. "
                            "Reauthenticate and try again."
                        ),
                    )
                raise
        self.remember_battery_reserve(normalized_profile, normalized_reserve)
        self.set_battery_pending(
            profile=normalized_profile,
            reserve=normalized_reserve,
            sub_type=normalized_sub_type,
            require_exact_settings=require_exact_pending_match,
        )
        state._storm_guard_cache_until = None
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    async def async_apply_battery_reserve_only(
        self,
        *,
        profile: str,
        reserve: int,
        require_exact_pending_match: bool = True,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        normalized_profile = self.normalize_battery_profile_key(profile)
        if not normalized_profile:
            self._raise_validation(
                "battery_profile_unavailable",
                message="Battery profile is unavailable.",
            )
        normalized_reserve = self.normalize_battery_reserve_for_profile(
            normalized_profile, reserve
        )
        normalized_sub_type = self.target_operation_mode_sub_type(normalized_profile)
        payload = {"batteryBackupPercentage": normalized_reserve}
        await self.async_assert_battery_profile_write_allowed()
        self.assert_battery_settings_write_allowed()
        async with state._battery_profile_write_lock:
            async with state._battery_settings_write_lock:
                state._battery_profile_last_write_mono = time.monotonic()
                state._battery_settings_last_write_mono = (
                    state._battery_profile_last_write_mono
                )
                try:
                    await coord.client.set_battery_settings_compat(
                        payload,
                        merged_payload=True,
                        strip_devices=True,
                    )
                except aiohttp.ClientResponseError as err:
                    if err.status == HTTPStatus.FORBIDDEN:
                        owner = coord.battery_user_is_owner
                        installer = coord.battery_user_is_installer
                        if owner is False and installer is False:
                            self._raise_validation(
                                "battery_profile_update_not_permitted",
                                message=(
                                    "Battery profile updates are not permitted for this "
                                    "account."
                                ),
                            )
                        self._raise_validation(
                            "battery_profile_update_forbidden",
                            message=(
                                "Battery profile update was rejected by Enphase "
                                "(HTTP 403 Forbidden)."
                            ),
                        )
                    if err.status == HTTPStatus.UNAUTHORIZED:
                        self._raise_validation(
                            "battery_profile_update_unauthorized",
                            message=(
                                "Battery profile update could not be authenticated. "
                                "Reauthenticate and try again."
                            ),
                        )
                    raise
        self.parse_battery_settings_payload(
            payload,
            clear_missing_schedule_times=False,
            clear_missing_reserve_bounds=False,
        )
        self.remember_battery_reserve(normalized_profile, normalized_reserve)
        self.set_battery_pending(
            profile=normalized_profile,
            reserve=normalized_reserve,
            sub_type=normalized_sub_type,
            require_exact_settings=require_exact_pending_match,
        )
        state._storm_guard_cache_until = None
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    async def async_apply_battery_settings(self, payload: dict[str, object]) -> None:
        coord = self.coordinator
        state = self.battery_state
        if not isinstance(payload, dict) or not payload:
            self._raise_validation(
                "battery_settings_payload_unavailable",
                message="Battery settings payload is unavailable.",
            )
        await self.async_assert_battery_settings_write_allowed()
        async with state._battery_settings_write_lock:
            state._battery_settings_last_write_mono = time.monotonic()
            try:
                await coord.client.set_battery_settings(payload)
            except aiohttp.ClientResponseError as err:
                if err.status == HTTPStatus.FORBIDDEN:
                    self._raise_validation(
                        "battery_settings_update_forbidden",
                        message=(
                            "Battery settings update was rejected by Enphase "
                            "(HTTP 403 Forbidden)."
                        ),
                    )
                if err.status == HTTPStatus.UNAUTHORIZED:
                    self._raise_validation(
                        "battery_settings_update_unauthorized",
                        message=(
                            "Battery settings update could not be authenticated. "
                            "Reauthenticate and try again."
                        ),
                    )
                raise
        self.parse_battery_settings_payload(
            payload,
            clear_missing_schedule_times=False,
            clear_missing_reserve_bounds=False,
        )
        self.set_cfg_settings_pending_from_payload(payload)
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    async def async_apply_battery_settings_compat(
        self,
        payload: dict[str, object],
        *,
        schedule_type: str = "cfg",
        include_source: bool = True,
        merged_payload: bool = False,
        strip_devices: bool = False,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        if not isinstance(payload, dict) or not payload:
            self._raise_validation(
                "battery_settings_payload_unavailable",
                message="Battery settings payload is unavailable.",
            )
        await self.async_assert_battery_settings_write_allowed()
        async with state._battery_settings_write_lock:
            state._battery_settings_last_write_mono = time.monotonic()
            try:
                await coord.client.set_battery_settings_compat(
                    payload,
                    schedule_type=schedule_type,
                    include_source=include_source,
                    merged_payload=merged_payload,
                    strip_devices=strip_devices,
                )
            except aiohttp.ClientResponseError as err:
                if err.status == HTTPStatus.FORBIDDEN:
                    self._raise_validation(
                        "battery_settings_update_forbidden",
                        message=(
                            "Battery settings update was rejected by Enphase "
                            "(HTTP 403 Forbidden)."
                        ),
                    )
                if err.status == HTTPStatus.UNAUTHORIZED:
                    self._raise_validation(
                        "battery_settings_update_unauthorized",
                        message=(
                            "Battery settings update could not be authenticated. "
                            "Reauthenticate and try again."
                        ),
                    )
                raise
        self.parse_battery_settings_payload(
            payload,
            clear_missing_schedule_times=False,
            clear_missing_reserve_bounds=False,
        )
        self.set_cfg_settings_pending_from_payload(payload)
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    def _profile_recovery_reserve_nudge(self, profile: str, reserve: int) -> int | None:
        normalized_profile = self.normalize_battery_profile_key(profile)
        if normalized_profile == "backup_only":
            return None
        normalized_reserve = self.normalize_battery_reserve_for_profile(
            normalized_profile or profile, reserve
        )
        min_reserve = self.battery_reserve_min_bound()
        max_reserve = self.battery_reserve_max_bound()
        if normalized_reserve < max_reserve:
            return min(max_reserve, normalized_reserve + 5)
        if normalized_reserve > min_reserve:
            return max(min_reserve, normalized_reserve - 5)
        return None

    def _schedule_profile_recovery_reserve_restore(
        self, profile: str, reserve: int
    ) -> None:
        state = self.battery_state
        task = getattr(state, "_battery_profile_recovery_restore_task", None)
        if task is not None and not task.done():
            task.cancel()

        async def _restore() -> None:
            try:
                await asyncio.sleep(_PROFILE_RECOVERY_RESERVE_RESTORE_DELAY_S)
                await self.async_apply_battery_reserve_only(
                    profile=profile,
                    reserve=reserve,
                    require_exact_pending_match=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed to restore battery reserve after profile recovery for site %s: %s",
                    redact_site_id(self.coordinator.site_id),
                    redact_text(str(err)),
                )
            finally:
                current = getattr(
                    self.battery_state, "_battery_profile_recovery_restore_task", None
                )
                if current is asyncio.current_task():
                    self.battery_state._battery_profile_recovery_restore_task = None

        hass = getattr(self.coordinator, "hass", None)
        creator = getattr(hass, "async_create_task", None)
        if callable(creator):
            state._battery_profile_recovery_restore_task = creator(_restore())
        else:
            state._battery_profile_recovery_restore_task = asyncio.create_task(
                _restore()
            )

    async def async_recover_split_battery_profile(
        self, *, profile: str, reserve: int
    ) -> bool:
        state = self.battery_state
        normalized_profile = self.normalize_battery_profile_key(profile)
        if not normalized_profile:
            return False
        live_profile = getattr(state, "_battery_live_profile", None)
        if live_profile is None or live_profile == normalized_profile:
            return False
        if getattr(state, "_battery_profile", None) != normalized_profile:
            return False
        nudge_reserve = self._profile_recovery_reserve_nudge(
            normalized_profile, reserve
        )
        if nudge_reserve is None:
            return False
        _LOGGER.debug(
            "Recovering split battery profile for site %s (configured=%s live=%s target=%s)",
            redact_site_id(self.coordinator.site_id),
            getattr(state, "_battery_profile", None),
            live_profile,
            normalized_profile,
        )
        await self.async_apply_battery_reserve_only(
            profile=normalized_profile,
            reserve=nudge_reserve,
            require_exact_pending_match=False,
        )
        self._schedule_profile_recovery_reserve_restore(
            normalized_profile,
            self.normalize_battery_reserve_for_profile(normalized_profile, reserve),
        )
        return True

    def parse_battery_backup_history_payload(
        self, payload: object
    ) -> list[dict[str, object]] | None:
        coord = self.coordinator
        if not isinstance(payload, dict):
            return None
        histories = payload.get("histories")
        if not isinstance(histories, list):
            return None
        total_records = self._coerce_int(payload.get("total_records"), default=-1)
        total_backup = self._coerce_int(payload.get("total_backup"), default=-1)
        events: list[dict[str, object]] = []
        for item in histories:
            if not isinstance(item, dict):
                continue
            try:
                duration = int(item.get("duration"))
            except (TypeError, ValueError):
                continue
            if duration <= 0:
                continue
            start_raw = item.get("start_time")
            if start_raw is None:
                continue
            try:
                start_text = str(start_raw).strip()
            except Exception:  # noqa: BLE001
                continue
            if not start_text:
                continue
            start = dt_util.parse_datetime(start_text)
            if start is None:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=self.backup_history_tzinfo())
            end = start + timedelta(seconds=duration)
            events.append(
                {
                    "start": start,
                    "end": end,
                    "duration_seconds": duration,
                }
            )
        events.sort(key=lambda item: item["start"])
        if total_records >= 0 and total_records != len(events):
            _LOGGER.debug(
                "Battery backup history total_records mismatch for site %s (payload=%s parsed=%s)",
                redact_site_id(coord.site_id),
                total_records,
                len(events),
            )
        if total_backup >= 0:
            parsed_total_backup = sum(int(item["duration_seconds"]) for item in events)
            if total_backup != parsed_total_backup:
                _LOGGER.debug(
                    "Battery backup history total_backup mismatch for site %s (payload=%s parsed=%s)",
                    redact_site_id(coord.site_id),
                    total_backup,
                    parsed_total_backup,
                )
        return events

    def parse_battery_profile_payload(self, payload: object) -> None:
        state = self.battery_state
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload

        profile = self.normalize_battery_profile_key(data.get("profile"))
        reserve = self._coerce_optional_int(data.get("batteryBackupPercentage"))
        subtype = self._normalize_battery_sub_type(data.get("operationModeSubType"))
        polling_interval = self._coerce_optional_int(data.get("pollingInterval"))
        supports_mqtt = self._coerce_optional_bool(data.get("supportsMqtt"))
        evse_storm_enabled = self._coerce_optional_bool(data.get("evseStormEnabled"))
        storm_state = self.normalize_storm_guard_state(data.get("stormGuardState"))
        self.remember_previous_battery_reserves(
            data.get("previousBatteryBackupPercentage")
        )
        self._apply_battery_capability_blocks(data)
        devices: list[dict[str, object]] = []
        profile_evse_device: dict[str, object] | None = None
        raw_devices = data.get("devices")
        if isinstance(raw_devices, dict):
            iq_evse = raw_devices.get("iqEvse")
            if isinstance(iq_evse, list):
                for item in iq_evse:
                    if not isinstance(item, dict):
                        continue
                    uuid = item.get("uuid")
                    if uuid is None:
                        continue
                    try:
                        uuid_text = str(uuid)
                    except Exception:
                        continue
                    devices.append(
                        {
                            "uuid": uuid_text,
                            "chargeMode": item.get("chargeMode"),
                            "enable": self._coerce_optional_bool(item.get("enable")),
                        }
                    )
                    if profile_evse_device is None:
                        profile_evse_device = {
                            "uuid": uuid_text,
                            "device_name": item.get("deviceName"),
                            "profile": self.normalize_battery_profile_key(
                                item.get("profile")
                            )
                            or item.get("profile"),
                            "profile_config": item.get("profileConfig"),
                            "enable": self._coerce_optional_bool(item.get("enable")),
                            "status": self._coerce_optional_int(item.get("status")),
                            "charge_mode": item.get("chargeMode"),
                            "charge_mode_status": item.get("chargeModeStatus"),
                            "updated_at": self._coerce_optional_int(
                                item.get("updatedAt")
                            ),
                        }

        stale_profile_read = self._read_profile_is_stale_after_write(profile)
        if profile is not None and not stale_profile_read:
            state._battery_profile = profile
            self._note_authoritative_profile_read(profile)
        if reserve is not None and not stale_profile_read:
            normalized_reserve = self.normalize_battery_reserve_for_profile(
                profile or state._battery_profile or "self-consumption",
                reserve,
            )
            state._battery_backup_percentage = normalized_reserve
            self.remember_battery_reserve(
                profile or state._battery_profile, normalized_reserve
            )
        if subtype is not None and not stale_profile_read:
            state._battery_operation_mode_sub_type = subtype
        elif not stale_profile_read and profile not in {
            "cost_savings",
            "ai_optimisation",
        }:
            state._battery_operation_mode_sub_type = None
        if supports_mqtt is not None:
            state._battery_supports_mqtt = supports_mqtt
        if polling_interval is not None and polling_interval > 0:
            state._battery_polling_interval_s = polling_interval
        if storm_state is not None:
            state._storm_guard_state = storm_state
        self.sync_storm_guard_pending(storm_state)
        if evse_storm_enabled is not None:
            state._storm_evse_enabled = evse_storm_enabled
        if devices:
            state._battery_profile_devices = devices
        elif profile is not None:
            state._battery_profile_devices = []
        if profile_evse_device is not None:
            state._battery_profile_evse_device = profile_evse_device
        self.sync_backend_battery_profile_pending(data.get("isBatteryChangePending"))

        if (
            self.effective_profile_matches_pending()
            and self._pending_authoritative_confirmation_received()
        ):
            self.confirm_battery_pending()

    def parse_battery_status_payload(self, payload: object) -> None:
        state = self.battery_state
        if not isinstance(payload, dict):
            state._battery_storage_data = {}
            state._battery_storage_order = []
            state._battery_aggregate_charge_pct = None
            state._battery_aggregate_status = None
            state._battery_aggregate_status_details = {}
            state._battery_summary_sample_utc = None
            state._battery_live_profile = None
            state._battery_live_profile_label = None
            state._battery_live_profile_sample_utc = None
            self._refresh_cached_topology()
            return

        storages = payload.get("storages")
        storage_items = storages if isinstance(storages, list) else []
        snapshots: dict[str, dict[str, object]] = {}
        order: list[str] = []
        status_map: dict[str, str] = {}
        raw_status_map: dict[str, str | None] = {}
        status_text_map: dict[str, str | None] = {}
        total_available_energy = 0.0
        total_capacity = 0.0
        contributing_count = 0
        missing_energy_capacity_keys: list[str] = []
        excluded_count = 0
        worst_key: str | None = None
        worst_status: str | None = None
        worst_severity = self._battery_status_severity_value("normal")
        live_profiles: list[tuple[str, str]] = []

        for item in storage_items:
            if not isinstance(item, dict):
                continue
            raw_payload: dict[str, object] = {}
            for raw_key, raw_value in item.items():
                raw_payload[str(raw_key)] = raw_value

            live_label = self._coerce_optional_text(raw_payload.get("battery_mode"))
            live_profile = self.normalize_live_battery_mode_profile(live_label)
            if live_profile is not None and live_label is not None:
                live_profiles.append((live_profile, live_label))

            excluded = self._coerce_optional_bool(raw_payload.get("excluded")) is True
            if excluded:
                excluded_count += 1

            key = self._battery_storage_key(raw_payload)
            if not key or excluded:
                continue

            charge_pct = self._parse_percent_value(raw_payload.get("current_charge"))
            available_energy = self._coerce_optional_kwh(
                raw_payload.get("available_energy")
            )
            max_capacity = self._coerce_optional_kwh(raw_payload.get("max_capacity"))
            status_raw = self._coerce_optional_text(raw_payload.get("status"))
            status_text = self._coerce_optional_text(raw_payload.get("statusText"))
            normalized_status_raw = self._normalize_battery_status_text(status_raw)
            normalized_status_text = self._normalize_battery_status_text(status_text)
            normalized_status = normalized_status_raw
            if normalized_status in (
                None,
                "unknown",
            ) and normalized_status_text not in (
                None,
                "unknown",
            ):
                normalized_status = normalized_status_text
            if normalized_status is None:
                normalized_status = "unknown"
            severity = self._battery_status_severity_value(normalized_status)

            snapshot = dict(raw_payload)
            normalized_battery_id = self._normalize_battery_id(raw_payload.get("id"))
            if normalized_battery_id is not None:
                snapshot["id"] = normalized_battery_id
                snapshot["battery_id"] = normalized_battery_id
            snapshot["identity"] = key
            snapshot["current_charge_pct"] = charge_pct
            snapshot["available_energy_kwh"] = available_energy
            snapshot["max_capacity_kwh"] = max_capacity
            snapshot["status_normalized"] = normalized_status
            snapshot["status_text"] = status_text
            snapshots[key] = snapshot
            order.append(key)

            status_map[key] = normalized_status
            raw_status_map[key] = status_raw
            status_text_map[key] = status_text

            if (
                max_capacity is not None
                and max_capacity > 0
                and available_energy is not None
            ):
                total_capacity += max_capacity
                total_available_energy += available_energy
                contributing_count += 1
            else:
                missing_energy_capacity_keys.append(key)

            if severity > worst_severity:
                worst_severity = severity
                worst_status = normalized_status
                worst_key = key
            elif worst_status is None:
                worst_status = normalized_status
                worst_key = key

        aggregate_charge = None
        site_current_charge = self._parse_percent_value(payload.get("current_charge"))
        aggregate_charge_source = "unknown"
        included_count = len(snapshots)
        can_compute_weighted = (
            included_count > 0
            and contributing_count == included_count
            and total_capacity > 0
        )
        if can_compute_weighted:
            aggregate_charge = round((total_available_energy / total_capacity) * 100, 1)
            aggregate_charge_source = "computed"
        elif site_current_charge is not None:
            aggregate_charge = site_current_charge
            aggregate_charge_source = "site_current_charge"

        aggregate_status = worst_status or ("normal" if snapshots else "unknown")

        state._battery_storage_data = snapshots
        state._battery_storage_order = list(dict.fromkeys(order))
        state._battery_aggregate_charge_pct = aggregate_charge
        state._battery_aggregate_status = aggregate_status
        sample_utc = dt_util.utcnow()
        state._battery_summary_sample_utc = sample_utc
        if live_profiles:
            live_profile, live_label = live_profiles[0]
            state._battery_live_profile = live_profile
            state._battery_live_profile_label = live_label
            state._battery_live_profile_sample_utc = sample_utc
        else:
            state._battery_live_profile = None
            state._battery_live_profile_label = None
            state._battery_live_profile_sample_utc = None
        state._battery_aggregate_status_details = {
            "status": aggregate_status,
            "worst_storage_key": worst_key,
            "worst_status": worst_status,
            "per_battery_status": status_map,
            "per_battery_status_raw": raw_status_map,
            "per_battery_status_text": status_text_map,
            "included_count": included_count,
            "contributing_count": contributing_count,
            "aggregate_charge_source": aggregate_charge_source,
            "missing_energy_capacity_keys": list(
                dict.fromkeys(missing_energy_capacity_keys)
            ),
            "excluded_count": excluded_count,
            "available_energy_kwh": round(total_available_energy, 2),
            "max_capacity_kwh": round(total_capacity, 2),
            "site_current_charge_pct": site_current_charge,
            "site_available_energy_kwh": self._coerce_optional_kwh(
                payload.get("available_energy")
            ),
            "site_max_capacity_kwh": self._coerce_optional_kwh(
                payload.get("max_capacity")
            ),
            "site_available_power_kw": self._coerce_optional_float(
                payload.get("available_power")
            ),
            "site_max_power_kw": self._coerce_optional_float(payload.get("max_power")),
            "site_total_micros": self._coerce_optional_int(payload.get("total_micros")),
            "site_active_micros": self._coerce_optional_int(
                payload.get("active_micros")
            ),
            "site_inactive_micros": self._coerce_optional_int(
                payload.get("inactive_micros")
            ),
            "site_included_count": self._coerce_optional_int(
                payload.get("included_count")
            ),
            "site_excluded_count": self._coerce_optional_int(
                payload.get("excluded_count")
            ),
        }
        self._sync_ac_battery_from_battery_status(snapshots)
        self._refresh_cached_topology()
        if (
            self.effective_profile_matches_pending()
            and self._pending_authoritative_confirmation_received()
        ):
            self.confirm_battery_pending()

    def _sync_ac_battery_from_battery_status(
        self, snapshots: dict[str, dict[str, object]]
    ) -> None:
        """Seed AC Battery records from battery status when the HTML page is empty."""

        coord = self.coordinator
        state = self.battery_state
        if getattr(coord, "battery_has_acb", None) is not True:
            return
        inventory_view = getattr(coord, "inventory_view", None)
        has_type_for_entities = getattr(inventory_view, "has_type_for_entities", None)
        if callable(has_type_for_entities) and not has_type_for_entities("ac_battery"):
            return

        existing = getattr(state, "_ac_battery_data", None)
        rows: dict[str, dict[str, object]] = (
            {
                str(key): dict(value)
                for key, value in existing.items()
                if isinstance(value, dict)
            }
            if isinstance(existing, dict)
            else {}
        )

        known_ids: set[str] = {
            str(snapshot.get("battery_id"))
            for snapshot in rows.values()
            if snapshot.get("battery_id") is not None
        }
        known_serials: set[str] = {
            str(snapshot.get("serial_number"))
            for snapshot in rows.values()
            if snapshot.get("serial_number") is not None
        }
        type_bucket = (
            inventory_view.type_bucket("ac_battery")
            if callable(getattr(inventory_view, "type_bucket", None))
            else None
        )
        members = type_bucket.get("devices") if isinstance(type_bucket, dict) else None
        if isinstance(members, list):
            for member in members:
                for key in ("id", "device_id", "device_uid", "uid"):
                    value = member.get(key)
                    if value is not None:
                        known_ids.add(str(value))
                for key in ("serial_number", "serial", "serialNumber"):
                    value = member.get(key)
                    if value is not None:
                        known_serials.add(str(value))

        acb_only_site = getattr(
            coord, "battery_has_encharge", None
        ) is not True and bool(snapshots)
        if not acb_only_site and not known_ids and not known_serials:
            return

        order = list(getattr(state, "_ac_battery_order", []) or [])
        status_map: dict[str, str] = {}
        status_text_map: dict[str, str | None] = {}
        worst_key: str | None = None
        worst_status: str | None = None
        worst_severity = self._battery_status_severity_value("normal")
        synced_count = 0

        for key, source in snapshots.items():
            battery_id = self._normalize_battery_id(source.get("battery_id"))
            serial = self._coerce_optional_text(source.get("serial_number")) or key
            if (
                not acb_only_site
                and battery_id not in known_ids
                and serial not in known_serials
                and key not in known_serials
            ):
                continue

            row = dict(rows.get(key, {}))
            row.setdefault("serial_number", serial)
            if battery_id is not None:
                row["battery_id"] = battery_id
            # Keep this fallback to inventory/status fields. The battery-status
            # endpoint's available_power/max_power values describe capability,
            # not the AC Battery's current live power.
            for source_key, target_key in (
                ("current_charge_pct", "current_charge_pct"),
                ("cycle_count", "cycle_count"),
                ("status_text", "status_text"),
                ("status_normalized", "status_normalized"),
                ("available_energy_kwh", "available_energy_kwh"),
                ("max_capacity_kwh", "max_capacity_kwh"),
                ("battery_mode", "battery_mode"),
                ("rated_power", "rated_power"),
                ("battery_phase_count", "battery_phase_count"),
            ):
                if source.get(source_key) is not None:
                    row[target_key] = source[source_key]
            if source.get("last_report") is not None:
                parsed_last_report = parse_inverter_last_report(
                    source.get("last_report")
                )
                if parsed_last_report is not None:
                    row["last_reported"] = parsed_last_report
            rows[key] = row
            synced_count += 1
            if key not in order:
                order.append(key)

            normalized_status = self._coerce_optional_text(row.get("status_normalized"))
            if normalized_status is None:
                normalized_status = "unknown"
            status_map[key] = normalized_status
            status_text_map[key] = self._coerce_optional_text(row.get("status_text"))
            severity = self._battery_status_severity_value(normalized_status)
            if severity > worst_severity or worst_status is None:
                worst_severity = severity
                worst_status = normalized_status
                worst_key = key

        if synced_count == 0:
            return

        state._ac_battery_data = rows
        state._ac_battery_order = list(dict.fromkeys(order))
        details = dict(getattr(state, "_ac_battery_aggregate_status_details", {}) or {})
        details.update(
            {
                "battery_count": len(rows),
                "status_source": "battery_status",
                "per_battery_status": status_map,
                "per_battery_status_text": status_text_map,
                "worst_storage_key": worst_key,
                "worst_status": worst_status,
                "battery_ids": {
                    serial_key: snapshot.get("battery_id")
                    for serial_key, snapshot in rows.items()
                },
            }
        )
        if "sleep_state" not in details:
            details["sleep_state"] = getattr(state, "_ac_battery_sleep_state", None)
        if "selected_sleep_min_soc" not in details:
            details["selected_sleep_min_soc"] = getattr(
                state, "_ac_battery_selected_sleep_min_soc", None
            )
        state._ac_battery_aggregate_status_details = details
        state._ac_battery_aggregate_status = worst_status or "unknown"
        self._ac_battery_runtime.refresh_ac_battery_summary()

    def parse_battery_site_settings_payload(self, payload: object) -> None:
        state = self.battery_state
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload

        def _as_text(value: object) -> str | None:
            if value is None:
                return None
            try:
                text = str(value).strip()
            except Exception:
                return None
            return text or None

        state._battery_show_production = self._coerce_optional_bool(
            data.get("showProduction")
        )
        state._battery_show_consumption = self._coerce_optional_bool(
            data.get("showConsumption")
        )
        state._battery_show_charge_from_grid = self._coerce_optional_bool(
            data.get("showChargeFromGrid")
        )
        state._battery_show_savings_mode = self._coerce_optional_bool(
            data.get("showSavingsMode")
        )
        ai_opti_savings_mode = self._coerce_optional_bool(
            data.get("showAiOptiSavingsMode")
        )
        state._battery_show_ai_optimisation_mode = ai_opti_savings_mode
        state._battery_show_ai_opti_savings_mode = ai_opti_savings_mode
        state._battery_is_emea = self._coerce_optional_bool(data.get("isEmea"))
        state._battery_show_storm_guard = self._coerce_optional_bool(
            data.get("showStormGuard")
        )
        state._battery_show_full_backup = self._coerce_optional_bool(
            data.get("showFullBackup")
        )
        state._battery_show_battery_backup_percentage = self._coerce_optional_bool(
            data.get("showBatteryBackupPercentage")
        )
        state._battery_is_charging_modes_enabled = self._coerce_optional_bool(
            data.get("isChargingModesEnabled")
        )
        state._battery_has_encharge = self._coerce_optional_bool(
            data.get("hasEncharge")
        )
        state._battery_has_acb = self._coerce_optional_bool(data.get("hasAcb"))
        state._battery_has_enpower = self._coerce_optional_bool(data.get("hasEnpower"))
        state._battery_limit_support = self._coerce_optional_bool(
            data.get("batteryLimitSupport")
        )
        state._battery_country_code = _as_text(data.get("countryCode"))
        state._battery_region = _as_text(data.get("region"))
        state._battery_locale = _as_text(data.get("locale"))
        state._battery_timezone = _as_text(data.get("timezone"))
        grid_mode = self._normalize_battery_grid_mode(data.get("batteryGridMode"))
        if grid_mode is not None:
            state._battery_grid_mode = grid_mode
        raw_feature_details = data.get("featureDetails")
        feature_details: dict[str, object] = {}
        if isinstance(raw_feature_details, dict):
            for key, value in raw_feature_details.items():
                key_text = _as_text(key)
                if not key_text:
                    continue
                normalized_bool = self._coerce_optional_bool(value)
                if normalized_bool is not None:
                    feature_details[key_text] = normalized_bool
                    continue
                if isinstance(value, (str, int, float)):
                    feature_details[key_text] = value
        state._battery_feature_details = feature_details
        self._apply_battery_capability_blocks(data)
        self._apply_battery_user_details(data.get("userDetails"))
        site_status = data.get("siteStatus")
        if isinstance(site_status, dict):
            state._battery_site_status_code = _as_text(site_status.get("code"))
            state._battery_site_status_text = _as_text(site_status.get("text"))
            state._battery_site_status_severity = _as_text(site_status.get("severity"))

    def parse_battery_settings_payload(
        self,
        payload: object,
        *,
        clear_missing_schedule_times: bool = True,
        clear_missing_reserve_bounds: bool = True,
        clear_missing_controls: bool = False,
    ) -> None:
        state = self.battery_state
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload

        grid_mode = self._normalize_battery_grid_mode(data.get("batteryGridMode"))
        if grid_mode is not None:
            state._battery_grid_mode = grid_mode
        hide_cfg = self._coerce_optional_bool(data.get("hideChargeFromGrid"))
        if hide_cfg is not None:
            state._battery_hide_charge_from_grid = hide_cfg
        supports_vls = self._coerce_optional_bool(data.get("envoySupportsVls"))
        if supports_vls is not None:
            state._battery_envoy_supports_vls = supports_vls
        charge_from_grid = self._coerce_optional_bool(data.get("chargeFromGrid"))
        if charge_from_grid is not None:
            state._battery_charge_from_grid = charge_from_grid
        schedule_enabled = self._coerce_optional_bool(
            data.get("chargeFromGridScheduleEnabled")
        )
        if schedule_enabled is not None:
            state._battery_charge_from_grid_schedule_enabled = schedule_enabled
        begin = self._normalize_minutes_of_day(data.get("chargeBeginTime"))
        if begin is not None:
            state._battery_charge_begin_time = begin
        elif clear_missing_schedule_times:
            state._battery_charge_begin_time = None
        end = self._normalize_minutes_of_day(data.get("chargeEndTime"))
        if end is not None:
            state._battery_charge_end_time = end
        elif clear_missing_schedule_times:
            state._battery_charge_end_time = None
        accepted = data.get("acceptedItcDisclaimer")
        if accepted is not None:
            if accepted is True:
                pass
            else:
                try:
                    state._battery_accepted_itc_disclaimer = str(accepted)
                except Exception:
                    state._battery_accepted_itc_disclaimer = None
        very_low_soc = self._coerce_optional_int(data.get("veryLowSoc"))
        if very_low_soc is not None:
            state._battery_very_low_soc = very_low_soc
        very_low_soc_min = self._coerce_optional_int(data.get("veryLowSocMin"))
        if very_low_soc_min is not None:
            state._battery_very_low_soc_min = very_low_soc_min
        very_low_soc_max = self._coerce_optional_int(data.get("veryLowSocMax"))
        if very_low_soc_max is not None:
            state._battery_very_low_soc_max = very_low_soc_max
        self.remember_previous_battery_reserves(
            data.get("previousBatteryBackupPercentage")
        )
        settings_profile = self.normalize_battery_profile_key(data.get("profile"))
        profile_write_awaiting_authoritative_read = (
            self._pending_requires_authoritative_confirmation()
        )
        stale_profile_read = (
            profile_write_awaiting_authoritative_read
            or self._settings_profile_is_stale_after_authoritative_read(
                settings_profile
            )
        )
        if (
            settings_profile is not None
            and not stale_profile_read
            and (
                not getattr(state, "_battery_profile_authoritative_seen", False)
                or not self._profile_authoritative_read_is_fresh()
            )
        ):
            state._battery_profile = settings_profile
        settings_reserve = self._coerce_optional_int(
            data.get("batteryBackupPercentage")
        )
        settings_reserve_min = self._coerce_optional_int(
            data.get("batteryBackupPercentageMin")
        )
        if settings_reserve_min is not None:
            state._battery_backup_percentage_min = max(
                0, min(100, int(settings_reserve_min))
            )
        elif clear_missing_reserve_bounds:
            state._battery_backup_percentage_min = None
        settings_reserve_max = self._coerce_optional_int(
            data.get("batteryBackupPercentageMax")
        )
        if settings_reserve_max is not None:
            state._battery_backup_percentage_max = max(
                0, min(100, int(settings_reserve_max))
            )
        elif clear_missing_reserve_bounds:
            state._battery_backup_percentage_max = None
        if settings_reserve is not None and not stale_profile_read:
            state._battery_backup_percentage = (
                self.normalize_battery_reserve_for_profile(
                    settings_profile or state._battery_profile or "self-consumption",
                    settings_reserve,
                )
            )
            self.remember_battery_reserve(
                settings_profile or state._battery_profile,
                state._battery_backup_percentage,
            )
        settings_subtype = self._normalize_battery_sub_type(
            data.get("operationModeSubType")
        )
        if settings_subtype is not None and not stale_profile_read:
            state._battery_operation_mode_sub_type = settings_subtype
        elif not stale_profile_read and (
            settings_profile or state._battery_profile
        ) not in {
            "cost_savings",
            "ai_optimisation",
        }:
            state._battery_operation_mode_sub_type = None
        storm_state = self.normalize_storm_guard_state(data.get("stormGuardState"))
        if storm_state is not None:
            state._storm_guard_state = storm_state
        self.sync_storm_guard_pending(storm_state)
        self._apply_battery_capability_blocks(
            data,
            clear_missing_controls=clear_missing_controls,
        )
        dtg_enabled = self._coerce_optional_bool(
            (data.get("dtgControl") or {}).get("enabled")
            if isinstance(data.get("dtgControl"), dict)
            else None
        )
        if dtg_enabled is not None:
            state._battery_dtg_schedule_enabled = dtg_enabled
        elif clear_missing_controls and "dtgControl" not in data:
            state._battery_dtg_schedule_enabled = None
        rbd_enabled = self._coerce_optional_bool(
            (data.get("rbdControl") or {}).get("enabled")
            if isinstance(data.get("rbdControl"), dict)
            else None
        )
        if rbd_enabled is not None:
            state._battery_rbd_schedule_enabled = rbd_enabled
        elif clear_missing_controls and "rbdControl" not in data:
            state._battery_rbd_schedule_enabled = None
        raw_devices = data.get("devices")
        if isinstance(raw_devices, dict):
            iq_evse = raw_devices.get("iqEvse")
            if isinstance(iq_evse, dict):
                use_battery = self._coerce_optional_bool(
                    iq_evse.get("useBatteryFrSelfConsumption")
                )
                if use_battery is not None:
                    state._battery_use_battery_for_self_consumption = use_battery
        self.sync_backend_battery_profile_pending(data.get("isBatteryChangePending"))

        if (
            self.effective_profile_matches_pending()
            and self._pending_authoritative_confirmation_received()
        ):
            self.confirm_battery_pending()

    def parse_battery_schedules_payload(self, payload: object) -> None:
        state = self.battery_state
        coord = self.coordinator

        def _control_enabled(schedule_type: str) -> bool | None:
            normalized = str(schedule_type).lower()
            attr_name = {
                "cfg": "battery_charge_from_grid_schedule_enabled",
                "dtg": "battery_dtg_control_enabled",
                "rbd": "battery_rbd_control_enabled",
            }.get(normalized, "")
            value = getattr(coord, attr_name, None)
            return value if isinstance(value, bool) else None

        def _reset_family(schedule_type: str) -> None:
            setattr(state, self._battery_schedule_limit_attr(schedule_type), None)
            setattr(state, self._battery_schedule_id_attr(schedule_type), None)
            setattr(state, self._battery_schedule_days_attr(schedule_type), None)
            setattr(state, self._battery_schedule_timezone_attr(schedule_type), None)
            setattr(state, self._battery_schedule_status_attr(schedule_type), None)
            setattr(state, self._battery_schedule_enabled_attr(schedule_type), None)
            if str(schedule_type).lower() != "cfg":
                setattr(state, self._battery_schedule_start_attr(schedule_type), None)
                setattr(state, self._battery_schedule_end_attr(schedule_type), None)

        def _apply_family(schedule_type: str, family_payload: object) -> None:
            _reset_family(schedule_type)
            if not isinstance(family_payload, dict):
                return
            family_status = family_payload.get("scheduleStatus")
            if isinstance(family_status, str) and family_status.strip():
                setattr(
                    state,
                    self._battery_schedule_status_attr(schedule_type),
                    family_status.strip().lower(),
                )

            details = family_payload.get("details")
            if not isinstance(details, list) or not details:
                return
            preferred_enabled = _control_enabled(schedule_type)
            chosen = None
            for entry in details:
                if not isinstance(entry, dict):
                    continue
                if chosen is None:
                    chosen = entry
                entry_enabled = self._coerce_optional_bool(entry.get("isEnabled"))
                if preferred_enabled is not None:
                    if entry_enabled is preferred_enabled:
                        chosen = entry
                        break
                    continue
                if entry.get("isEnabled") is True:
                    chosen = entry
                    break
            if chosen is None:
                return

            start_str = chosen.get("startTime")
            if isinstance(start_str, str) and ":" in start_str:
                try:
                    parts = start_str.split(":")
                    setattr(
                        state,
                        self._battery_schedule_start_attr(schedule_type),
                        int(parts[0]) * 60 + int(parts[1]),
                    )
                except (ValueError, IndexError):
                    pass
            end_str = chosen.get("endTime")
            if isinstance(end_str, str) and ":" in end_str:
                try:
                    parts = end_str.split(":")
                    setattr(
                        state,
                        self._battery_schedule_end_attr(schedule_type),
                        int(parts[0]) * 60 + int(parts[1]),
                    )
                except (ValueError, IndexError):
                    pass

            schedule_id = chosen.get("scheduleId")
            if schedule_id is not None:
                setattr(
                    state,
                    self._battery_schedule_id_attr(schedule_type),
                    str(schedule_id),
                )
            days = chosen.get("days")
            if isinstance(days, list):
                setattr(
                    state,
                    self._battery_schedule_days_attr(schedule_type),
                    [int(d) for d in days],
                )
            tz = chosen.get("timezone")
            if not isinstance(tz, str) or not tz.strip():
                tz = payload.get("timezone") if isinstance(payload, dict) else None
            if isinstance(tz, str) and tz.strip():
                setattr(
                    state,
                    self._battery_schedule_timezone_attr(schedule_type),
                    tz.strip(),
                )
            limit = chosen.get("limit")
            if isinstance(limit, (int, float)):
                setattr(
                    state,
                    self._battery_schedule_limit_attr(schedule_type),
                    int(limit),
                )
            enabled = self._coerce_optional_bool(chosen.get("isEnabled"))
            if enabled is not None and _control_enabled(schedule_type) is None:
                setattr(
                    state,
                    self._battery_schedule_enabled_attr(schedule_type),
                    enabled,
                )
            entry_status = chosen.get("scheduleStatus")
            status = entry_status or family_status
            if isinstance(status, str) and status.strip():
                setattr(
                    state,
                    self._battery_schedule_status_attr(schedule_type),
                    status.strip().lower(),
                )

        for schedule_type in ("cfg", "dtg", "rbd"):
            family_payload = (
                payload.get(schedule_type) if isinstance(payload, dict) else None
            )
            _apply_family(schedule_type, family_payload)

    def parse_dry_contact_settings_payload(self, payload: object) -> None:
        state = self.battery_state
        result = parse_dry_contact_settings_payload(
            payload,
            members=(
                self._dry_contact_members_for_settings()
                if isinstance(payload, dict)
                else []
            ),
            coerce_bool=self._coerce_optional_bool,
            coerce_int=self._coerce_optional_int,
            coerce_text=self._coerce_optional_text,
        )
        state._dry_contact_settings_entries = result.entries
        state._dry_contact_unmatched_settings = result.unmatched
        state._dry_contact_settings_supported = result.supported

    def dry_contact_settings_matches(
        self,
        members: list[dict[str, object]] | tuple[dict[str, object], ...] | object,
    ) -> tuple[list[dict[str, object] | None], list[dict[str, object]]]:
        matches, unmatched = self._match_dry_contact_settings(
            (
                [dict(member) for member in members if isinstance(member, dict)]
                if isinstance(members, (list, tuple))
                else []
            ),
            settings_entries=[
                entry
                for entry in getattr(
                    self.battery_state, "_dry_contact_settings_entries", []
                )
                if isinstance(entry, dict)
            ],
        )
        return (
            [
                (
                    self._copy_dry_contact_settings_entry(entry)
                    if isinstance(entry, dict)
                    else None
                )
                for entry in matches
            ],
            [
                self._copy_dry_contact_settings_entry(entry)
                for entry in unmatched
                if isinstance(entry, dict)
            ],
        )

    def parse_grid_control_check_payload(self, payload: object) -> None:
        state = self.battery_state
        keys = (
            "disableGridControl",
            "activeDownload",
            "sunlightBackupSystemCheck",
            "gridOutageCheck",
            "userInitiatedGridToggle",
        )
        if not isinstance(payload, dict):
            state._grid_control_supported = False
            state._grid_control_disable = None
            state._grid_control_active_download = None
            state._grid_control_sunlight_backup_system_check = None
            state._grid_control_grid_outage_check = None
            state._grid_control_user_initiated_toggle = None
            return
        data = payload.get("data")
        if isinstance(data, dict):
            payload = data
        if not any(key in payload for key in keys):
            state._grid_control_supported = False
            state._grid_control_disable = None
            state._grid_control_active_download = None
            state._grid_control_sunlight_backup_system_check = None
            state._grid_control_grid_outage_check = None
            state._grid_control_user_initiated_toggle = None
            return
        state._grid_control_supported = True
        state._grid_control_disable = self._coerce_optional_bool(
            payload.get("disableGridControl")
        )
        state._grid_control_active_download = self._coerce_optional_bool(
            payload.get("activeDownload")
        )
        state._grid_control_sunlight_backup_system_check = self._coerce_optional_bool(
            payload.get("sunlightBackupSystemCheck")
        )
        state._grid_control_grid_outage_check = self._coerce_optional_bool(
            payload.get("gridOutageCheck")
        )
        state._grid_control_user_initiated_toggle = self._coerce_optional_bool(
            payload.get("userInitiatedGridToggle")
        )

    def backup_history_tzinfo(self) -> _tz | ZoneInfo:
        tz_name = getattr(self.battery_state, "_battery_timezone", None)
        if isinstance(tz_name, str) and tz_name.strip():
            try:
                return ZoneInfo(tz_name.strip())
            except Exception:  # noqa: BLE001
                pass
        default_tz = getattr(dt_util, "DEFAULT_TIME_ZONE", None)
        if default_tz is not None:
            return default_tz
        return _tz.utc

    async def async_refresh_battery_status(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        family = "battery_status"
        if not force and state._battery_status_cache_until:
            if now < state._battery_status_cache_until:
                return
        if not coord._endpoint_family_should_run(family, force=force):
            return
        fetcher = getattr(coord.client, "battery_status", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            state._battery_status_cache_until = coord._endpoint_family_next_retry_mono(
                family
            )
            return
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._battery_status_payload = redacted_payload
        else:
            state._battery_status_payload = {"value": redacted_payload}
        self.parse_battery_status_payload(payload)
        coord._note_endpoint_family_success(family)
        state._battery_status_cache_until = coord._endpoint_family_next_retry_mono(
            family
        )

    async def async_refresh_battery_backup_history(
        self, *, force: bool = False
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        family = "battery_backup_history"
        if not force and state._battery_backup_history_cache_until:
            if now < state._battery_backup_history_cache_until:
                return
        if not coord._endpoint_family_should_run(family, force=force):
            return
        fetcher = getattr(coord.client, "battery_backup_history", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            state._battery_backup_history_cache_until = (
                now + BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL
            )
            return
        parsed = self.parse_battery_backup_history_payload(payload)
        if parsed is None:
            coord._note_endpoint_family_failure(
                family,
                ValueError(
                    f"Battery backup history payload was invalid for site {coord.site_id}"
                ),
            )
            state._battery_backup_history_cache_until = (
                now + BATTERY_BACKUP_HISTORY_FAILURE_CACHE_TTL
            )
            return
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._battery_backup_history_payload = redacted_payload
        else:
            state._battery_backup_history_payload = {"value": redacted_payload}
        state._battery_backup_history_events = parsed
        state._battery_backup_history_cache_until = (
            now + BATTERY_BACKUP_HISTORY_CACHE_TTL
        )
        coord._note_endpoint_family_success(family)

    async def async_refresh_battery_settings(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        family = "battery_settings"
        pending_profile = getattr(state, "_battery_pending_profile", None)
        if not force and not pending_profile and state._battery_settings_cache_until:
            if now < state._battery_settings_cache_until:
                return
        if not coord._endpoint_family_should_run(
            family,
            force=force or bool(pending_profile),
        ):
            return
        fetcher = getattr(coord.client, "battery_settings_details", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            return
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._battery_settings_payload = redacted_payload
        else:
            state._battery_settings_payload = {"value": redacted_payload}
        self.parse_battery_settings_payload(
            payload,
            clear_missing_schedule_times=True,
            clear_missing_controls=True,
        )
        self.sync_cfg_settings_pending()
        success_ttl = self._battery_control_refresh_success_ttl_seconds(
            BATTERY_SETTINGS_CACHE_TTL
        )
        state._battery_settings_cache_until = now + success_ttl
        coord._note_endpoint_family_success(family, success_ttl_s=success_ttl)

    async def async_refresh_battery_schedules(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        family = "battery_schedules"
        if not coord._endpoint_family_should_run(family, force=force):
            return
        fetcher = getattr(coord.client, "battery_schedules", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            return
        if not isinstance(payload, dict):
            coord._note_endpoint_family_failure(
                family, ValueError("Battery schedules payload was not a dictionary")
            )
            return
        redacted = coord.redact_battery_payload(payload)
        if isinstance(redacted, dict):
            state._battery_schedules_payload = redacted
        else:
            state._battery_schedules_payload = {"value": redacted}
        self.parse_battery_schedules_payload(payload)
        coord._note_endpoint_family_success(
            family,
            success_ttl_s=self._battery_control_refresh_success_ttl_seconds(
                BATTERY_SETTINGS_CACHE_TTL
            ),
        )

    async def async_refresh_battery_site_settings(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        family = "battery_site_settings"
        if not force and state._battery_site_settings_cache_until:
            if now < state._battery_site_settings_cache_until:
                return
        if not coord._endpoint_family_should_run(family, force=force):
            return
        fetcher = getattr(coord.client, "battery_site_settings", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            return
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._battery_site_settings_payload = redacted_payload
        else:
            state._battery_site_settings_payload = {"value": redacted_payload}
        self.parse_battery_site_settings_payload(payload)
        state._battery_site_settings_cache_until = now + BATTERY_SITE_SETTINGS_CACHE_TTL
        coord._note_endpoint_family_success(family)

    async def async_refresh_grid_control_check(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        family = "grid_control_check"
        if not force and state._grid_control_check_cache_until:
            if now < state._grid_control_check_cache_until:
                return
        if not coord._endpoint_family_should_run(family, force=force):
            if state._grid_control_supported is not None and (
                coord._endpoint_family_state(family).cooldown_active
                and not coord._endpoint_family_can_use_stale(family)
            ):
                state._grid_control_supported = None
                state._grid_control_disable = None
                state._grid_control_active_download = None
                state._grid_control_sunlight_backup_system_check = None
                state._grid_control_grid_outage_check = None
                state._grid_control_user_initiated_toggle = None
            return
        fetcher = getattr(coord.client, "grid_control_check", None)
        if not callable(fetcher):
            state._grid_control_supported = None
            state._grid_control_disable = None
            state._grid_control_active_download = None
            state._grid_control_sunlight_backup_system_check = None
            state._grid_control_grid_outage_check = None
            state._grid_control_user_initiated_toggle = None
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            state._grid_control_check_failures = max(
                state._grid_control_check_failures + 1,
                coord._endpoint_family_state(family).consecutive_failures,
            )
            last_success = getattr(state, "_grid_control_check_last_success_mono", None)
            if (
                not isinstance(last_success, (int, float))
                or (now - float(last_success)) >= GRID_CONTROL_CHECK_STALE_AFTER_S
            ):
                state._grid_control_supported = None
                state._grid_control_disable = None
                state._grid_control_active_download = None
                state._grid_control_sunlight_backup_system_check = None
                state._grid_control_grid_outage_check = None
                state._grid_control_user_initiated_toggle = None
            state._grid_control_check_cache_until = now + 15.0
            return
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._grid_control_check_payload = redacted_payload
        else:
            state._grid_control_check_payload = {"value": redacted_payload}
        self.parse_grid_control_check_payload(payload)
        state._grid_control_check_failures = 0
        state._grid_control_check_last_success_mono = now
        state._grid_control_check_cache_until = now + GRID_CONTROL_CHECK_CACHE_TTL
        coord._note_endpoint_family_success(family)

    async def async_refresh_dry_contact_settings(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        family = "dry_contact_settings"
        if not force and state._dry_contact_settings_cache_until:
            if now < state._dry_contact_settings_cache_until:
                return
        if not coord._endpoint_family_should_run(family, force=force):
            if state._dry_contact_settings_supported is not None and (
                coord._endpoint_family_state(family).cooldown_active
                and not coord._endpoint_family_can_use_stale(family)
            ):
                state._dry_contact_settings_supported = None
            return
        fetcher = getattr(coord.client, "dry_contacts_settings", None)
        if not callable(fetcher):
            state._dry_contact_settings_supported = None
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            state._dry_contact_settings_failures = max(
                state._dry_contact_settings_failures + 1,
                coord._endpoint_family_state(family).consecutive_failures,
            )
            last_success = getattr(
                state, "_dry_contact_settings_last_success_mono", None
            )
            if (
                not isinstance(last_success, (int, float))
                or (now - float(last_success)) >= DRY_CONTACT_SETTINGS_STALE_AFTER_S
            ):
                state._dry_contact_settings_supported = None
            state._dry_contact_settings_cache_until = (
                now + DRY_CONTACT_SETTINGS_FAILURE_CACHE_TTL
            )
            return
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._dry_contact_settings_payload = redacted_payload
        else:
            state._dry_contact_settings_payload = {"value": redacted_payload}
        self.parse_dry_contact_settings_payload(payload)
        state._dry_contact_settings_failures = 0
        state._dry_contact_settings_last_success_mono = now
        state._dry_contact_settings_cache_until = now + DRY_CONTACT_SETTINGS_CACHE_TTL
        coord._note_endpoint_family_success(family)

    @staticmethod
    def normalize_storm_guard_state(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return "enabled" if value else "disabled"
        if isinstance(value, (int, float)):
            return "enabled" if value != 0 else "disabled"
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("enabled", "disabled"):
                return normalized
            if normalized in ("true", "1", "yes", "y", "on"):
                return "enabled"
            if normalized in ("false", "0", "no", "n", "off"):
                return "disabled"
        return None

    def clear_storm_guard_pending(self) -> None:
        state = self.battery_state
        state._storm_guard_pending_state = None
        state._storm_guard_pending_expires_mono = None

    def set_storm_guard_pending(self, target_state: str) -> None:
        state = self.battery_state
        normalized = self.normalize_storm_guard_state(target_state)
        if normalized is None:
            self.clear_storm_guard_pending()
            return
        state._storm_guard_pending_state = normalized
        state._storm_guard_pending_expires_mono = (
            time.monotonic() + STORM_GUARD_PENDING_HOLD_S
        )

    def sync_storm_guard_pending(self, effective_state: str | None = None) -> None:
        state = self.battery_state
        pending_state = getattr(state, "_storm_guard_pending_state", None)
        if pending_state is None:
            return
        if effective_state is None:
            effective_state = self.normalize_storm_guard_state(
                getattr(state, "_storm_guard_state", None)
            )
        else:
            effective_state = self.normalize_storm_guard_state(effective_state)
        if effective_state == pending_state:
            self.clear_storm_guard_pending()
            return
        expires_at = getattr(state, "_storm_guard_pending_expires_mono", None)
        if expires_at is None:
            self.clear_storm_guard_pending()
            return
        if time.monotonic() >= float(expires_at):
            self.clear_storm_guard_pending()

    def parse_storm_guard_profile(
        self, payload: object
    ) -> tuple[str | None, bool | None]:
        if not isinstance(payload, dict):
            return None, None
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        state = self.normalize_storm_guard_state(data.get("stormGuardState"))
        evse = self._coerce_optional_bool(data.get("evseStormEnabled"))
        return state, evse

    def storm_alert_status_is_inactive(self, status: str | None) -> bool:
        if status is None:
            return False
        return status in STORM_ALERT_INACTIVE_STATUSES

    def storm_alert_is_active(self, alert: dict[str, object]) -> bool:
        coord = self.coordinator
        explicit_active = coord.coerce_optional_bool(alert.get("active"))
        if explicit_active is not None:
            return explicit_active
        status = coord.coerce_optional_text(alert.get("status"))
        if status:
            normalized_status = status.strip().lower().replace("_", "-")
            if normalized_status:
                return not self.storm_alert_status_is_inactive(normalized_status)
        return True

    def parse_storm_alert(self, payload: object) -> bool | None:
        coord = self.coordinator
        state = self.battery_state
        if not isinstance(payload, dict):
            return None
        state._storm_alert_critical_override = coord.coerce_optional_bool(
            payload.get("criticalAlertsOverride")
        )
        alerts = payload.get("stormAlerts")
        normalized_alerts: list[dict[str, object]] = []
        derived_alert_active: bool | None = None
        if isinstance(alerts, list):
            derived_alert_active = False
            for alert in alerts:
                alert_active = False
                if isinstance(alert, dict):
                    normalized: dict[str, object] = {}
                    for key in (
                        "id",
                        "name",
                        "source",
                        "status",
                        "active",
                        "critical",
                        "type",
                        "severity",
                        "message",
                        "startTime",
                        "endTime",
                        "region",
                    ):
                        if key in alert and alert.get(key) is not None:
                            normalized[key] = alert.get(key)
                    if not normalized:
                        for key, value in alert.items():
                            if isinstance(value, (str, int, float, bool)):
                                normalized[str(key)] = value
                    if normalized:
                        normalized_alerts.append(normalized)
                    else:
                        normalized_alerts.append({"active": True})
                    alert_active = self.storm_alert_is_active(alert)
                elif alert is not None:
                    try:
                        normalized_alerts.append({"value": str(alert)})
                    except Exception:  # noqa: BLE001
                        normalized_alerts.append({"active": True})
                    alert_active = True
                if alert_active:
                    derived_alert_active = True
        state._storm_alerts = normalized_alerts
        critical_active = coord.coerce_optional_bool(payload.get("criticalAlertActive"))
        if derived_alert_active is None:
            return critical_active
        if critical_active is None:
            return derived_alert_active
        return critical_active or derived_alert_active

    async def async_refresh_storm_guard_profile(self, *, force: bool = False) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        family = "storm_guard"
        pending_profile = getattr(state, "_battery_pending_profile", None)
        if not force and not pending_profile and state._storm_guard_cache_until:
            if now < state._storm_guard_cache_until:
                return
        if not coord._endpoint_family_should_run(
            family,
            force=force or bool(pending_profile),
        ):
            return
        try:
            locale = getattr(coord.hass.config, "language", None)
        except Exception:  # noqa: BLE001
            locale = None
        fetcher = getattr(coord.client, "storm_guard_profile", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher(locale=locale)
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            return
        redacted_payload = coord.redact_battery_payload(payload)
        if isinstance(redacted_payload, dict):
            state._battery_profile_payload = redacted_payload
        else:
            state._battery_profile_payload = {"value": redacted_payload}
        self.parse_battery_profile_payload(payload)
        storm_state, evse = self.parse_storm_guard_profile(payload)
        if storm_state is not None:
            state._storm_guard_state = storm_state
        self.sync_storm_guard_pending(storm_state)
        if evse is not None:
            state._storm_evse_enabled = evse
        state._storm_guard_cache_until = now + (
            self._battery_profile_refresh_cache_ttl_seconds(STORM_GUARD_CACHE_TTL)
        )
        coord._note_endpoint_family_success(family)

    async def async_refresh_storm_alert(
        self,
        *,
        force: bool = False,
        raise_on_error: bool = False,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        now = time.monotonic()
        family = "storm_alert"
        if not force and state._storm_alert_cache_until:
            if now < state._storm_alert_cache_until:
                return
        if not coord._endpoint_family_should_run(family, force=force):
            return
        fetcher = getattr(coord.client, "storm_guard_alert", None)
        if not callable(fetcher):
            return
        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(family, err)
            if raise_on_error:
                raise
            return
        active = self.parse_storm_alert(payload)
        if active is not None:
            state._storm_alert_active = active
        state._storm_alert_cache_until = now + STORM_ALERT_CACHE_TTL
        coord._note_endpoint_family_success(family)

    async def async_set_battery_reserve(self, reserve: int) -> None:
        coord = self.coordinator
        profile = coord.battery_selected_profile
        if not profile:
            self._raise_validation(
                "battery_profile_unavailable",
                message="Battery profile is unavailable.",
            )
        if profile == "backup_only":
            self._raise_validation(
                "full_backup_reserve_fixed",
                message="Full Backup reserve is fixed at 100%.",
            )
        self._assert_battery_profile_feature_writable(
            "Battery reserve is unavailable.",
            unavailable_key="battery_reserve_unavailable",
        )
        if not coord.battery_reserve_editable:
            self._raise_validation(
                "battery_reserve_unavailable",
                message="Battery reserve is unavailable.",
            )
        normalized = self.normalize_battery_reserve_for_profile(profile, reserve)
        await self.async_apply_battery_reserve_only(
            profile=profile,
            reserve=normalized,
        )

    async def async_set_savings_use_battery_after_peak(self, enabled: bool) -> None:
        coord = self.coordinator
        profile = coord.battery_selected_profile
        if profile != "cost_savings":
            self._raise_validation(
                "savings_profile_required",
                message="Savings profile must be active.",
            )
        self._assert_battery_profile_feature_writable(
            "Savings profile settings are unavailable.",
            unavailable_key="savings_profile_settings_unavailable",
        )
        if not coord.savings_use_battery_switch_available:
            self._raise_validation(
                "savings_profile_settings_unavailable",
                message="Savings profile settings are unavailable.",
            )
        reserve = coord.battery_selected_backup_percentage
        if reserve is None:
            reserve = self.target_reserve_for_profile("cost_savings")
        sub_type = SAVINGS_OPERATION_MODE_SUBTYPE if enabled else None
        await self.async_apply_battery_profile(
            profile="cost_savings",
            reserve=reserve,
            sub_type=sub_type,
        )

    async def async_set_system_profile(self, profile_key: str) -> None:
        coord = self.coordinator
        profile = self.normalize_battery_profile_key(profile_key)
        if not profile:
            self._raise_validation(
                "battery_profile_unavailable",
                message="Battery profile is unavailable.",
            )
        if profile not in coord.battery_profile_option_keys:
            self._raise_validation(
                "battery_profile_unsupported",
                message="Selected battery profile is not supported.",
            )
        self._assert_battery_profile_feature_writable(
            "Battery profile is unavailable.",
            unavailable_key="battery_profile_unavailable",
        )
        reserve = self.target_reserve_for_profile(profile)
        sub_type = self.target_operation_mode_sub_type(profile)
        if await self.async_recover_split_battery_profile(
            profile=profile, reserve=reserve
        ):
            return
        await self.async_apply_battery_profile(
            profile=profile,
            reserve=reserve,
            sub_type=sub_type,
            require_exact_pending_match=False,
        )

    async def async_cancel_pending_profile_change(self) -> None:
        coord = self.coordinator
        state = self.battery_state
        if not coord.battery_profile_pending:
            self.clear_battery_pending()
            self.clear_battery_optimistic_profile()
            return
        await self.async_assert_battery_profile_write_allowed()
        async with state._battery_profile_write_lock:
            state._battery_profile_last_write_mono = time.monotonic()
            try:
                await coord.client.cancel_battery_profile_update()
            except aiohttp.ClientResponseError as err:
                if not self._is_already_processed_profile_cancel_error(err):
                    raise
                _LOGGER.debug(
                    "Ignoring already-processed battery profile cancel on site %s",
                    redact_site_id(coord.site_id),
                )
        self.clear_battery_pending()
        self.clear_battery_optimistic_profile()
        state._storm_guard_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    async def async_set_charge_from_grid(self, enabled: bool) -> None:
        coord = self.coordinator
        self._assert_battery_settings_feature_writable(
            "Charge from grid setting is unavailable.",
            unavailable_key="charge_from_grid_unavailable",
        )
        if not coord.charge_from_grid_control_available:
            self._raise_validation(
                "charge_from_grid_unavailable",
                message="Charge from grid setting is unavailable.",
            )
        payload: dict[str, object] = {"chargeFromGrid": bool(enabled)}
        if enabled:
            await self._async_accept_charge_from_grid_disclaimer()
            await self._async_validate_cfg_schedule_commit()
            payload["acceptedItcDisclaimer"] = True
            await self.async_apply_battery_settings(payload)
        else:
            await self._async_validate_cfg_schedule_commit()
            await self.async_apply_battery_settings(payload)

        for attempt in range(4):
            await self.async_refresh_battery_settings(force=True)
            if coord.battery_charge_from_grid_enabled is enabled:
                self.clear_battery_settings_write_pending()
                return
            if attempt < 3:
                await asyncio.sleep(0.75)

        self._raise_validation(
            "charge_from_grid_toggle_not_applied",
            message="Charge from grid toggle was not applied by Enphase.",
        )

    async def async_set_charge_from_grid_schedule_enabled(self, enabled: bool) -> None:
        coord = self.coordinator
        self._assert_battery_settings_feature_writable(
            "Charge from grid schedule is unavailable.",
            unavailable_key="charge_from_grid_schedule_unavailable",
        )
        if not coord.charge_from_grid_force_schedule_supported:
            self._raise_validation(
                "charge_from_grid_schedule_unavailable",
                message="Charge from grid schedule is unavailable.",
            )
        start, end = self.current_charge_from_grid_schedule_window()
        if start == end:
            self._raise_validation(
                "charge_from_grid_schedule_times_different",
                message=(
                    "Charge-from-grid schedule start and end times must be different."
                ),
            )
        charge_from_grid_enabled = coord.battery_charge_from_grid_enabled is True
        payload: dict[str, object] = {
            "chargeFromGrid": True if enabled else charge_from_grid_enabled,
            "chargeFromGridScheduleEnabled": bool(enabled),
            "chargeBeginTime": start,
            "chargeEndTime": end,
            "acceptedItcDisclaimer": self.battery_itc_disclaimer_value(),
        }
        cfg_control = coord.battery_cfg_control
        if isinstance(cfg_control, dict):
            cfg_payload: dict[str, object] = {}
            field_map = {
                "show": "show",
                "enabled": "enabled",
                "locked": "locked",
                "show_day_schedule": "showDaySchedule",
                "schedule_supported": "scheduleSupported",
                "force_schedule_supported": "forceScheduleSupported",
            }
            for source_key, target_key in field_map.items():
                value = cfg_control.get(source_key)
                if value is not None:
                    cfg_payload[target_key] = value
            cfg_payload["forceScheduleOpted"] = bool(enabled)
            payload["cfgControl"] = cfg_payload

        async def _verify() -> bool:
            for attempt in range(4):
                await self.async_refresh_battery_settings(force=True)
                if coord.battery_charge_from_grid_schedule_enabled is enabled and (
                    not enabled or coord.battery_charge_from_grid_enabled is True
                ):
                    self.clear_battery_settings_write_pending()
                    return True
                if attempt < 3:
                    await asyncio.sleep(0.75)
            return False

        await self.async_apply_battery_settings(payload)
        if await _verify():
            return

        await self.async_apply_battery_settings_compat(
            payload,
            schedule_type="cfg",
            include_source=False,
            merged_payload=True,
            strip_devices=True,
        )
        if await _verify():
            return

        self._raise_validation(
            "charge_from_grid_schedule_toggle_not_applied",
            message="Charge-from-grid schedule toggle was not applied by Enphase.",
        )

    async def async_set_charge_from_grid_schedule_time(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        self._assert_battery_settings_feature_writable(
            "Charge from grid schedule is unavailable.",
            unavailable_key="charge_from_grid_schedule_unavailable",
        )
        if not coord.charge_from_grid_schedule_supported:
            self._raise_validation(
                "charge_from_grid_schedule_unavailable",
                message="Charge from grid schedule is unavailable.",
            )
        if coord.battery_charge_from_grid_enabled is not True:
            self._raise_validation(
                "charge_from_grid_required",
                message="Charge from grid must be enabled first.",
            )
        if coord.battery_cfg_schedule_pending:
            self._raise_validation(
                "schedule_change_pending",
                message="A schedule change is pending Envoy sync. Please wait.",
            )
        current_start, current_end = self.current_charge_from_grid_schedule_window()
        next_start = (
            coord.time_to_minutes_of_day(start) if start is not None else current_start
        )
        next_end = coord.time_to_minutes_of_day(end) if end is not None else current_end
        if next_start is None or next_end is None:
            self._raise_validation(
                "charge_from_grid_schedule_time_invalid",
                message="Charge-from-grid schedule time is invalid.",
            )
        if next_start == next_end:
            self._raise_validation(
                "charge_from_grid_schedule_times_different",
                message=(
                    "Charge-from-grid schedule start and end times must be different."
                ),
            )

        schedule_id = getattr(coord, "_battery_cfg_schedule_id", None)
        if schedule_id and hasattr(coord.client, "update_battery_schedule"):
            await self.async_assert_battery_settings_write_allowed()
            async with state._battery_settings_write_lock:
                state._battery_settings_last_write_mono = time.monotonic()
                start_hhmm = f"{next_start // 60:02d}:{next_start % 60:02d}"
                end_hhmm = f"{next_end // 60:02d}:{next_end % 60:02d}"
                limit = getattr(coord, "_battery_cfg_schedule_limit", None)
                if limit is None:
                    limit = 100
                days = getattr(coord, "_battery_cfg_schedule_days", None) or [
                    1,
                    2,
                    3,
                    4,
                    5,
                    6,
                    7,
                ]
                tz = getattr(coord, "_battery_cfg_schedule_timezone", None) or "UTC"
                await self.async_update_battery_schedule(
                    schedule_id,
                    start_time=start_hhmm,
                    end_time=end_hhmm,
                    limit=limit,
                    days=days,
                    timezone=tz,
                )
            state._battery_charge_begin_time = next_start
            state._battery_charge_end_time = next_end
            state._battery_settings_cache_until = None
            coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
            await coord.async_request_refresh()
            return

        if (
            hasattr(coord.client, "create_battery_schedule")
            and getattr(coord, "_battery_schedules_payload", None) is not None
        ):
            await self.async_assert_battery_settings_write_allowed()
            async with state._battery_settings_write_lock:
                state._battery_settings_last_write_mono = time.monotonic()
                start_hhmm = f"{next_start // 60:02d}:{next_start % 60:02d}"
                end_hhmm = f"{next_end // 60:02d}:{next_end % 60:02d}"
                days = [1, 2, 3, 4, 5, 6, 7]
                tz = getattr(coord, "_battery_cfg_schedule_timezone", None) or "UTC"
                _LOGGER.info(
                    "Creating new CFG schedule: %s-%s limit=100 tz=%s",
                    start_hhmm,
                    end_hhmm,
                    tz,
                )
                self._raise_schedule_overlap_validation_error(
                    start_time=start_hhmm,
                    end_time=end_hhmm,
                    days=days,
                )
                await self.async_create_battery_schedule(
                    schedule_type="CFG",
                    start_time=start_hhmm,
                    end_time=end_hhmm,
                    limit=100,
                    days=days,
                    timezone=tz,
                    is_enabled=bool(
                        getattr(
                            coord, "_battery_charge_from_grid_schedule_enabled", None
                        )
                    ),
                )
            state._battery_charge_begin_time = next_start
            state._battery_charge_end_time = next_end
            state._battery_cfg_schedule_id = None
            state._battery_settings_cache_until = None
            coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
            await coord.async_request_refresh()
            return

        payload: dict[str, object] = {
            "chargeFromGrid": True,
            "chargeFromGridScheduleEnabled": bool(
                getattr(coord, "_battery_charge_from_grid_schedule_enabled", None)
            ),
            "chargeBeginTime": next_start,
            "chargeEndTime": next_end,
            "acceptedItcDisclaimer": self.battery_itc_disclaimer_value(),
        }
        await self.async_apply_battery_settings(payload)

    async def async_set_cfg_schedule_limit(self, limit: int) -> None:
        coord = self.coordinator
        state = self.battery_state
        self._assert_battery_settings_feature_writable(
            "Charge from grid schedule is unavailable.",
            unavailable_key="charge_from_grid_schedule_unavailable",
        )
        if coord.battery_cfg_control_force_schedule_supported is False:
            self._raise_validation(
                "charge_from_grid_schedule_unavailable",
                message="Charge from grid schedule is unavailable.",
            )
        if not hasattr(coord.client, "update_battery_schedule"):
            self._raise_validation(
                "schedule_api_unavailable",
                message="Schedule API not available on this client version.",
            )
        if coord.battery_cfg_schedule_pending:
            self._raise_validation(
                "schedule_change_pending",
                message="A schedule change is pending Envoy sync. Please wait.",
            )
        if (
            getattr(coord, "_battery_cfg_schedule_id", None) is None
            or getattr(coord, "_battery_cfg_schedule_limit", None) is None
            or getattr(coord, "_battery_charge_begin_time", None) is None
            or getattr(coord, "_battery_charge_end_time", None) is None
        ):
            self._raise_validation(
                "charge_from_grid_schedule_missing",
                message="No existing charge-from-grid schedule is available.",
            )
        await self.async_assert_battery_settings_write_allowed()
        async with state._battery_settings_write_lock:
            state._battery_settings_last_write_mono = time.monotonic()
            current_start, current_end = self.current_charge_from_grid_schedule_window()
            start_hhmm = f"{current_start // 60:02d}:{current_start % 60:02d}"
            end_hhmm = f"{current_end // 60:02d}:{current_end % 60:02d}"
            days = getattr(coord, "_battery_cfg_schedule_days", None) or [
                1,
                2,
                3,
                4,
                5,
                6,
                7,
            ]
            tz = getattr(coord, "_battery_cfg_schedule_timezone", None) or "UTC"
            await self.async_update_battery_schedule(
                getattr(coord, "_battery_cfg_schedule_id", None),
                start_time=start_hhmm,
                end_time=end_hhmm,
                limit=limit,
                days=days,
                timezone=tz,
            )
        state._battery_cfg_schedule_limit = limit
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    def _schedule_supported_property_name(self, schedule_type: str) -> str:
        names = {
            "cfg": "charge_from_grid_schedule_supported",
            "dtg": "discharge_to_grid_schedule_supported",
            "rbd": "restrict_battery_discharge_schedule_supported",
        }
        return names[str(schedule_type).lower()]

    def _schedule_pending_property_name(self, schedule_type: str) -> str:
        names = {
            "cfg": "battery_cfg_schedule_pending",
            "dtg": "battery_dtg_schedule_pending",
            "rbd": "battery_rbd_schedule_pending",
        }
        return names[str(schedule_type).lower()]

    def _schedule_control_enabled_value(self, schedule_type: str) -> bool | None:
        normalized = str(schedule_type).lower()
        coord = self.coordinator
        if normalized == "dtg":
            return coord.battery_dtg_control_enabled
        if normalized == "rbd":
            return coord.battery_rbd_control_enabled
        return getattr(coord, self._battery_schedule_enabled_attr(schedule_type), None)

    def _schedule_family_toggle_validation_details(
        self,
        schedule_type: str,
        *,
        enabled: bool,
        schedule_id: object | None,
        current_start: int | None,
        current_end: int | None,
    ) -> tuple[str, dict[str, object], str]:
        normalized = str(schedule_type).lower()
        coord = self.coordinator
        placeholders = self._schedule_label_placeholders(schedule_type)

        if normalized == "rbd" and enabled:
            if schedule_id is None or current_start is None or current_end is None:
                return (
                    "schedule_toggle_create_before_enable",
                    {"schedule_label": "restrict battery discharge schedule"},
                    "Create a restrict battery discharge schedule in the IQ Battery "
                    "scheduler before enabling it.",
                )
            if coord.battery_rbd_control is None:
                return (
                    "schedule_not_exposed",
                    {"schedule_label": "Restrict battery discharge"},
                    "Restrict battery discharge is not currently exposed by Enphase "
                    "for this site. Wait for the schedule sync to complete, then "
                    "refresh and try again.",
                )

        if normalized == "dtg" and enabled and coord.battery_dtg_control is None:
            return (
                "schedule_not_exposed",
                {"schedule_label": "Discharge to grid"},
                "Discharge to grid is not currently exposed by Enphase for this "
                "site. Wait for the schedule sync to complete, then refresh and "
                "try again.",
            )

        return (
            "schedule_toggle_not_applied",
            placeholders,
            (f"{placeholders['schedule_label']} toggle was not applied by " "Enphase."),
        )

    def _schedule_family_toggle_validation_error(
        self,
        schedule_type: str,
        *,
        enabled: bool,
        schedule_id: object | None,
        current_start: int | None,
        current_end: int | None,
    ) -> str:
        """Backward-compatible message helper used by tests."""

        _key, _placeholders, message = self._schedule_family_toggle_validation_details(
            schedule_type,
            enabled=enabled,
            schedule_id=schedule_id,
            current_start=current_start,
            current_end=current_end,
        )
        return message

    def _schedule_family_toggle_effective_state(
        self, schedule_type: str
    ) -> bool | None:
        normalized = str(schedule_type).lower()
        state = self.battery_state
        schedule_enabled = getattr(
            state,
            self._battery_schedule_enabled_attr(schedule_type),
            None,
        )
        schedule_id = getattr(
            state, self._battery_schedule_id_attr(schedule_type), None
        )
        effective_enabled = self._schedule_control_enabled_value(schedule_type)
        if effective_enabled is False or schedule_enabled is False:
            return False
        if effective_enabled is not None:
            return effective_enabled
        toggle_target = getattr(
            state,
            self._schedule_toggle_target_attr(schedule_type),
            None,
        )
        if normalized in {"dtg", "rbd"} and toggle_target is not None:
            return toggle_target

        if normalized in {"dtg", "rbd"} and schedule_id is not None:
            if schedule_enabled is not None:
                return schedule_enabled

        if schedule_enabled is not None:
            return schedule_enabled

        if schedule_id is not None:
            return None

        schedule_status = getattr(
            state, self._battery_schedule_status_attr(schedule_type), None
        )
        if (
            normalized in {"dtg", "rbd"}
            and isinstance(schedule_status, str)
            and schedule_status.strip()
        ):
            return False

        return None

    async def _async_verify_schedule_family_toggle_applied(
        self,
        schedule_type: str,
        *,
        enabled: bool,
    ) -> None:
        normalized = str(schedule_type).lower()
        state = self.battery_state
        attempts = 4 if normalized in {"dtg", "rbd"} else 1
        effective_enabled: bool | None = None

        for attempt in range(attempts):
            await self.async_refresh_battery_settings(force=True)
            if normalized in {"dtg", "rbd"}:
                await self.async_refresh_battery_schedules(force=True)

            effective_enabled = self._schedule_family_toggle_effective_state(
                schedule_type
            )
            control_enabled = self._schedule_control_enabled_value(schedule_type)
            schedule_enabled = getattr(
                state, self._battery_schedule_enabled_attr(schedule_type), None
            )
            if enabled:
                if schedule_enabled is True or control_enabled is True:
                    return
            elif schedule_enabled is False or control_enabled is False:
                return

            if attempt + 1 < attempts:
                await asyncio.sleep(0.75)

        state = self.battery_state
        if normalized in {"dtg", "rbd"}:
            _LOGGER.debug(
                "Schedule family toggle verification mismatch for %s on site %s "
                "(requested=%s effective=%s schedule_enabled=%s control=%s "
                "schedule_id=%s schedule_status=%s)",
                normalized,
                redact_site_id(self.coordinator.site_id),
                enabled,
                effective_enabled,
                getattr(
                    state, self._battery_schedule_enabled_attr(schedule_type), None
                ),
                (
                    self.coordinator.battery_dtg_control
                    if normalized == "dtg"
                    else self.coordinator.battery_rbd_control
                ),
                getattr(state, self._battery_schedule_id_attr(schedule_type), None),
                getattr(state, self._battery_schedule_status_attr(schedule_type), None),
            )

        key, placeholders, message = self._schedule_family_toggle_validation_details(
            schedule_type,
            enabled=enabled,
            schedule_id=getattr(
                self.battery_state,
                self._battery_schedule_id_attr(schedule_type),
                None,
            ),
            current_start=getattr(
                self.battery_state,
                self._battery_schedule_start_attr(schedule_type),
                None,
            ),
            current_end=getattr(
                self.battery_state,
                self._battery_schedule_end_attr(schedule_type),
                None,
            ),
        )
        self._raise_validation(key, placeholders=placeholders, message=message)

    def _schedule_family_control_payload(
        self,
        schedule_type: str,
        *,
        enabled: bool,
        current_start: int | None,
        current_end: int | None,
    ) -> dict[str, object]:
        normalized = str(schedule_type).lower()
        coord = self.coordinator

        payload: dict[str, object] = {"enabled": bool(enabled)}
        control: dict[str, object] | None = None
        if normalized == "dtg":
            raw = coord.battery_dtg_control
            if isinstance(raw, dict):
                control = raw
        elif normalized == "rbd":
            raw = coord.battery_rbd_control
            if isinstance(raw, dict):
                control = raw

        if isinstance(control, dict):
            field_map = {
                "show": "show",
                "locked": "locked",
                "show_day_schedule": "showDaySchedule",
                "schedule_supported": "scheduleSupported",
                "force_schedule_supported": "forceScheduleSupported",
                "force_schedule_opted": "forceScheduleOpted",
            }
            for source_key, target_key in field_map.items():
                value = control.get(source_key)
                if value is not None:
                    payload[target_key] = value

        if (
            normalized in {"dtg", "rbd"}
            and current_start is not None
            and current_end is not None
        ):
            if current_start == current_end:
                placeholders = self._schedule_label_placeholders(schedule_type)
                self._raise_validation(
                    "schedule_family_times_different",
                    placeholders=placeholders,
                    message=(
                        f"{placeholders['schedule_label']} start and end times must "
                        "be different."
                    ),
                )
            payload["scheduleSupported"] = True
            payload["startTime"] = current_start
            payload["endTime"] = current_end

        if normalized == "dtg" and enabled:
            if current_start is None or current_end is None:
                self._raise_validation(
                    "discharge_to_grid_schedule_time_invalid",
                    message="Discharge to grid schedule time is invalid.",
                )

        return payload

    def _current_battery_schedule_window_for_type(
        self, schedule_type: str
    ) -> tuple[int | None, int | None]:
        if str(schedule_type).lower() == "cfg":
            start, end = self.current_charge_from_grid_schedule_window()
            return start, end
        return self.current_battery_schedule_or_control_window(schedule_type)

    def _schedule_create_supported(self) -> bool:
        coord = self.coordinator
        return bool(
            hasattr(coord.client, "create_battery_schedule")
            and getattr(coord, "_battery_schedules_payload", None) is not None
        )

    def _schedule_default_limit_for_create(self, schedule_type: str) -> int | None:
        normalized = str(schedule_type).lower()
        if normalized == "cfg":
            return 100
        if normalized == "dtg":
            shutdown_floor = self.coordinator.battery_shutdown_level
            return max(5, int(shutdown_floor)) if shutdown_floor is not None else 5
        return None

    def _schedule_default_window_for_create(
        self, schedule_type: str
    ) -> tuple[int, int] | None:
        normalized = str(schedule_type).lower()
        if normalized == "rbd":
            return (60, 960)
        return None

    def _schedule_family_days(self, schedule_type: str) -> list[int]:
        state = self.battery_state
        days = getattr(state, self._battery_schedule_days_attr(schedule_type), None)
        return list(days) if isinstance(days, list) and days else [1, 2, 3, 4, 5, 6, 7]

    def _schedule_family_timezone(self, schedule_type: str) -> str:
        state = self.battery_state
        timezone = getattr(
            state, self._battery_schedule_timezone_attr(schedule_type), None
        )
        if isinstance(timezone, str) and timezone.strip():
            return timezone.strip()
        site_timezone = getattr(state, "_battery_timezone", None)
        if isinstance(site_timezone, str) and site_timezone.strip():
            return site_timezone.strip()
        return "UTC"

    async def _async_create_or_update_schedule_family(
        self,
        schedule_type: str,
        *,
        start_minutes: int,
        end_minutes: int,
        limit: int | None,
        is_enabled: bool | None,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        schedule_id = getattr(
            state, self._battery_schedule_id_attr(schedule_type), None
        )
        days = self._schedule_family_days(schedule_type)
        timezone = self._schedule_family_timezone(schedule_type)
        start_time = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
        end_time = f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
        if schedule_id is not None and hasattr(coord.client, "update_battery_schedule"):
            await self.async_update_battery_schedule(
                schedule_id,
                schedule_type=str(schedule_type).upper(),
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                days=days,
                timezone=timezone,
            )
            return
        if not self._schedule_create_supported():
            placeholders = self._schedule_label_placeholders(schedule_type)
            self._raise_validation(
                "schedule_missing",
                placeholders=placeholders,
                message=(
                    f"No existing {placeholders['schedule_label_lower']} schedule is "
                    "available."
                ),
            )
        create_limit = limit
        if create_limit is None:
            create_limit = self._schedule_default_limit_for_create(schedule_type)
        self._raise_schedule_overlap_validation_error(
            start_time=start_time,
            end_time=end_time,
            days=days,
        )
        await self.async_create_battery_schedule(
            schedule_type=str(schedule_type).upper(),
            start_time=start_time,
            end_time=end_time,
            limit=create_limit,
            days=days,
            timezone=timezone,
            is_enabled=is_enabled,
        )

    async def _async_set_schedule_family_enabled(
        self, schedule_type: str, enabled: bool
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        label = self._battery_schedule_label(schedule_type)
        normalized_schedule_type = str(schedule_type).lower()
        self._assert_battery_settings_feature_writable(
            f"{label} schedule is unavailable.",
            unavailable_key="schedule_unavailable",
        )
        if not getattr(coord, self._schedule_supported_property_name(schedule_type)):
            self._raise_validation(
                "schedule_unavailable",
                placeholders=self._schedule_label_placeholders(schedule_type),
                message=f"{label} schedule is unavailable.",
            )
        await self.async_assert_battery_settings_write_allowed()
        if normalized_schedule_type == "cfg":
            if getattr(coord, self._schedule_pending_property_name(schedule_type)):
                self._raise_validation(
                    "schedule_change_pending",
                    message="A schedule change is pending Envoy sync. Please wait.",
                )
            current_start, current_end = self._current_battery_schedule_window_for_type(
                schedule_type,
            )
            if current_start == current_end:
                self._raise_validation(
                    "schedule_family_times_different",
                    placeholders=self._schedule_label_placeholders(schedule_type),
                    message=f"{label} schedule start and end times must be different.",
                )
            await self.async_set_charge_from_grid_schedule_enabled(enabled)
            return
        schedule_id = getattr(
            state, self._battery_schedule_id_attr(schedule_type), None
        )
        use_battery_settings_toggle = normalized_schedule_type in {
            "dtg",
            "rbd",
        }
        if use_battery_settings_toggle:
            self._set_schedule_family_toggle_target(schedule_type, enabled)
        try:
            current_start, current_end = self._current_battery_schedule_window_for_type(
                schedule_type,
            )
            if normalized_schedule_type == "rbd" and enabled:
                if schedule_id is None or current_start is None or current_end is None:
                    self._raise_validation(
                        "schedule_toggle_create_before_enable",
                        placeholders={
                            "schedule_label": "restrict battery discharge schedule"
                        },
                        message=(
                            "Create a restrict battery discharge schedule in the IQ "
                            "Battery scheduler before enabling it."
                        ),
                    )
                if coord.battery_rbd_control is None:
                    self._raise_validation(
                        "schedule_not_exposed",
                        placeholders={"schedule_label": "Restrict battery discharge"},
                        message=(
                            "Restrict battery discharge is not currently exposed by "
                            "Enphase for this site. Wait for the schedule sync to "
                            "complete, then refresh and try again."
                        ),
                    )
            if use_battery_settings_toggle:
                control_key = f"{normalized_schedule_type}Control"
                control_payload = self._schedule_family_control_payload(
                    schedule_type,
                    enabled=enabled,
                    current_start=current_start,
                    current_end=current_end,
                )
                payload = {control_key: control_payload}
                primary_write_rejected = False
                async with state._battery_settings_write_lock:
                    state._battery_settings_last_write_mono = time.monotonic()
                    try:
                        await coord.client.set_battery_settings(
                            payload,
                            schedule_type=normalized_schedule_type,
                        )
                    except aiohttp.ClientResponseError as err:
                        if (
                            normalized_schedule_type in {"dtg", "rbd"}
                            and err.status == HTTPStatus.FORBIDDEN
                        ):
                            primary_write_rejected = True
                        else:
                            self.raise_schedule_update_validation_error(err)
                            raise
                state._battery_settings_cache_until = None
                coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
                try:
                    if primary_write_rejected:
                        self._raise_validation(
                            "schedule_primary_write_rejected",
                            message="primary write rejected",
                        )
                    await self._async_verify_schedule_family_toggle_applied(
                        schedule_type,
                        enabled=enabled,
                    )
                except ServiceValidationError:
                    self.clear_battery_settings_write_pending()
                    await self.async_apply_battery_settings_compat(
                        payload,
                        schedule_type=normalized_schedule_type,
                        include_source=False,
                        merged_payload=True,
                        strip_devices=True,
                    )
                    await self._async_verify_schedule_family_toggle_applied(
                        schedule_type,
                        enabled=enabled,
                    )
                self.clear_battery_settings_write_pending()
                await coord.async_request_refresh()
            setattr(state, self._battery_schedule_enabled_attr(schedule_type), enabled)
        finally:
            if use_battery_settings_toggle:
                self._clear_schedule_family_toggle_target(schedule_type)
        if schedule_id is not None:
            setattr(
                state, self._battery_schedule_start_attr(schedule_type), current_start
            )
            setattr(state, self._battery_schedule_end_attr(schedule_type), current_end)

    async def _async_set_schedule_family_time(
        self,
        schedule_type: str,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        label = self._battery_schedule_label(schedule_type)
        self._assert_battery_settings_feature_writable(
            f"{label} schedule is unavailable.",
            unavailable_key="schedule_unavailable",
        )
        if not getattr(coord, self._schedule_supported_property_name(schedule_type)):
            self._raise_validation(
                "schedule_unavailable",
                placeholders=self._schedule_label_placeholders(schedule_type),
                message=f"{label} schedule is unavailable.",
            )
        if getattr(coord, self._schedule_pending_property_name(schedule_type)):
            self._raise_validation(
                "schedule_change_pending",
                message="A schedule change is pending Envoy sync. Please wait.",
            )

        current_start, current_end = self._current_battery_schedule_window_for_type(
            schedule_type
        )
        next_start = (
            coord.time_to_minutes_of_day(start) if start is not None else current_start
        )
        next_end = coord.time_to_minutes_of_day(end) if end is not None else current_end
        if next_start is None or next_end is None:
            default_window = self._schedule_default_window_for_create(schedule_type)
            if default_window is None:
                self._raise_validation(
                    "schedule_time_invalid",
                    placeholders=self._schedule_label_placeholders(schedule_type),
                    message=f"{label} schedule time is invalid.",
                )
            default_start, default_end = default_window
            if next_start is None:
                next_start = default_start
            if next_end is None:
                next_end = default_end
        if next_start == next_end:
            self._raise_validation(
                "schedule_family_times_different",
                placeholders=self._schedule_label_placeholders(schedule_type),
                message=f"{label} schedule start and end times must be different.",
            )
        current_limit = getattr(
            state, self._battery_schedule_limit_attr(schedule_type), None
        )
        next_limit = (
            int(current_limit)
            if current_limit is not None
            else self._schedule_default_limit_for_create(schedule_type)
        )
        current_enabled = getattr(
            state, self._battery_schedule_enabled_attr(schedule_type), None
        )
        await self.async_assert_battery_settings_write_allowed()
        async with state._battery_settings_write_lock:
            state._battery_settings_last_write_mono = time.monotonic()
            await self._async_create_or_update_schedule_family(
                schedule_type,
                start_minutes=next_start,
                end_minutes=next_end,
                limit=next_limit,
                is_enabled=False if current_enabled is False else None,
            )
        setattr(state, self._battery_schedule_start_attr(schedule_type), next_start)
        setattr(state, self._battery_schedule_end_attr(schedule_type), next_end)
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    async def _async_set_schedule_family_limit(
        self, schedule_type: str, limit: int
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        label = self._battery_schedule_label(schedule_type)
        self._assert_battery_settings_feature_writable(
            f"{label} schedule is unavailable.",
            unavailable_key="schedule_unavailable",
        )
        if not getattr(coord, self._schedule_supported_property_name(schedule_type)):
            self._raise_validation(
                "schedule_unavailable",
                placeholders=self._schedule_label_placeholders(schedule_type),
                message=f"{label} schedule is unavailable.",
            )
        if getattr(coord, self._schedule_pending_property_name(schedule_type)):
            self._raise_validation(
                "schedule_change_pending",
                message="A schedule change is pending Envoy sync. Please wait.",
            )
        current_start, current_end = self._current_battery_schedule_window_for_type(
            schedule_type
        )
        if current_start is None or current_end is None:
            self._raise_validation(
                "current_schedule_time_invalid",
                placeholders=self._schedule_label_placeholders(schedule_type),
                message=f"Current {label.lower()} schedule time is invalid.",
            )
        if not 5 <= int(limit) <= 100:
            self._raise_validation(
                "schedule_limit_range",
                placeholders={
                    **self._schedule_label_placeholders(schedule_type),
                    "minimum": "5",
                    "maximum": "100",
                },
                message=f"{label} schedule limit must be between 5 and 100.",
            )
        shutdown_floor = coord.battery_shutdown_level
        if shutdown_floor is not None and int(limit) < shutdown_floor:
            self._raise_validation(
                "schedule_limit_minimum",
                placeholders={
                    **self._schedule_label_placeholders(schedule_type),
                    "minimum": str(shutdown_floor),
                },
                message=f"{label} schedule limit must be at least {shutdown_floor}%.",
            )
        current_enabled = getattr(
            state, self._battery_schedule_enabled_attr(schedule_type), None
        )
        await self.async_assert_battery_settings_write_allowed()
        async with state._battery_settings_write_lock:
            state._battery_settings_last_write_mono = time.monotonic()
            await self._async_create_or_update_schedule_family(
                schedule_type,
                start_minutes=current_start,
                end_minutes=current_end,
                limit=int(limit),
                is_enabled=False if current_enabled is False else None,
            )
        setattr(state, self._battery_schedule_limit_attr(schedule_type), int(limit))
        setattr(state, self._battery_schedule_start_attr(schedule_type), current_start)
        setattr(state, self._battery_schedule_end_attr(schedule_type), current_end)
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    async def async_set_discharge_to_grid_schedule_enabled(self, enabled: bool) -> None:
        await self._async_set_schedule_family_enabled("dtg", enabled)

    async def async_set_discharge_to_grid_schedule_time(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
    ) -> None:
        await self._async_set_schedule_family_time("dtg", start=start, end=end)

    async def async_set_discharge_to_grid_schedule_limit(self, limit: int) -> None:
        await self._async_set_schedule_family_limit("dtg", limit)

    async def async_set_restrict_battery_discharge_schedule_enabled(
        self, enabled: bool
    ) -> None:
        await self._async_set_schedule_family_enabled("rbd", enabled)

    async def async_set_restrict_battery_discharge_schedule_time(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
    ) -> None:
        await self._async_set_schedule_family_time("rbd", start=start, end=end)

    async def async_set_restrict_battery_discharge_schedule_limit(
        self, limit: int
    ) -> None:
        await self._async_set_schedule_family_limit("rbd", limit)

    async def async_update_cfg_schedule(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
        limit: int | None = None,
    ) -> None:
        coord = self.coordinator
        state = self.battery_state
        self._assert_battery_settings_feature_writable(
            "Charge from grid schedule is unavailable.",
            unavailable_key="charge_from_grid_schedule_unavailable",
        )
        if not hasattr(coord.client, "update_battery_schedule"):
            self._raise_validation(
                "schedule_api_unavailable",
                message="Schedule API not available on this client version.",
            )
        if coord.battery_cfg_schedule_pending:
            self._raise_validation(
                "schedule_change_pending",
                message="A schedule change is pending Envoy sync. Please wait.",
            )
        schedule_id = getattr(state, "_battery_cfg_schedule_id", None)
        if schedule_id is None:
            self._raise_validation(
                "charge_from_grid_schedule_missing",
                message="No existing charge-from-grid schedule is available.",
            )
        current_start, current_end = self._current_schedule_window_from_coordinator()
        if current_start is None or current_end is None:
            self._raise_validation(
                "current_schedule_times_unavailable",
                message="Current schedule times are not available.",
            )
        next_start = (
            coord.time_to_minutes_of_day(start) if start is not None else current_start
        )
        next_end = coord.time_to_minutes_of_day(end) if end is not None else current_end
        next_limit = (
            limit
            if limit is not None
            else getattr(state, "_battery_cfg_schedule_limit", None) or 100
        )
        if next_start is None or next_end is None:
            self._raise_validation(
                "charge_from_grid_schedule_time_invalid",
                message="Charge-from-grid schedule time is invalid.",
            )
        if next_start == next_end:
            self._raise_validation(
                "charge_from_grid_schedule_times_different",
                message=(
                    "Charge-from-grid schedule start and end times must be different."
                ),
            )
        if not 5 <= next_limit <= 100:
            self._raise_validation(
                "charge_from_grid_schedule_limit_range",
                placeholders={"minimum": "5", "maximum": "100"},
                message="Charge-from-grid schedule limit must be between 5 and 100.",
            )
        shutdown_floor = coord.battery_shutdown_level
        if shutdown_floor is not None and next_limit < shutdown_floor:
            self._raise_validation(
                "charge_from_grid_schedule_limit_minimum",
                placeholders={"minimum": str(shutdown_floor)},
                message=(
                    "Charge-from-grid schedule limit must be at least "
                    f"{shutdown_floor}%."
                ),
            )
        await self.async_assert_battery_settings_write_allowed()
        async with state._battery_settings_write_lock:
            state._battery_settings_last_write_mono = time.monotonic()
            start_hhmm = f"{next_start // 60:02d}:{next_start % 60:02d}"
            end_hhmm = f"{next_end // 60:02d}:{next_end % 60:02d}"
            days = getattr(state, "_battery_cfg_schedule_days", None) or [
                1,
                2,
                3,
                4,
                5,
                6,
                7,
            ]
            tz = getattr(state, "_battery_cfg_schedule_timezone", None) or "UTC"
            await self.async_update_battery_schedule(
                schedule_id,
                start_time=start_hhmm,
                end_time=end_hhmm,
                limit=next_limit,
                days=days,
                timezone=tz,
            )
        state._battery_charge_begin_time = next_start
        state._battery_charge_end_time = next_end
        state._battery_cfg_schedule_limit = next_limit
        state._battery_settings_cache_until = None
        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()

    def raise_grid_validation(
        self,
        key: str,
        *,
        placeholders: dict[str, object] | None = None,
        message: str | None = None,
    ) -> None:
        coord = self.coordinator
        func = getattr(coord, "raise_grid_validation", None)
        if callable(func):
            func(
                key,
                placeholders=placeholders,
                message=message,
            )
            return
        func = getattr(coord, "_raise_grid_validation", None)
        if callable(func):
            func(
                key,
                placeholders=placeholders,
                message=message,
            )
            return
        raise_translated_service_validation(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders=placeholders,
            message=message,
        )

    def grid_envoy_serial(self) -> str | None:
        bucket = self.coordinator.inventory_view.type_bucket("envoy")
        if not isinstance(bucket, dict):
            return None
        devices = bucket.get("devices")
        if not isinstance(devices, list):
            return None
        for device in devices:
            if not isinstance(device, dict):
                continue
            serial = self.coordinator.coerce_optional_text(device.get("serial_number"))
            if serial:
                return serial
        return None

    async def async_assert_grid_toggle_allowed(self) -> None:
        coord = self.coordinator
        await self.async_refresh_grid_control_check(force=True)
        if coord.grid_control_supported is not True:
            self.raise_grid_validation("grid_control_unavailable")
        if coord.grid_toggle_allowed is True:
            return
        reasons = coord.grid_toggle_blocked_reasons
        reasons_text = ", ".join(reasons) if reasons else "unknown"
        self.raise_grid_validation(
            "grid_control_blocked",
            placeholders={"reasons": reasons_text},
        )

    async def async_request_grid_toggle_otp(self) -> None:
        coord = self.coordinator
        await self.async_assert_grid_toggle_allowed()
        requester = getattr(coord.client, "request_grid_toggle_otp", None)
        if not callable(requester):
            self.raise_grid_validation("grid_control_unavailable")
        try:
            await requester()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Grid toggle OTP request failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
            )
            self.raise_grid_validation("grid_control_unavailable")

    async def async_set_grid_mode(self, mode: str, otp: str) -> None:
        coord = self.coordinator
        try:
            normalized_mode = str(mode).strip().lower()
        except Exception:
            normalized_mode = ""
        if normalized_mode not in {"on_grid", "off_grid"}:
            self.raise_grid_validation("grid_mode_invalid")

        otp_text = str(otp).strip() if otp is not None else ""
        if not otp_text:
            self.raise_grid_validation("grid_otp_required")
        if len(otp_text) != 4 or not otp_text.isdigit():
            self.raise_grid_validation("grid_otp_invalid_format")

        await self.async_assert_grid_toggle_allowed()

        validator = getattr(coord.client, "validate_grid_toggle_otp", None)
        if not callable(validator):
            self.raise_grid_validation("grid_control_unavailable")
        try:
            valid = await validator(otp_text)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Grid toggle OTP validation call failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
            )
            self.raise_grid_validation("grid_control_unavailable")
        if valid is not True:
            self.raise_grid_validation("grid_otp_invalid")

        envoy_serial = self.grid_envoy_serial()
        if envoy_serial is None:
            self.raise_grid_validation("grid_envoy_serial_missing")

        grid_state = 2 if normalized_mode == "on_grid" else 1
        setter = getattr(coord.client, "set_grid_state", None)
        if not callable(setter):
            self.raise_grid_validation("grid_control_unavailable")
        try:
            await setter(envoy_serial, grid_state)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Grid mode set request failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(
                    err,
                    site_ids=(coord.site_id,),
                    identifiers=(envoy_serial,),
                ),
            )
            self.raise_grid_validation("grid_control_unavailable")

        old_state = (
            "OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD"
            if normalized_mode == "on_grid"
            else "OPER_RELAY_CLOSED"
        )
        new_state = (
            "OPER_RELAY_CLOSED"
            if normalized_mode == "on_grid"
            else "OPER_RELAY_OFFGRID_AC_GRID_PRESENT"
        )
        logger = getattr(coord.client, "log_grid_change", None)
        if callable(logger):
            try:
                await logger(envoy_serial, old_state, new_state)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Grid toggle audit log failed for site %s: %s",
                    redact_site_id(coord.site_id),
                    redact_text(
                        err,
                        site_ids=(coord.site_id,),
                        identifiers=(envoy_serial,),
                    ),
                )

        coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
        await coord.async_request_refresh()
        await self.async_refresh_grid_control_check(force=True)

    async def async_set_grid_connection(
        self, enabled: bool, *, otp: str | None = None
    ) -> None:
        if not otp:
            self.raise_grid_validation("grid_otp_required")
        mode = "on_grid" if bool(enabled) else "off_grid"
        await self.async_set_grid_mode(mode, otp)

    async def async_set_battery_shutdown_level(self, level: int) -> None:
        coord = self.coordinator
        self._assert_battery_settings_feature_writable(
            "Battery shutdown level is unavailable.",
            unavailable_key="battery_shutdown_level_unavailable",
        )
        if not coord.battery_shutdown_level_available:
            self._raise_validation(
                "battery_shutdown_level_unavailable",
                message="Battery shutdown level is unavailable.",
            )
        try:
            normalized = int(level)
        except Exception:  # noqa: BLE001
            self._raise_validation(
                "battery_shutdown_level_invalid",
                message="Battery shutdown level is invalid.",
            )
        min_level = coord.battery_shutdown_level_min
        max_level = coord.battery_shutdown_level_max
        if normalized < min_level or normalized > max_level:
            self._raise_validation(
                "battery_shutdown_level_range",
                placeholders={
                    "minimum": str(min_level),
                    "maximum": str(max_level),
                },
                message=(
                    f"Battery shutdown level must be between {min_level} and "
                    f"{max_level}."
                ),
            )
        await self.async_apply_battery_settings({"veryLowSoc": normalized})

    async def async_opt_out_all_storm_alerts(self) -> None:
        coord = self.coordinator
        await coord.async_refresh_storm_alert(force=True)

        actionable: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for alert in coord.storm_alerts:
            if not isinstance(alert, dict):
                continue
            alert_id = coord.coerce_optional_text(alert.get("id"))
            if not alert_id or alert_id in seen_ids:
                continue
            if not self.storm_alert_is_active(alert):
                continue
            name = (
                coord.coerce_optional_text(alert.get("name")) or "Storm Alert"
            )  # noqa: SLF001
            actionable.append((alert_id, name))
            seen_ids.add(alert_id)

        if not actionable:
            return

        opt_out = getattr(coord.client, "opt_out_storm_alert", None)
        if not callable(opt_out):
            self._raise_validation(
                "storm_alert_opt_out_unavailable",
                message="Storm Alert opt-out is unavailable.",
            )

        failures: list[tuple[str, Exception]] = []
        for alert_id, name in actionable:
            try:
                await opt_out(alert_id=alert_id, name=name)
            except Exception as err:  # noqa: BLE001
                failures.append((alert_id, err))
                _LOGGER.warning(
                    "Storm Alert opt-out failed for site %s alert %s: %s",
                    redact_site_id(coord.site_id),
                    redact_identifier(alert_id),
                    redact_text(
                        err,
                        site_ids=(coord.site_id,),
                        identifiers=(alert_id,),
                    ),
                )

        refresh_err: Exception | None = None
        self.battery_state._storm_alert_cache_until = None
        try:
            await coord.async_refresh_storm_alert(force=True, raise_on_error=True)
            coord.kick_fast(FAST_TOGGLE_POLL_HOLD_S)
            await coord.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            refresh_err = err
            _LOGGER.warning(
                "Storm Alert opt-out refresh failed for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
            )

        if failures:
            self._raise_validation(
                "storm_alert_opt_out_failed",
                placeholders={"count": str(len(failures))},
                message=f"Storm Alert opt-out failed for {len(failures)} alert(s).",
            )
        if refresh_err is not None:
            raise refresh_err

    async def async_set_storm_guard_enabled(self, enabled: bool) -> None:
        coord = self.coordinator
        await self.async_ensure_battery_write_access_confirmed(
            denied_message="Storm Guard updates are not permitted for this account.",
            denied_key="storm_guard_update_not_permitted",
        )
        await coord.async_refresh_storm_guard_profile(force=True)
        if getattr(coord, "_storm_evse_enabled", None) is None:
            self._raise_validation(
                "storm_guard_settings_unavailable",
                message="Storm Guard settings are unavailable.",
            )
        target_state = "enabled" if enabled else "disabled"
        self.set_storm_guard_pending(target_state)
        try:
            await coord.client.set_storm_guard(
                enabled=bool(enabled),
                evse_enabled=bool(getattr(coord, "_storm_evse_enabled", None)),
            )
        except aiohttp.ClientResponseError as err:
            self.clear_storm_guard_pending()
            if err.status == HTTPStatus.FORBIDDEN:
                owner = coord.battery_user_is_owner
                installer = coord.battery_user_is_installer
                if owner is False and installer is False:
                    self._raise_validation(
                        "storm_guard_update_not_permitted",
                        message=(
                            "Storm Guard updates are not permitted for this account."
                        ),
                    )
                self._raise_validation(
                    "storm_guard_update_forbidden",
                    message=(
                        "Storm Guard update was rejected by Enphase "
                        "(HTTP 403 Forbidden)."
                    ),
                )
            if err.status == HTTPStatus.UNAUTHORIZED:
                self._raise_validation(
                    "storm_guard_update_unauthorized",
                    message=(
                        "Storm Guard update could not be authenticated. "
                        "Reauthenticate and try again."
                    ),
                )
            raise
        except Exception:
            self.clear_storm_guard_pending()
            raise
        self.battery_state._storm_guard_cache_until = None
        self.sync_storm_guard_pending(getattr(coord, "_storm_guard_state", None))

    async def async_set_storm_evse_enabled(self, enabled: bool) -> None:
        coord = self.coordinator
        await self.async_ensure_battery_write_access_confirmed(
            denied_message="Storm Guard updates are not permitted for this account.",
            denied_key="storm_guard_update_not_permitted",
        )
        await coord.async_refresh_storm_guard_profile(force=True)
        if getattr(coord, "_storm_guard_state", None) is None:
            self._raise_validation(
                "storm_guard_settings_unavailable",
                message="Storm Guard settings are unavailable.",
            )
        try:
            await coord.client.set_storm_guard(
                enabled=getattr(coord, "_storm_guard_state", None) == "enabled",
                evse_enabled=bool(enabled),
            )
        except aiohttp.ClientResponseError as err:
            if err.status == HTTPStatus.FORBIDDEN:
                owner = coord.battery_user_is_owner
                installer = coord.battery_user_is_installer
                if owner is False and installer is False:
                    self._raise_validation(
                        "storm_guard_update_not_permitted",
                        message=(
                            "Storm Guard updates are not permitted for this account."
                        ),
                    )
                self._raise_validation(
                    "storm_guard_update_forbidden",
                    message=(
                        "Storm Guard update was rejected by Enphase "
                        "(HTTP 403 Forbidden)."
                    ),
                )
            if err.status == HTTPStatus.UNAUTHORIZED:
                self._raise_validation(
                    "storm_guard_update_unauthorized",
                    message=(
                        "Storm Guard update could not be authenticated. "
                        "Reauthenticate and try again."
                    ),
                )
            raise
        self.battery_state._storm_evse_enabled = bool(enabled)
        self.battery_state._storm_guard_cache_until = (
            time.monotonic() + STORM_GUARD_CACHE_TTL
        )
