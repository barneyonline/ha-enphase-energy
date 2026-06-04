"""Coordinate Enphase cloud refreshes and expose normalized integration state.

The coordinator owns polling cadence, auth refresh, endpoint health, runtime
managers, and diagnostics state for one Home Assistant config entry. Entity
platforms should read normalized state from here instead of calling the cloud
client directly.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from datetime import timezone as _tz
from http import HTTPStatus
from numbers import Real
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

import aiohttp
from email.utils import parsedate_to_datetime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ServiceValidationError,
)  # noqa: F401
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AuthTokens,
    EnphaseLoginWallUnauthorized,
    EnphaseEVClient,
    InvalidPayloadError,
    OptionalEndpointUnavailable,
    Unauthorized,
    is_scheduler_unavailable_error,
)
from .auth_refresh_runtime import AuthRefreshRuntime, ManualAuthRefreshResult
from .const import (
    AUTH_BLOCKED_COOLDOWN_S,
    BATTERY_MIN_SOC_FALLBACK,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_BLOCK_REASON,
    CONF_AUTH_BLOCKED_UNTIL,
    CONF_AUTH_REFRESH_SUSPENDED_UNTIL,
    CONF_COOKIE,
    DEFAULT_CHARGE_LEVEL_SETTING,
    CONF_EAUTH,
    CONF_EMAIL,
    CONF_HEMS_AUTH_BACKOFF_UNTIL,
    CONF_HEMS_AUTH_FAILURE_COUNT,
    CONF_HEMS_AUTH_LAST_ENDPOINT,
    CONF_HEMS_AUTH_LAST_FAILURE_UTC,
    CONF_HEMS_AUTH_LAST_REASON,
    CONF_HEMS_AUTH_LAST_STATUS,
    CONF_HEMS_AUTH_LAST_SUCCESS_UTC,
    CONF_INCLUDE_INVERTERS,
    CONF_PASSWORD,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SERIALS,
    CONF_SESSION_ID,
    CONF_SITE_ID,
    CONF_SITE_ONLY,
    CONF_SITE_NAME,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_API_TIMEOUT,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MAX_API_TIMEOUT,
    MAX_POLL_INTERVAL,
    MAX_SESSION_HISTORY_INTERVAL_MIN,
    MIN_API_TIMEOUT,
    MIN_FAST_POLL_INTERVAL,
    MIN_SLOW_POLL_INTERVAL,
    MIN_SESSION_HISTORY_INTERVAL_MIN,
    DRY_CONTACT_SETTINGS_STALE_AFTER_S,
    DOMAIN,
    GRID_CONTROL_CHECK_STALE_AFTER_S,
    HEMS_AUTH_BACKOFF_STEPS_S,
    HEMS_AUTH_MANUAL_CLEAR_COOLDOWN_S,
    OPT_API_TIMEOUT,
    OPT_FAST_POLL_INTERVAL,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
    OPT_SESSION_HISTORY_INTERVAL,
    PHASE_SWITCH_CONFIG_SETTING,
    SAVINGS_OPERATION_MODE_SUBTYPE,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
)
from .battery_runtime import BatteryRuntime
from .coordinator_diagnostics import CoordinatorDiagnostics
from .current_power_runtime import CurrentPowerRuntime
from .discovery_snapshot import DiscoverySnapshotManager
from .device_types import (
    normalize_type_key,
    parse_type_identifier,
)
from .energy import EnergyManager
from .evse_timeseries import EVSETimeseriesManager
from .evse_feature_flags_runtime import EvseFeatureFlagsRuntime
from .evse_runtime import (
    AMP_RESTART_DELAY_S,
    FAST_TOGGLE_POLL_HOLD_S,
    SUSPENDED_EVSE_STATUS,
    ChargeModeStartPreferences,
    EvseRuntime,
    evse_power_is_actively_charging,
)
from .evse_power import build_evse_power_snapshot
from .heatpump_runtime import HeatpumpRuntime
from .inventory_runtime import CoordinatorTopologySnapshot, InventoryRuntime
from .inventory_view import InventoryView
from .labels import (
    battery_grid_mode_label,
    battery_profile_label as translated_battery_profile_label,
)
from .log_redaction import (
    redact_site_id,
    redact_text,
    truncate_identifier,
)
from . import payload_debug
from .parsing_helpers import (
    coerce_optional_bool,
    coerce_optional_float,
    coerce_optional_text,
    parse_inverter_last_report,
)
from .runtime_helpers import (
    coerce_int as helper_coerce_int,
    coerce_optional_int as helper_coerce_optional_int,
    copy_diagnostics_value,
    normalize_poll_intervals,
    normalize_iso_date,
    redact_battery_payload,
    resolve_inverter_start_date,
    resolve_site_local_current_date,
    resolve_site_timezone_name,
)
from .session_history import (
    MIN_SESSION_HISTORY_CACHE_TTL,
    SESSION_HISTORY_CACHE_DAY_RETENTION,
    SESSION_HISTORY_CONCURRENCY,
    SESSION_HISTORY_FAILURE_BACKOFF_S,
    SessionHistoryManager,
)
from .coordinator_refresh_metrics import record_refresh_performance_sample
from .summary import SummaryStore
from . import system_dashboard_helpers as sd_helpers
from .refresh_plan import (
    build_followup_plan,
    build_heatpump_followup_plan,
    build_post_session_followup_plan,
    build_site_only_followup_plan,
)
from .refresh_runner import RefreshRunner
from .tariff import TARIFF_SUCCESS_TTL_S, TariffRuntime
from .service_validation import raise_translated_service_validation
from .state_models import (
    BatteryControlCapability,
    BatteryState,
    DiscoveryState,
    EndpointFamilyHealth,
    EVSEState,
    HeatpumpState,
    InventoryState,
    RefreshHealthState,
    install_state_descriptors,
)
from .voltage import (
    coerce_nominal_voltage,
    preferred_operating_voltage,
    resolve_nominal_voltage_for_hass,
)

if TYPE_CHECKING:
    from .runtime_data import EnphaseConfigEntry

_LOGGER = logging.getLogger(__name__)
# Session history can arrive after real-time charging state changes. These
# windows keep recent context available without letting old sessions override
# live charger telemetry.
SESSION_HISTORY_ACTIVE_SOFT_TTL_S = 120.0
SESSION_HISTORY_RECENT_STOP_SOFT_TTL_S = 300.0
SESSION_HISTORY_IDLE_HARD_TTL_GRACE_S = 300.0
SESSION_HISTORY_RECENT_STOP_WINDOW_S = 600.0
BATTERY_GRID_MODE_PERMISSIONS = {
    "importexport": (True, True),
    "importonly": (True, False),
    "exportonly": (False, True),
}
BATTERY_STATUS_SEVERITY = {
    "normal": 0,
    "unknown": 1,
    "warning": 2,
    "error": 3,
}

ACTIVE_CONNECTOR_STATUSES = {"CHARGING", "FINISHING", "SUSPENDED"}
ACTIVE_SUSPENDED_PREFIXES = ("SUSPENDED_EV",)
_SERVICE_VALIDATION_ERROR_COMPAT = ServiceValidationError

COORDINATOR_RUNTIME_CLASSES: dict[str, type] = {
    "current_power_runtime": CurrentPowerRuntime,
    "auth_refresh_runtime": AuthRefreshRuntime,
    "evse_feature_flags_runtime": EvseFeatureFlagsRuntime,
    "tariff_runtime": TariffRuntime,
}


def _coerce_epoch_seconds(value: object) -> int | None:
    try:
        timestamp = int(value)
    except Exception:
        return None
    if timestamp > 10**12:
        timestamp = timestamp // 1000
    return timestamp


def _charger_sample_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=_tz.utc)
        return value.astimezone(_tz.utc)
    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric > 10**12:
            numeric = numeric / 1000.0
        if numeric <= 0:
            return None
        try:
            return datetime.fromtimestamp(numeric, tz=_tz.utc)
        except Exception:
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _charger_sample_datetime(int(text))
        parsed = dt_util.parse_datetime(text)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_tz.utc)
        return parsed.astimezone(_tz.utc)
    return None


def _extract_error_description(raw: str | None) -> str | None:
    """Best-effort extraction of a descriptive message from error payloads."""

    if not raw:
        return None
    text = str(raw).strip()

    def _search(obj):
        if isinstance(obj, dict):
            for key in (
                "description",
                "code_description",
                "codeDescription",
                "displayMessage",
                "message",
                "detail",
                "error_description",
                "errorDescription",
                "errorMessage",
            ):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            for key in ("error", "details", "data"):
                nested = obj.get(key)
                result = _search(nested)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = _search(item)
                if result:
                    return result
        elif isinstance(obj, str) and obj.strip():
            return obj.strip()
        return None

    candidates = [text]
    trimmed = text.strip("\"'")
    if trimmed != text:
        candidates.append(trimmed)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        description = _search(parsed)
        if description:
            return description
    return None


def _coerce_boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    return False


def _coerce_optional_boolish(value: object) -> bool | None:
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
        if normalized in (
            "false",
            "0",
            "no",
            "n",
            "disabled",
            "disable",
            "off",
        ):
            return False
    return None


def _coerce_floatish(value: object, *, precision: int | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except Exception:
            return None
    else:
        return None
    if precision is not None:
        try:
            return round(parsed, precision)
        except Exception:
            return parsed
    return parsed


def _coerce_intish(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except Exception:
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except Exception:
            return None
    return None


def _coerce_textish(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _coerce_int_list(value: object) -> list[int] | None:
    if not isinstance(value, list):
        return None
    parsed: list[int] = []
    for item in value:
        coerced = _coerce_intish(item)
        if coerced is not None:
            parsed.append(coerced)
    return parsed or None


def _power_as_float(raw: object) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class ChargerState:
    sn: str
    name: str | None
    connected: bool
    plugged: bool
    charging: bool
    faulted: bool
    connector_status: str | None
    session_kwh: float | None
    session_start: int | None


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


@dataclass(slots=True)
class RefreshPipelineContext:
    """Mutable state shared by one coordinator refresh pipeline."""

    started_mono: float
    refresh_started_utc: datetime
    phase_timings: dict[str, float]
    fallback_data: dict[str, dict]
    first_refresh: bool
    first_refresh_followups_task: asyncio.Task[None] | None = None
    auth_refresh_rejected_count_at_start: int = 0
    status_used_stale: bool = False
    status_stale_network_failure: bool = False
    status_stale_dns_failure: bool = False
    fast_poll: bool = False


class EnphaseCoordinator(DataUpdateCoordinator[dict]):
    def __init__(
        self,
        hass: HomeAssistant,
        config: Mapping[str, Any],
        config_entry: EnphaseConfigEntry | None = None,
    ) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self.site_id = str(config[CONF_SITE_ID])
        raw_serials = config.get(CONF_SERIALS) or []
        self.serials: set[str] = set()
        self._serial_order: list[str] = []
        if isinstance(raw_serials, (list, tuple, set)):
            normalized_serials: list[str] = []
            for sn in raw_serials:
                if sn is None:
                    continue
                try:
                    normalized = str(sn).strip()
                except Exception:
                    continue
                if not normalized:
                    continue
                normalized_serials.append(normalized)
            self.serials.update(normalized_serials)
            self._serial_order.extend(list(dict.fromkeys(normalized_serials)))
        else:
            if raw_serials is not None:
                try:
                    normalized = str(raw_serials).strip()
                except Exception:
                    normalized = ""
                if normalized:
                    self.serials = {normalized}
                    self._serial_order.append(normalized)
        self._configured_serials: set[str] = set(self.serials)
        raw_site_only = config.get(CONF_SITE_ONLY, None)
        if raw_site_only is None and config_entry is not None:
            raw_site_only = config_entry.options.get(CONF_SITE_ONLY)
        self.site_only = bool(raw_site_only)
        self.include_inverters = bool(config.get(CONF_INCLUDE_INVERTERS, True))
        raw_selected_type_keys = config.get(CONF_SELECTED_TYPE_KEYS, None)
        self._selected_type_keys: set[str] | None = None
        if raw_selected_type_keys is not None:
            if isinstance(raw_selected_type_keys, (list, tuple, set)):
                iterable = raw_selected_type_keys
                selected_keys: set[str] = set()
                for key in iterable:
                    normalized = normalize_type_key(key)
                    if normalized:
                        selected_keys.add(normalized)
                self._selected_type_keys = selected_keys
            elif isinstance(raw_selected_type_keys, str):
                iterable = [raw_selected_type_keys]
                selected_keys = set()
                for key in iterable:
                    normalized = normalize_type_key(key)
                    if normalized:
                        selected_keys.add(normalized)
                if selected_keys:
                    self._selected_type_keys = selected_keys

        self.site_name = config.get(CONF_SITE_NAME)
        self._email = config.get(CONF_EMAIL)
        self._remember_password = bool(config.get(CONF_REMEMBER_PASSWORD))
        self._stored_password = config.get(CONF_PASSWORD)
        cookie = config.get(CONF_COOKIE, "") or ""
        access_token = config.get(CONF_EAUTH) or config.get(CONF_ACCESS_TOKEN)
        self._tokens = AuthTokens(
            cookie=cookie,
            session_id=config.get(CONF_SESSION_ID),
            access_token=access_token,
            token_expires_at=config.get(CONF_TOKEN_EXPIRES_AT),
        )
        auth_refresh_suspended_until = self._coerce_utc_datetime(
            config.get(CONF_AUTH_REFRESH_SUSPENDED_UNTIL)
        )
        auth_blocked_until = self._coerce_utc_datetime(
            config.get(CONF_AUTH_BLOCKED_UNTIL)
        )
        raw_auth_block_reason = config.get(CONF_AUTH_BLOCK_REASON)
        auth_block_reason = (
            str(raw_auth_block_reason).strip() if raw_auth_block_reason else None
        )
        hems_auth_backoff_until = self._coerce_utc_datetime(
            config.get(CONF_HEMS_AUTH_BACKOFF_UNTIL)
        )
        hems_auth_last_failure_utc = self._coerce_utc_datetime(
            config.get(CONF_HEMS_AUTH_LAST_FAILURE_UTC)
        )
        hems_auth_last_success_utc = self._coerce_utc_datetime(
            config.get(CONF_HEMS_AUTH_LAST_SUCCESS_UTC)
        )
        raw_hems_auth_failure_count = config.get(CONF_HEMS_AUTH_FAILURE_COUNT, 0)
        try:
            hems_auth_failure_count = max(0, int(raw_hems_auth_failure_count or 0))
        except (TypeError, ValueError):
            hems_auth_failure_count = 0
        raw_hems_auth_last_status = config.get(CONF_HEMS_AUTH_LAST_STATUS)
        try:
            hems_auth_last_status = (
                int(raw_hems_auth_last_status)
                if raw_hems_auth_last_status not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            hems_auth_last_status = None
        raw_hems_auth_last_endpoint = config.get(CONF_HEMS_AUTH_LAST_ENDPOINT)
        raw_hems_auth_last_reason = config.get(CONF_HEMS_AUTH_LAST_REASON)
        timeout = DEFAULT_API_TIMEOUT
        if config_entry:
            timeout = helper_coerce_int(
                config_entry.options.get(OPT_API_TIMEOUT, DEFAULT_API_TIMEOUT),
                default=DEFAULT_API_TIMEOUT,
            )
        timeout = min(MAX_API_TIMEOUT, max(MIN_API_TIMEOUT, timeout))
        self.client = EnphaseEVClient(
            async_get_clientsession(hass),
            self.site_id,
            self._tokens.access_token,
            self._tokens.cookie,
            timeout=timeout,
        )
        set_reauth_cb = getattr(self.client, "set_reauth_callback", None)
        if callable(set_reauth_cb):
            result = set_reauth_cb(self._handle_client_unauthorized)
            if inspect.isawaitable(result):
                try:
                    self.hass.async_create_task(
                        result, name=f"{DOMAIN}_set_reauth_callback"
                    )
                except TypeError:
                    self.hass.async_create_task(result)
        from .schedule_sync import ScheduleSync

        self.schedule_sync = ScheduleSync(hass, self, config_entry)
        object.__setattr__(
            self,
            "discovery_state",
            DiscoveryState(
                _topology_snapshot_cache=CoordinatorTopologySnapshot(
                    charger_serials=(),
                    battery_serials=(),
                    ac_battery_serials=(),
                    inverter_serials=(),
                    active_type_keys=(),
                    gateway_iq_router_keys=(),
                    inventory_ready=False,
                )
            ),
        )
        object.__setattr__(self, "refresh_state", RefreshHealthState())
        object.__setattr__(self, "inventory_state", InventoryState())
        object.__setattr__(self, "heatpump_state", HeatpumpState())
        object.__setattr__(self, "evse_state", EVSEState())
        object.__setattr__(
            self,
            "battery_state",
            BatteryState(
                _battery_profile_write_lock=asyncio.Lock(),
                _battery_settings_write_lock=asyncio.Lock(),
            ),
        )
        self._refresh_lock = asyncio.Lock()
        self._auth_refresh_suspended_until_utc = auth_refresh_suspended_until
        self._auth_blocked_until_utc = auth_blocked_until
        self._auth_block_reason = auth_block_reason
        self._hems_auth_failure_count = hems_auth_failure_count
        self._hems_auth_last_failure_utc = hems_auth_last_failure_utc
        self._hems_auth_last_success_utc = hems_auth_last_success_utc
        self._hems_auth_last_endpoint = (
            str(raw_hems_auth_last_endpoint).strip()
            if raw_hems_auth_last_endpoint
            else None
        )
        self._hems_auth_last_status = hems_auth_last_status
        self._hems_auth_last_reason = (
            str(raw_hems_auth_last_reason).strip()
            if raw_hems_auth_last_reason
            else None
        )
        self._hems_auth_backoff_ends_utc = hems_auth_backoff_until
        self._hems_auth_backoff_until = None
        if isinstance(hems_auth_backoff_until, datetime):
            remaining = (hems_auth_backoff_until - dt_util.utcnow()).total_seconds()
            if remaining > 0:
                self._hems_auth_backoff_until = time.monotonic() + remaining
        # Nominal voltage for estimated power when API omits voltage; user-configurable
        self._nominal_v = resolve_nominal_voltage_for_hass(hass)
        if config_entry is not None:
            configured_nominal = coerce_nominal_voltage(
                config_entry.options.get(OPT_NOMINAL_VOLTAGE)
            )
            if configured_nominal is not None:
                self._nominal_v = configured_nominal
        # Options: allow dynamic fast/slow polling
        interval = helper_coerce_int(
            config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            default=DEFAULT_SCAN_INTERVAL,
        )
        interval = min(MAX_POLL_INTERVAL, max(MIN_SLOW_POLL_INTERVAL, interval))
        if config_entry is not None:
            fast_opt = config_entry.options.get(OPT_FAST_POLL_INTERVAL)
            fast_interval = max(
                MIN_FAST_POLL_INTERVAL,
                helper_coerce_int(fast_opt, default=DEFAULT_FAST_POLL_INTERVAL),
            )
            slow_opt = config_entry.options.get(OPT_SLOW_POLL_INTERVAL)
            if slow_opt is not None:
                _fast, interval = normalize_poll_intervals(
                    fast_interval,
                    slow_opt,
                    fast_default=fast_interval,
                    slow_default=interval,
                )
            elif fast_opt is not None:
                interval = max(interval, fast_interval)
        self._configured_slow_poll_interval = interval
        self.summary = SummaryStore(
            lambda: self.client,
            site_id_getter=lambda: self.site_id,
            logger=_LOGGER,
        )
        self.energy = EnergyManager(
            client_provider=lambda: self.client,
            site_id=self.site_id,
            logger=_LOGGER,
            summary_invalidator=self.summary.invalidate,
            hems_auth_skip=lambda endpoint: self._skip_hems_polling_due_to_auth_circuit(
                endpoint=endpoint
            ),
            hems_auth_failure=lambda err, endpoint: self._note_hems_auth_failure(
                err,
                endpoint=endpoint,
            ),
            hems_auth_success=lambda endpoint: self._note_hems_auth_success(
                endpoint=endpoint
            ),
        )
        self.evse_timeseries = EVSETimeseriesManager(
            hass,
            lambda: self.client,
            site_id_getter=lambda: self.site_id,
            logger=_LOGGER,
        )
        self._session_history_interval_min = DEFAULT_SESSION_HISTORY_INTERVAL_MIN
        if config_entry is not None:
            try:
                configured_interval = helper_coerce_int(
                    config_entry.options.get(
                        OPT_SESSION_HISTORY_INTERVAL,
                        DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
                    ),
                    default=DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
                )
                self._session_history_interval_min = min(
                    MAX_SESSION_HISTORY_INTERVAL_MIN,
                    max(MIN_SESSION_HISTORY_INTERVAL_MIN, configured_interval),
                )
            except Exception:
                self._session_history_interval_min = (
                    DEFAULT_SESSION_HISTORY_INTERVAL_MIN
                )
        self._session_history_cache_ttl_value = max(
            MIN_SESSION_HISTORY_CACHE_TTL, self._session_history_interval_min * 60
        )
        self._session_history_day_retention = SESSION_HISTORY_CACHE_DAY_RETENTION
        super_kwargs = {
            "name": DOMAIN,
            "update_interval": timedelta(seconds=self._configured_slow_poll_interval),
            "always_update": self.site_only or not self.serials,
        }
        if config_entry is not None:
            super_kwargs["config_entry"] = config_entry
        super().__init__(
            hass,
            _LOGGER,
            **super_kwargs,
        )
        self.config_entry = config_entry
        self.session_history = SessionHistoryManager(
            hass,
            lambda: self.client,
            cache_ttl=self._session_history_cache_ttl_value,
            failure_backoff=SESSION_HISTORY_FAILURE_BACKOFF_S,
            concurrency=SESSION_HISTORY_CONCURRENCY,
            data_supplier=lambda: self.data,
            publish_callback=self.async_set_updated_data,
            site_id_getter=lambda: self.site_id,
            logger=_LOGGER,
        )
        self.evse_runtime = EvseRuntime(self)
        self.battery_runtime = BatteryRuntime(self)
        self.heatpump_runtime = HeatpumpRuntime(self)
        self._ensure_coordinator_runtime("current_power_runtime")
        self._ensure_coordinator_runtime("auth_refresh_runtime")
        self._ensure_coordinator_runtime("evse_feature_flags_runtime")
        self._ensure_coordinator_runtime("tariff_runtime")
        self.inventory_runtime = InventoryRuntime(self)
        self.discovery_snapshot = DiscoverySnapshotManager(self)
        self.inventory_view = InventoryView(self)
        self.diagnostics = CoordinatorDiagnostics(self)
        self.refresh_runner = RefreshRunner(self)
        self._endpoint_family_policies = self._build_endpoint_family_policies()

    def __setattr__(self, name, value):
        if name == "_async_fetch_sessions_today" and hasattr(self, "session_history"):
            object.__setattr__(self, name, value)
            self.session_history.set_fetch_override(value)
            return
        super().__setattr__(name, value)

    def _ensure_coordinator_runtime(self, attr_name: str) -> object:
        """Instantiate and cache a coordinator sub-runtime (single factory for __init__ / __getattr__)."""

        cls = COORDINATOR_RUNTIME_CLASSES[attr_name]
        existing = self.__dict__.get(attr_name)
        if existing is None:
            existing = cls(self)
            self.__dict__[attr_name] = existing
        return existing

    def __getattr__(self, name: str):
        if name == "energy":
            energy = EnergyManager(
                client_provider=lambda: getattr(self, "client", None),
                site_id=str(getattr(self, "site_id", "")),
                logger=_LOGGER,
                summary_invalidator=getattr(
                    getattr(self, "summary", None), "invalidate", None
                ),
                hems_auth_skip=lambda endpoint: self._skip_hems_polling_due_to_auth_circuit(
                    endpoint=endpoint
                ),
                hems_auth_failure=lambda err, endpoint: self._note_hems_auth_failure(
                    err,
                    endpoint=endpoint,
                ),
                hems_auth_success=lambda endpoint: self._note_hems_auth_success(
                    endpoint=endpoint
                ),
            )
            self.__dict__["energy"] = energy
            return energy
        if name == "evse_timeseries":
            manager = EVSETimeseriesManager(
                self.__dict__.get("hass"),
                lambda: self.__dict__.get("client"),
                site_id_getter=lambda: getattr(self, "site_id", None),
                logger=_LOGGER,
            )
            self.__dict__["evse_timeseries"] = manager
            return manager
        if name == "diagnostics":
            diagnostics = CoordinatorDiagnostics(self)
            self.__dict__["diagnostics"] = diagnostics
            return diagnostics
        if name == "inventory_view":
            inventory_view = InventoryView(self)
            self.__dict__["inventory_view"] = inventory_view
            return inventory_view
        if name == "refresh_runner":
            refresh_runner = RefreshRunner(self)
            self.__dict__["refresh_runner"] = refresh_runner
            return refresh_runner
        if name in COORDINATOR_RUNTIME_CLASSES:
            return self._ensure_coordinator_runtime(name)
        raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")

    async def _async_setup(self) -> None:
        """Prepare lightweight state before the first refresh."""
        self._phase_timings = {}

    def _build_endpoint_family_policies(self) -> dict[str, EndpointFamilyPolicy]:
        """Return cooldown/cache policies for read-only endpoint families."""

        return {
            "core_realtime": EndpointFamilyPolicy(
                failure_backoff_schedule_s=(60.0, 120.0, 300.0, 600.0),
                max_backoff_s=600.0,
            ),
            "battery_status": EndpointFamilyPolicy(
                success_ttl_s=300.0,
                stale_after_s=1800.0,
                failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
                max_backoff_s=3600.0,
                support_state_on_success=True,
            ),
            "ac_battery_devices": EndpointFamilyPolicy(
                success_ttl_s=300.0,
                stale_after_s=1800.0,
                failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
                max_backoff_s=3600.0,
                optional=True,
                suppress_after_failures=3,
                support_state_on_success=True,
            ),
            "ac_battery_telemetry": EndpointFamilyPolicy(
                success_ttl_s=300.0,
                stale_after_s=1800.0,
                failure_backoff_schedule_s=(300.0, 900.0, 1800.0, 3600.0),
                max_backoff_s=3600.0,
                optional=True,
                suppress_after_failures=3,
                support_state_on_success=True,
            ),
            "ac_battery_events": EndpointFamilyPolicy(
                success_ttl_s=900.0,
                stale_after_s=21600.0,
                failure_backoff_schedule_s=(900.0, 1800.0, 3600.0, 7200.0),
                max_backoff_s=7200.0,
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
        }

    async def async_request_refresh(self) -> None:
        """Request a coordinator refresh and allow one cooldown-bypass cycle."""

        self._endpoint_manual_bypass_requested = True
        self._endpoint_manual_bypass_active = True
        try:
            await super().async_request_refresh()
        finally:
            self._endpoint_manual_bypass_requested = False
            self._endpoint_manual_bypass_active = False

    def _endpoint_family_policy(self, family: str) -> EndpointFamilyPolicy | None:
        return self._endpoint_family_policies.get(family)

    def _endpoint_family_state(self, family: str) -> EndpointFamilyHealth:
        state = self._endpoint_family_health.get(family)
        if state is None:
            state = EndpointFamilyHealth()
            self._endpoint_family_health[family] = state
        return state

    def _consume_endpoint_manual_bypass(self) -> bool:
        requested = bool(self._endpoint_manual_bypass_requested)
        self._endpoint_manual_bypass_requested = False
        self._endpoint_manual_bypass_active = requested
        return requested

    def _clear_endpoint_manual_bypass(self) -> None:
        self._endpoint_manual_bypass_active = False

    def endpoint_manual_bypass_active(self) -> bool:
        """Return True when the current refresh cycle is bypassing wait windows."""

        return bool(self._endpoint_manual_bypass_active)

    def _endpoint_family_wait_active(self, family: str) -> bool:
        health = self._endpoint_family_state(family)
        next_retry = health.next_retry_mono
        if not isinstance(next_retry, (int, float)):
            return False
        if time.monotonic() < float(next_retry):
            return True
        health.next_retry_mono = None
        health.next_retry_utc = None
        health.cooldown_active = False
        return False

    def _endpoint_family_should_run(self, family: str, *, force: bool = False) -> bool:
        if self._endpoint_family_policy(family) is None:
            return True
        if force or self.endpoint_manual_bypass_active():
            return True
        return not self._endpoint_family_wait_active(family)

    def _endpoint_family_can_use_stale(self, family: str) -> bool:
        policy = self._endpoint_family_policy(family)
        if policy is None or policy.stale_after_s is None:
            return False
        last_success = self._endpoint_family_state(family).last_success_mono
        if not isinstance(last_success, (int, float)):
            return False
        return (time.monotonic() - float(last_success)) <= float(policy.stale_after_s)

    def _endpoint_family_next_retry_mono(self, family: str) -> float | None:
        """Return the current monotonic retry/cache deadline for an endpoint family."""

        next_retry = self._endpoint_family_state(family).next_retry_mono
        if not isinstance(next_retry, (int, float)):
            return None
        return float(next_retry)

    @staticmethod
    def _endpoint_family_status_from_error(err: Exception) -> int | None:
        if isinstance(err, aiohttp.ClientResponseError):
            return int(err.status)
        if isinstance(err, InvalidPayloadError):
            return int(err.status) if isinstance(err.status, int) else None
        return None

    @staticmethod
    def _retry_after_delay(err: Exception) -> float | None:
        """Return a Retry-After delay in seconds when an HTTP error provides one."""

        if not isinstance(err, aiohttp.ClientResponseError) or not err.headers:
            return None
        retry_after = err.headers.get("Retry-After")
        if not retry_after:
            return None
        try:
            return max(0.0, float(int(retry_after)))
        except Exception:
            retry_dt = None
            try:
                retry_dt = parsedate_to_datetime(str(retry_after))
            except Exception:
                retry_dt = None
            if retry_dt is None:
                return None
            if retry_dt.tzinfo is None:
                retry_dt = retry_dt.replace(tzinfo=_tz.utc)
            retry_dt = retry_dt.astimezone(_tz.utc)
            return max(0.0, (retry_dt - dt_util.utcnow()).total_seconds())

    def _endpoint_family_backoff_delay(
        self,
        family: str,
        consecutive_failures: int,
    ) -> float:
        policy = self._endpoint_family_policy(family)
        if policy is None or not policy.failure_backoff_schedule_s:
            return 0.0
        index = min(
            max(consecutive_failures - 1, 0),
            len(policy.failure_backoff_schedule_s) - 1,
        )
        delay = float(policy.failure_backoff_schedule_s[index])
        if policy.max_backoff_s is not None:
            delay = min(delay, float(policy.max_backoff_s))
        return delay * random.uniform(1.0, 1.1)

    def _endpoint_family_failure_is_cooldown_worthy(
        self,
        family: str,
        err: Exception,
    ) -> bool:
        status = self._endpoint_family_status_from_error(err)
        if isinstance(err, (InvalidPayloadError, OptionalEndpointUnavailable)):
            return True
        if status is not None:
            if status in (406, 429) or status >= 500:
                return True
            policy = self._endpoint_family_policy(family)
            return bool(policy and policy.optional and status in (401, 403, 404))
        if isinstance(err, (aiohttp.ClientError, asyncio.TimeoutError)):
            return True
        return True

    def _log_endpoint_family_transition(
        self,
        family: str,
        *,
        previous_wait_active: bool,
        previous_support_state: str,
        health: EndpointFamilyHealth,
        status: int | None = None,
        delay_s: float | None = None,
    ) -> None:
        site = redact_site_id(self.site_id)
        if (
            previous_support_state != "suppressed"
            and health.support_state == "suppressed"
        ):
            _LOGGER.info(
                "Endpoint family %s suppressed for site %s after repeated failures (status=%s, retry_in_s=%s)",
                family,
                site,
                status,
                round(delay_s, 1) if isinstance(delay_s, (int, float)) else None,
            )
            return
        if not previous_wait_active and health.cooldown_active:
            _LOGGER.info(
                "Endpoint family %s entered cooldown for site %s (status=%s, retry_in_s=%s)",
                family,
                site,
                status,
                round(delay_s, 1) if isinstance(delay_s, (int, float)) else None,
            )
            return
        if (
            (previous_wait_active or previous_support_state == "suppressed")
            and not health.cooldown_active
            and health.support_state in {"unknown", "supported"}
        ):
            _LOGGER.info(
                "Endpoint family %s recovered for site %s",
                family,
                site,
            )

    def _note_endpoint_family_success(
        self,
        family: str,
        *,
        success_ttl_s: float | None = None,
    ) -> None:
        policy = self._endpoint_family_policy(family)
        if policy is None:
            return
        health = self._endpoint_family_state(family)
        previous_wait_active = self._endpoint_family_wait_active(family)
        previous_support_state = health.support_state
        now_mono = time.monotonic()
        now_utc = dt_util.utcnow()
        ttl = policy.success_ttl_s if success_ttl_s is None else success_ttl_s
        health.request_count += 1
        health.last_request_utc = now_utc
        health.consecutive_failures = 0
        health.last_status = None
        health.last_error = None
        health.last_success_mono = now_mono
        health.last_success_utc = now_utc
        health.cooldown_active = False
        if policy.support_state_on_success or previous_support_state == "suppressed":
            health.support_state = "supported"
        if isinstance(ttl, (int, float)) and ttl > 0:
            health.next_retry_mono = now_mono + float(ttl)
            try:
                health.next_retry_utc = now_utc + timedelta(seconds=float(ttl))
            except Exception:
                health.next_retry_utc = None
        else:
            health.next_retry_mono = None
            health.next_retry_utc = None
        self._log_endpoint_family_transition(
            family,
            previous_wait_active=previous_wait_active,
            previous_support_state=previous_support_state,
            health=health,
        )

    def _note_endpoint_family_failure(self, family: str, err: Exception) -> bool:
        policy = self._endpoint_family_policy(family)
        if policy is None or not self._endpoint_family_failure_is_cooldown_worthy(
            family, err
        ):
            return False
        health = self._endpoint_family_state(family)
        previous_wait_active = self._endpoint_family_wait_active(family)
        previous_support_state = health.support_state
        now_utc = dt_util.utcnow()
        now_mono = time.monotonic()
        status = self._endpoint_family_status_from_error(err)
        health.request_count += 1
        health.last_request_utc = now_utc
        health.consecutive_failures += 1
        health.last_failure_utc = now_utc
        health.last_status = status
        health.last_error = redact_text(err, site_ids=(self.site_id,))
        delay = self._endpoint_family_backoff_delay(family, health.consecutive_failures)
        retry_after_delay = self._retry_after_delay(err)
        if retry_after_delay is not None:
            delay = max(delay, retry_after_delay)
        if policy.suppress_after_failures is not None and (
            health.consecutive_failures >= int(policy.suppress_after_failures)
        ):
            health.support_state = "suppressed"
            if policy.max_backoff_s is not None:
                delay = max(delay, float(policy.max_backoff_s))
        health.cooldown_active = True
        health.next_retry_mono = now_mono + delay
        try:
            health.next_retry_utc = now_utc + timedelta(seconds=delay)
        except Exception:
            health.next_retry_utc = None
        self._log_endpoint_family_transition(
            family,
            previous_wait_active=previous_wait_active,
            previous_support_state=previous_support_state,
            health=health,
            status=status,
            delay_s=delay,
        )
        return True

    @property
    def phase_timings(self) -> dict[str, float]:
        """Return the most recent phase timings."""
        return dict(self._phase_timings)

    @property
    def bootstrap_phase_timings(self) -> dict[str, float]:
        """Return the most recent bootstrap timings."""
        return dict(getattr(self, "_bootstrap_phase_timings", {}) or {})

    @property
    def warmup_phase_timings(self) -> dict[str, float]:
        """Return the most recent startup warmup timings."""
        return dict(getattr(self, "_warmup_phase_timings", {}) or {})

    @property
    def _summary_cache(self) -> tuple[float, list[dict], float] | None:
        """Legacy access to the summary cache tuple."""
        summary = getattr(self, "summary", None)
        if summary is None:
            return getattr(self, "_compat_summary_cache", None)
        return getattr(summary, "_cache", None)

    @_summary_cache.setter
    def _summary_cache(self, value: tuple[float, list[dict], float] | None) -> None:
        summary = getattr(self, "summary", None)
        if summary is None:
            self.__dict__["_compat_summary_cache"] = value
            return
        setattr(summary, "_cache", value)

    @property
    def _summary_ttl(self) -> float:
        """Legacy access to the current summary TTL."""
        summary = getattr(self, "summary", None)
        if summary is None:
            return getattr(self, "_compat_summary_ttl", 0.0)
        return summary.ttl

    @_summary_ttl.setter
    def _summary_ttl(self, value: float) -> None:
        summary = getattr(self, "summary", None)
        if summary is None:
            self.__dict__["_compat_summary_ttl"] = value
            return
        self.summary._ttl = value

    @property
    def _session_history_cache_ttl(self) -> float | None:
        """Expose the session history TTL for diagnostics/tests."""
        fallback = self.__dict__.get("_session_history_cache_ttl_value")
        session_history = self.__dict__.get("session_history")
        if session_history is None:
            return fallback
        return getattr(session_history, "cache_ttl", fallback)

    @_session_history_cache_ttl.setter
    def _session_history_cache_ttl(self, value: float | None) -> None:
        self._session_history_cache_ttl_value = value
        if hasattr(self, "session_history") and hasattr(
            self.session_history, "cache_ttl"
        ):
            self.session_history.cache_ttl = value

    @property
    def nominal_voltage(self) -> int:
        """Configured/default nominal voltage used when API voltage is missing."""
        return int(self._nominal_v)

    def preferred_nominal_voltage(self) -> int:
        """Best available nominal voltage (API-derived when available)."""
        discovered = self._preferred_operating_voltage()
        if discovered is not None:
            return discovered
        return int(self._nominal_v)

    def _preferred_operating_voltage(self) -> int | None:
        return preferred_operating_voltage(self._operating_v.values())

    def _seed_nominal_voltage_option_from_api(self) -> None:
        if self.config_entry is None:
            return

        options = dict(getattr(self.config_entry, "options", {}) or {})
        configured = coerce_nominal_voltage(options.get(OPT_NOMINAL_VOLTAGE))
        if configured is not None:
            self._nominal_v = configured
            return

        discovered = self._preferred_operating_voltage()
        if discovered is None:
            return

        options[OPT_NOMINAL_VOLTAGE] = discovered
        self._nominal_v = discovered
        try:
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=options
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to persist API-derived nominal voltage", exc_info=True
            )

    def _schedule_session_enrichment(
        self,
        serials: Iterable[str],
        day_local: datetime,
        *,
        max_cache_age: float | None = None,
    ) -> None:
        if max_cache_age is None:
            self.evse_runtime.schedule_session_enrichment(serials, day_local)
            return
        self.evse_runtime.schedule_session_enrichment(
            serials,
            day_local,
            max_cache_age=max_cache_age,
        )

    async def _async_enrich_sessions(
        self,
        serials: Iterable[str],
        day_local: datetime,
        *,
        in_background: bool,
        max_cache_age: float | None = None,
    ) -> dict[str, list[dict]]:
        if max_cache_age is None:
            return await self.evse_runtime.async_enrich_sessions(
                serials,
                day_local,
                in_background=in_background,
            )
        return await self.evse_runtime.async_enrich_sessions(
            serials,
            day_local,
            in_background=in_background,
            max_cache_age=max_cache_age,
        )

    def _sum_session_energy(self, sessions: list[dict]) -> float:
        return self.evse_runtime.sum_session_energy(sessions)

    @staticmethod
    def _session_history_day(payload: dict, day_local_default: datetime) -> datetime:
        return EvseRuntime.session_history_day(payload, day_local_default)

    def _session_history_soft_ttl(self, payload: dict) -> float:
        cache_ttl = self._session_history_cache_ttl
        base_ttl = float(cache_ttl or DEFAULT_SESSION_HISTORY_INTERVAL_MIN * 60)
        if payload.get("actual_charging") or payload.get("charging"):
            return min(base_ttl, SESSION_HISTORY_ACTIVE_SOFT_TTL_S)

        session_end = helper_coerce_optional_int(payload.get("session_end"))
        if session_end is not None:
            try:
                recent_stop_age = max(0.0, time.time() - float(session_end))
            except Exception:
                recent_stop_age = None
            if (
                recent_stop_age is not None
                and recent_stop_age <= SESSION_HISTORY_RECENT_STOP_WINDOW_S
            ):
                return min(base_ttl, SESSION_HISTORY_RECENT_STOP_SOFT_TTL_S)
        return base_ttl

    def _session_history_hard_ttl(self, payload: dict) -> float:
        cache_ttl = self._session_history_cache_ttl
        base_ttl = float(cache_ttl or DEFAULT_SESSION_HISTORY_INTERVAL_MIN * 60)
        soft_ttl = self._session_history_soft_ttl(payload)
        if soft_ttl < base_ttl:
            return base_ttl
        return max(base_ttl, soft_ttl + SESSION_HISTORY_IDLE_HARD_TTL_GRACE_S)

    def _session_history_prioritize_inline_refresh(self, payload: dict) -> bool:
        cache_ttl = self._session_history_cache_ttl
        base_ttl = float(cache_ttl or DEFAULT_SESSION_HISTORY_INTERVAL_MIN * 60)
        return self._session_history_soft_ttl(payload) < base_ttl

    async def _async_fetch_sessions_today(
        self,
        sn: str,
        *,
        day_local: datetime | None = None,
        max_cache_age: float | None = None,
    ) -> list[dict]:
        return await self.evse_runtime.async_fetch_sessions_today(
            sn,
            day_local=day_local,
            max_cache_age=max_cache_age,
        )

    @staticmethod
    def _normalize_serials(serials: Iterable[str] | None) -> set[str]:
        return EvseRuntime.normalize_serials(serials)

    def _retained_session_history_days(
        self, keep_day_keys: Iterable[str] | None = None
    ) -> set[str]:
        return self.evse_runtime.retained_session_history_days(keep_day_keys)

    def _prune_session_history_cache_shim(
        self,
        *,
        active_serials: Iterable[str] | None,
        keep_day_keys: Iterable[str] | None = None,
    ) -> None:
        self.evse_runtime.prune_session_history_cache_shim(
            active_serials=active_serials,
            keep_day_keys=keep_day_keys,
        )

    def _set_session_history_cache_shim_entry(
        self,
        serial: str,
        day_key: str,
        sessions: list[dict],
    ) -> None:
        self.evse_runtime.set_session_history_cache_shim_entry(
            serial,
            day_key,
            sessions,
        )

    def _prune_serial_runtime_state(self, active_serials: Iterable[str]) -> set[str]:
        return self.evse_runtime.prune_serial_runtime_state(active_serials)

    def _prune_runtime_caches(
        self,
        *,
        active_serials: Iterable[str],
        keep_day_keys: Iterable[str] | None = None,
    ) -> None:
        self.evse_runtime.prune_runtime_caches(
            active_serials=active_serials,
            keep_day_keys=keep_day_keys,
        )

    def cleanup_runtime_state(self) -> None:
        """Release runtime caches/listeners to make unload deterministic."""

        def _task_done(task: object) -> bool:
            done = getattr(task, "done", None)
            return callable(done) and done() is True

        if self._warmup_task is not None:
            self._warmup_task.cancel()
            self._warmup_task = None
        for task in list(self._amp_restart_tasks.values()):
            if task is not None and not _task_done(task):
                task.cancel()
        self._amp_restart_tasks.clear()
        if self._streaming_stop_task is not None:
            if not _task_done(self._streaming_stop_task):
                self._streaming_stop_task.cancel()
            self._streaming_stop_task = None
        if self._auth_refresh_task is not None:
            if not _task_done(self._auth_refresh_task):
                self._auth_refresh_task.cancel()
            self._auth_refresh_task = None
        clear_streaming_state = getattr(self, "_clear_streaming_state", None)
        if callable(clear_streaming_state):
            clear_streaming_state()
        self.discovery_snapshot.cancel_pending_save()
        session_manager = getattr(self, "session_history", None)
        if session_manager is not None and hasattr(session_manager, "clear"):
            session_manager.clear()
        self._session_history_cache_shim.clear()
        self._prune_runtime_caches(active_serials=(), keep_day_keys=())
        self._topology_listeners.clear()

    @callback
    def async_add_topology_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Listen for topology-only changes."""
        self._topology_listeners.append(update_callback)

        @callback
        def _remove_listener() -> None:
            if update_callback in self._topology_listeners:
                self._topology_listeners.remove(update_callback)

        return _remove_listener

    def topology_snapshot(self) -> CoordinatorTopologySnapshot:
        """Return the latest cached topology snapshot."""
        return self.inventory_runtime.topology_snapshot()

    def gateway_inventory_summary(self) -> dict[str, object]:
        source = self.inventory_runtime._gateway_inventory_summary_marker()
        summary = getattr(self, "_gateway_inventory_summary_cache", {}) or {}
        if source != self._gateway_inventory_summary_source:
            summary = self.inventory_runtime._build_gateway_inventory_summary()
            self._gateway_inventory_summary_cache = summary
            self._gateway_inventory_summary_source = source
        return dict(summary)

    def microinverter_inventory_summary(self) -> dict[str, object]:
        source = self.inventory_runtime._microinverter_inventory_summary_marker()
        summary = getattr(self, "_microinverter_inventory_summary_cache", {}) or {}
        if source != self._microinverter_inventory_summary_source:
            summary = self.inventory_runtime._build_microinverter_inventory_summary()
            self._microinverter_inventory_summary_cache = summary
            self._microinverter_inventory_summary_source = source
        return dict(summary)

    def heatpump_inventory_summary(self) -> dict[str, object]:
        source = self.inventory_runtime._heatpump_inventory_summary_marker()
        summary = getattr(self, "_heatpump_inventory_summary_cache", {}) or {}
        if source != self._heatpump_inventory_summary_source:
            summary = self.inventory_runtime._build_heatpump_inventory_summary()
            self._heatpump_inventory_summary_cache = summary
            self._heatpump_inventory_summary_source = source
        return dict(summary)

    def heatpump_type_summary(self, device_type: str) -> dict[str, object]:
        try:
            normalized = str(device_type).strip().upper()
        except Exception:  # noqa: BLE001
            normalized = ""
        source = self.inventory_runtime._heatpump_inventory_summary_marker()
        summaries = getattr(self, "_heatpump_type_summaries_cache", {}) or {}
        if source != self._heatpump_type_summaries_source or (
            normalized and normalized not in summaries
        ):
            summaries = self.inventory_runtime._build_heatpump_type_summaries()
            self._heatpump_type_summaries_cache = summaries
            self._heatpump_type_summaries_source = source
        summary = summaries.get(normalized, {})
        return dict(summary) if isinstance(summary, dict) else {}

    def _current_topology_snapshot(self) -> CoordinatorTopologySnapshot:
        return self.inventory_runtime._current_topology_snapshot()

    @callback
    def _notify_topology_listeners(self) -> None:
        self.inventory_runtime._notify_topology_listeners()

    @callback
    def _refresh_cached_topology(self) -> bool:
        return self.inventory_runtime._refresh_cached_topology()

    @callback
    def _begin_topology_refresh_batch(self) -> None:
        self.inventory_runtime._begin_topology_refresh_batch()

    @callback
    def _end_topology_refresh_batch(self) -> bool:
        return self.inventory_runtime._end_topology_refresh_batch()

    @staticmethod
    def _snapshot_compatible_value(value: object) -> object:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            out: dict[str, object] = {}
            for key, item in value.items():
                try:
                    key_text = str(key)
                except Exception:  # noqa: BLE001
                    continue
                out[key_text] = EnphaseCoordinator._snapshot_compatible_value(item)
            return out
        if isinstance(value, (list, tuple, set)):
            return [
                EnphaseCoordinator._snapshot_compatible_value(item) for item in value
            ]
        try:
            return str(value)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _snapshot_bool(value: object) -> bool | None:
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

    @staticmethod
    def _snapshot_text(value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    def _record_evse_transition_snapshot(
        self,
        serial: str,
        previous: dict[str, object] | None,
        current: dict[str, object],
    ) -> None:
        prev_status = self._snapshot_text(
            previous.get("connector_status") if isinstance(previous, dict) else None
        )
        cur_status = self._snapshot_text(current.get("connector_status"))
        if prev_status is None or cur_status is None or prev_status == cur_status:
            return

        snapshot = {
            "recorded_at_utc": dt_util.utcnow().isoformat(),
            "from_connector_status": prev_status,
            "to_connector_status": cur_status,
            "charging": self._snapshot_bool(current.get("charging")),
            "previous_charging": self._snapshot_bool(
                previous.get("charging") if isinstance(previous, dict) else None
            ),
            "charge_mode": self._snapshot_text(current.get("charge_mode")),
            "charge_mode_source": self._snapshot_text(
                current.get("charge_mode_source")
            ),
            "charge_mode_pref": self._snapshot_text(current.get("charge_mode_pref")),
            "charge_mode_pref_source": self._snapshot_text(
                current.get("charge_mode_pref_source")
            ),
            "charging_level": helper_coerce_optional_int(current.get("charging_level")),
            "charging_level_source": self._snapshot_text(
                current.get("charging_level_source")
            ),
            "last_set_amps": helper_coerce_optional_int(self.last_set_amps.get(serial)),
            "green_battery_enabled": self._snapshot_bool(
                current.get("green_battery_enabled")
            ),
            "safe_limit_state": helper_coerce_optional_int(
                current.get("safe_limit_state")
            ),
            "schedule_type": self._snapshot_text(current.get("schedule_type")),
            "sampled_at_utc": self._snapshot_text(current.get("sampled_at_utc")),
            "fetched_at_utc": self._snapshot_text(current.get("fetched_at_utc")),
            "scheduler_available": self.scheduler_available,
            "scheduler_backoff_active": self.scheduler_backoff_active(),
        }

        history = list(self.evse_state._evse_transition_snapshots.get(serial, []))
        history.append(snapshot)
        self.evse_state._evse_transition_snapshots[serial] = history[-8:]

        _LOGGER.debug(
            "EVSE connector transition for charger %s: %s -> %s "
            "(charging=%s, charge_mode=%s[%s], preferred_mode=%s[%s], "
            "charging_level=%s[%s], green_battery_enabled=%s, safe_limit_state=%s, "
            "scheduler_available=%s, scheduler_backoff_active=%s)",
            self._debug_truncate_identifier(serial) or "[unknown]",
            prev_status,
            cur_status,
            snapshot["charging"],
            snapshot["charge_mode"],
            snapshot["charge_mode_source"],
            snapshot["charge_mode_pref"],
            snapshot["charge_mode_pref_source"],
            snapshot["charging_level"],
            snapshot["charging_level_source"],
            snapshot["green_battery_enabled"],
            snapshot["safe_limit_state"],
            snapshot["scheduler_available"],
            snapshot["scheduler_backoff_active"],
        )

    @staticmethod
    def _charge_mode_resolution_parts(
        value: object,
    ) -> tuple[str | None, str | None]:
        mode = None
        source = None
        if value is not None:
            mode = getattr(value, "mode", None)
            source = getattr(value, "source", None)
            if mode is None and source is None:
                mode = value
        return mode, source

    def startup_migrations_ready(self) -> bool:
        return bool(getattr(self, "_devices_inventory_ready", False))

    def _publish_internal_state_update(self) -> None:
        current = self.data if isinstance(self.data, dict) else {}
        self.async_set_updated_data(dict(current))

    async def async_ensure_system_dashboard_diagnostics(self) -> None:
        await self.inventory_runtime.async_ensure_system_dashboard_diagnostics()

    async def async_ensure_battery_status_diagnostics(self) -> None:
        """Ensure battery-status payloads exist for diagnostics exports."""

        if isinstance(getattr(self, "_battery_status_payload", None), dict):
            return
        await self.battery_runtime.async_refresh_battery_status(force=True)

    async def _async_refresh_hems_support_preflight(
        self, *, force: bool = False
    ) -> None:
        await self.heatpump_runtime.async_refresh_hems_support_preflight(force=force)

    async def async_ensure_heatpump_runtime_diagnostics(
        self, *, force: bool = False
    ) -> None:
        await self.heatpump_runtime.async_ensure_heatpump_runtime_diagnostics(
            force=force
        )

    async def async_start_startup_warmup(self) -> None:
        await self.refresh_runner.async_start_startup_warmup()

    @staticmethod
    def _copy_diagnostics_value(value: object) -> object:  # pragma: no cover
        return copy_diagnostics_value(value)

    @staticmethod
    def _debug_truncate_identifier(value: object) -> str | None:  # pragma: no cover
        """Return a short, non-reversible debug identifier."""
        return truncate_identifier(value)

    @staticmethod
    def _debug_sorted_keys(value: object) -> list[str]:  # pragma: no cover
        """Return sorted string keys from a mapping."""

        return payload_debug.debug_sorted_keys(value)

    @classmethod
    def _debug_field_keys(cls, members: object) -> list[str]:  # pragma: no cover
        """Return sorted field keys present across a list of mappings."""

        return payload_debug.debug_field_keys(members)

    @classmethod
    def _debug_payload_shape(
        cls, payload: object
    ) -> dict[str, object]:  # pragma: no cover
        """Return a payload-shape summary suitable for debug logging."""

        return payload_debug.debug_payload_shape(payload)

    @staticmethod
    def _debug_render_summary(summary: object) -> str:  # pragma: no cover
        """Serialize a debug summary into stable compact JSON."""

        return payload_debug.debug_render_summary(summary)

    def _debug_log_summary_if_changed(  # pragma: no cover - debug helper
        self,
        cache_key: str,
        label: str,
        summary: object,
    ) -> None:
        """Log a debug summary only when it changes."""

        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        cached = self._debug_summary_log_cache.get(cache_key)
        if cached == summary:
            return
        self._debug_summary_log_cache[cache_key] = self._copy_diagnostics_value(summary)
        _LOGGER.debug("%s: %s", label, self._debug_render_summary(summary))

    def _debug_devices_inventory_summary(  # pragma: no cover - compatibility shim
        self,
        grouped: dict[str, dict[str, object]],
        ordered_keys: list[str],
    ) -> dict[str, object]:
        return self.inventory_runtime._debug_devices_inventory_summary(
            grouped, ordered_keys
        )

    def _debug_hems_inventory_summary(self) -> dict[str, object]:  # pragma: no cover
        return self.inventory_runtime._debug_hems_inventory_summary()

    def _debug_system_dashboard_summary(  # pragma: no cover - compatibility shim
        self,
        tree_payload: dict[str, object] | None,
        details_payloads: dict[str, dict[str, dict[str, object]]],
        type_summaries: dict[str, dict[str, object]],
        hierarchy_summary: dict[str, object],
    ) -> dict[str, object]:
        return self.inventory_runtime._debug_system_dashboard_summary(
            tree_payload,
            details_payloads,
            type_summaries,
            hierarchy_summary,
        )

    def _debug_evse_feature_flag_summary(self) -> dict[str, object]:  # pragma: no cover
        """Return a sanitized summary of EVSE feature-flag discovery."""

        return self.evse_feature_flags_runtime.debug_feature_flag_summary()

    def _debug_topology_summary(  # pragma: no cover - compatibility shim
        self, snapshot: CoordinatorTopologySnapshot
    ) -> dict[str, object]:
        return self.inventory_runtime._debug_topology_summary(snapshot)

    @staticmethod
    def _dashboard_key_token(key: object) -> str:  # pragma: no cover
        return sd_helpers.dashboard_key_token(key)

    @classmethod
    def _dashboard_key_matches(  # pragma: no cover - compatibility shim
        cls, key: object, *candidates: str
    ) -> bool:
        return sd_helpers.dashboard_key_matches(key, *candidates)

    @staticmethod
    def _dashboard_simple_value(value: object) -> object | None:  # pragma: no cover
        return sd_helpers.dashboard_simple_value(value)

    @classmethod
    def _iter_dashboard_mappings(  # pragma: no cover - compatibility shim
        cls, value: object
    ) -> Iterable[dict[str, object]]:
        yield from sd_helpers.iter_dashboard_mappings(value)

    @classmethod
    def _dashboard_first_value(  # pragma: no cover - compatibility shim
        cls, payload: object, *keys: str
    ) -> object | None:
        return sd_helpers.dashboard_first_value(payload, *keys)

    @classmethod
    def _dashboard_first_mapping(  # pragma: no cover - compatibility shim
        cls, payload: object, *keys: str
    ) -> dict[str, object] | None:
        return sd_helpers.dashboard_first_mapping(payload, *keys)

    @classmethod
    def _dashboard_field(  # pragma: no cover - compatibility shim
        cls, payload: object, *keys: str, default: object | None = None
    ) -> object | None:
        return sd_helpers.dashboard_field(payload, *keys, default=default)

    @classmethod
    def _dashboard_field_map(  # pragma: no cover - compatibility shim
        cls,
        payload: object,
        fields: dict[str, tuple[str, ...]],
    ) -> dict[str, object]:
        return sd_helpers.dashboard_field_map(payload, fields)

    @classmethod
    def _dashboard_aliases(  # pragma: no cover - compatibility shim
        cls, payload: dict[str, object]
    ) -> list[str]:
        return sd_helpers.dashboard_aliases(payload)

    @classmethod
    def _dashboard_primary_id(  # pragma: no cover - compatibility shim
        cls, payload: dict[str, object]
    ) -> str | None:
        return sd_helpers.dashboard_primary_id(payload)

    @classmethod
    def _dashboard_parent_id(  # pragma: no cover - compatibility shim
        cls, payload: dict[str, object]
    ) -> str | None:
        return sd_helpers.dashboard_parent_id(payload)

    @classmethod
    def _dashboard_raw_type(  # pragma: no cover - compatibility shim
        cls, payload: dict[str, object], fallback_type: str | None = None
    ) -> str | None:
        return sd_helpers.dashboard_raw_type(payload, fallback_type)

    @classmethod
    def _system_dashboard_type_key(  # pragma: no cover - compatibility shim
        cls, raw_type: object
    ) -> str | None:
        return sd_helpers.system_dashboard_type_key(raw_type)

    @classmethod
    def _system_dashboard_detail_records(  # pragma: no cover - compatibility shim
        cls,
        payloads: dict[str, object],
        *source_types: str,
    ) -> list[dict[str, object]]:
        return sd_helpers.system_dashboard_detail_records(payloads, *source_types)

    @classmethod
    def _system_dashboard_meter_kind(  # pragma: no cover - compatibility shim
        cls, payload: dict[str, object]
    ) -> str | None:
        return sd_helpers.system_dashboard_meter_kind(payload)

    @classmethod
    def _system_dashboard_battery_detail_subset(  # pragma: no cover - compatibility shim
        cls,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        return sd_helpers.system_dashboard_battery_detail_subset(payload)

    @classmethod
    def _dashboard_node_entry(  # pragma: no cover - compatibility shim
        cls,
        payload: dict[str, object],
        *,
        fallback_type: str | None = None,
        parent_uid: str | None = None,
    ) -> dict[str, object] | None:
        return sd_helpers.dashboard_node_entry(
            payload,
            fallback_type=fallback_type,
            parent_uid=parent_uid,
        )

    @classmethod
    def _dashboard_child_containers(  # pragma: no cover - compatibility shim
        cls, payload: dict[str, object]
    ) -> list[tuple[object, str | None]]:
        return sd_helpers.dashboard_child_containers(payload)

    @classmethod
    def _index_dashboard_nodes(  # pragma: no cover - compatibility shim
        cls,
        payload: object,
        *,
        fallback_type: str | None = None,
        parent_uid: str | None = None,
        index: dict[str, dict[str, object]] | None = None,
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, dict[str, object]]:
        return sd_helpers.index_dashboard_nodes(
            payload,
            fallback_type=fallback_type,
            parent_uid=parent_uid,
            index=index,
            alias_index=alias_index,
        )

    @classmethod
    def _system_dashboard_hierarchy_summary_from_index(  # pragma: no cover
        cls,
        index: dict[str, dict[str, object]],
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return sd_helpers.system_dashboard_hierarchy_summary_from_index(
            index, alias_index
        )

    @classmethod
    def _system_dashboard_type_hierarchy(  # pragma: no cover - compatibility shim
        cls,
        type_key: str,
        index: dict[str, dict[str, object]],
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return sd_helpers.system_dashboard_type_hierarchy(type_key, index, alias_index)

    @classmethod
    def _system_dashboard_meter_summaries(  # pragma: no cover - compatibility shim
        cls, payloads: dict[str, object]
    ) -> list[dict[str, object]]:
        return sd_helpers.system_dashboard_meter_summaries(payloads)

    @classmethod
    def _system_dashboard_envoy_summary(  # pragma: no cover - compatibility shim
        cls,
        payloads: dict[str, object],
        index: dict[str, dict[str, object]],
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return sd_helpers.system_dashboard_envoy_summary(payloads, index, alias_index)

    @classmethod
    def _system_dashboard_encharge_summary(  # pragma: no cover - compatibility shim
        cls,
        payloads: dict[str, object],
        index: dict[str, dict[str, object]],
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return sd_helpers.system_dashboard_encharge_summary(
            payloads, index, alias_index
        )

    @classmethod
    def _system_dashboard_microinverter_summary(  # pragma: no cover - compatibility shim
        cls,
        payloads: dict[str, object],
        index: dict[str, dict[str, object]],
        alias_index: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return sd_helpers.system_dashboard_microinverter_summary(
            payloads, index, alias_index
        )

    def _build_system_dashboard_summaries(  # pragma: no cover - compatibility shim
        self,
        tree_payload: dict[str, object] | None,
        details_payloads: dict[str, dict[str, object]],
    ) -> tuple[
        dict[str, dict[str, object]], dict[str, object], dict[str, dict[str, object]]
    ]:
        return sd_helpers.build_system_dashboard_summaries(
            tree_payload,
            details_payloads,
        )

    async def _async_refresh_system_dashboard(  # pragma: no cover - compatibility shim
        self, *, force: bool = False
    ) -> None:
        await self.inventory_runtime._async_refresh_system_dashboard(force=force)

    @staticmethod
    def _coerce_int(value: object, *, default: int = 0) -> int:  # pragma: no cover
        return helper_coerce_int(value, default=default)

    @staticmethod
    def coerce_int(value: object, *, default: int = 0) -> int:  # pragma: no cover
        return EnphaseCoordinator._coerce_int(value, default=default)

    @staticmethod
    def _normalize_iso_date(value: object) -> str | None:  # pragma: no cover
        return normalize_iso_date(value)

    def _inverter_start_date(self) -> str | None:  # pragma: no cover
        energy = getattr(self, "energy", None)
        return resolve_inverter_start_date(
            getattr(energy, "_site_energy_meta", None),
            getattr(self, "_inverter_data", None),
        )

    def _site_local_current_date(self) -> str:  # pragma: no cover
        return resolve_site_local_current_date(
            getattr(self, "_devices_inventory_payload", None),
            getattr(self, "_battery_timezone", None),
        )

    def _site_timezone_name(self) -> str:
        return resolve_site_timezone_name(getattr(self, "_battery_timezone", None))

    @staticmethod
    def _format_inverter_model_summary(model_counts: dict[str, int]) -> str | None:
        return InventoryRuntime._format_inverter_model_summary(model_counts)

    @staticmethod
    def _format_inverter_status_summary(summary_counts: dict[str, int]) -> str:
        return InventoryRuntime._format_inverter_status_summary(summary_counts)

    @staticmethod
    def _normalize_inverter_status(value: object) -> str:
        return InventoryRuntime._normalize_inverter_status(value)

    @staticmethod
    def _inverter_connectivity_state(  # pragma: no cover - compatibility shim
        summary_counts: dict[str, int],
    ) -> str | None:
        return InventoryRuntime._inverter_connectivity_state(summary_counts)

    @staticmethod
    def _parse_inverter_last_report(
        value: object,
    ) -> datetime | None:  # pragma: no cover
        return parse_inverter_last_report(value)

    def _merge_microinverter_type_bucket(self) -> None:  # pragma: no cover
        self.inventory_runtime._merge_microinverter_type_bucket()

    async def _async_refresh_inverters(self) -> None:
        await self.inventory_runtime._async_refresh_inverters()

    def _heatpump_primary_device_uid(self) -> str | None:
        return self.heatpump_runtime._heatpump_primary_device_uid()

    def _heatpump_runtime_device_uid(self) -> str | None:
        return self.heatpump_runtime._heatpump_runtime_device_uid()

    async def _async_refresh_heatpump_runtime_state(
        self, *, force: bool = False
    ) -> None:
        await self.heatpump_runtime.async_refresh_heatpump_runtime_state(force=force)

    async def _async_refresh_heatpump_daily_consumption(
        self, *, force: bool = False
    ) -> None:
        await self.heatpump_runtime.async_refresh_heatpump_daily_consumption(
            force=force
        )

    def _heatpump_daily_window(self) -> tuple[str, str, str, tuple[str, str]] | None:
        return self.heatpump_runtime._heatpump_daily_window()

    @staticmethod
    def _sum_optional_values(values: object) -> float | None:
        if not isinstance(values, list):
            return None
        total = 0.0
        found = False
        for item in values:
            if item is None:
                continue
            try:
                numeric = float(item)
            except Exception:
                continue
            if numeric != numeric or numeric in (float("inf"), float("-inf")):
                continue
            total += numeric
            found = True
        return total if found else None

    def _build_heatpump_daily_consumption_snapshot(
        self, split_payload: object, site_today_payload: object | None = None
    ) -> dict[str, object] | None:
        if site_today_payload is None:
            return self.heatpump_runtime._build_heatpump_daily_consumption_snapshot(
                split_payload
            )
        return self.heatpump_runtime._build_heatpump_daily_consumption_snapshot(
            split_payload,
            site_today_payload,
        )

    def _heatpump_member_for_uid(self, uid: object) -> dict[str, object] | None:
        return self.heatpump_runtime._heatpump_member_for_uid(uid)

    @classmethod
    def _heatpump_member_aliases(  # pragma: no cover - compatibility shim
        cls, member: dict[str, object] | None
    ) -> list[str]:
        return HeatpumpRuntime._heatpump_member_aliases(member)

    @classmethod
    def _heatpump_member_primary_id(
        cls, member: dict[str, object] | None
    ) -> str | None:
        return HeatpumpRuntime._heatpump_member_primary_id(member)

    @classmethod
    def _heatpump_member_parent_id(cls, member: dict[str, object] | None) -> str | None:
        return HeatpumpRuntime._heatpump_member_parent_id(member)

    def _heatpump_member_alias_map(self) -> dict[str, str]:
        return self.heatpump_runtime._heatpump_member_alias_map()

    def _heatpump_power_candidate_is_recommended(self, uid: str | None) -> bool:
        return self.heatpump_runtime._heatpump_power_candidate_is_recommended(uid)

    async def _async_refresh_heatpump_power(self, *, force: bool = False) -> None:
        await self.heatpump_runtime.async_refresh_heatpump_power(force=force)

    def _clear_current_power_consumption(self) -> None:
        self.current_power_runtime.clear()

    async def _async_refresh_current_power_consumption(
        self,
    ) -> None:  # pragma: no cover
        await self.current_power_runtime.async_refresh()

    def iter_inverter_serials(self) -> list[str]:
        return self.inventory_runtime.iter_inverter_serials()

    def inverter_data(self, serial: str) -> dict[str, object] | None:
        return self.inventory_runtime.inverter_data(serial)

    @staticmethod
    def parse_type_identifier(
        identifier: object,
    ) -> tuple[str, str] | None:  # pragma: no cover
        return parse_type_identifier(identifier)

    def collect_site_metrics(self) -> dict[str, object]:
        return self.diagnostics.collect_site_metrics()

    def charge_mode_cache_snapshot(self) -> dict[str, str]:  # pragma: no cover
        """Return scheduler mode cache keyed by charger serial."""

        return {
            str(sn): str(value[0])
            for sn, value in self._charge_mode_cache.items()
            if value and value[0]
        }

    def session_history_diagnostics(self) -> dict[str, object]:
        """Return session-history cache and service diagnostics."""

        session_manager = getattr(self, "session_history", None)
        if session_manager is None:
            return {
                "cache_ttl_seconds": None,
                "cache_keys": 0,
                "interval_minutes": None,
                "in_progress": 0,
            }
        last_failure_utc = getattr(session_manager, "service_last_failure_utc", None)
        return {
            "available": getattr(session_manager, "service_available", None),
            "using_stale": getattr(session_manager, "service_using_stale", None),
            "failures": getattr(session_manager, "service_failures", None),
            "last_error": getattr(session_manager, "service_last_error", None),
            "last_failure_utc": (
                last_failure_utc.isoformat()
                if isinstance(last_failure_utc, datetime)
                else None
            ),
            "last_payload_signature": getattr(
                session_manager,
                "_service_last_payload_signature",
                None,
            ),
            "cache_ttl_seconds": session_manager.cache_ttl,
            "cache_keys": session_manager.cache_key_count,
            "cache_state_counts": getattr(
                session_manager,
                "cache_state_counts",
                lambda: None,
            )(),
            "interval_minutes": getattr(self, "_session_history_interval_min", None),
            "in_progress": session_manager.in_progress,
        }

    def evse_timeseries_diagnostics(self) -> dict[str, object]:
        """Return EVSE-timeseries cache and service diagnostics."""

        manager = getattr(self, "evse_timeseries", None)
        if manager is None:
            return {
                "cache_ttl_seconds": None,
                "daily_cache_days": [],
                "daily_cache_age_seconds": {},
                "lifetime_cache_age_seconds": None,
                "lifetime_serial_count": 0,
            }
        return manager.diagnostics()

    def scheduler_diagnostics(self) -> dict[str, object]:
        """Return scheduler availability and failure diagnostics."""

        backoff_ends = getattr(self, "_scheduler_backoff_ends_utc", None)
        if isinstance(backoff_ends, datetime):
            backoff_ends = backoff_ends.isoformat()
        return {
            "available": self.scheduler_available,
            "last_error": self.scheduler_last_error,
            "failures": getattr(self, "_scheduler_failures", None),
            "backoff_until_monotonic": getattr(self, "_scheduler_backoff_until", None),
            "backoff_ends_utc": backoff_ends,
        }

    def battery_diagnostics_payloads(self) -> dict[str, object]:
        """Return battery-related payload snapshots used by diagnostics."""

        return {
            "site_settings_payload": getattr(
                self, "_battery_site_settings_payload", None
            ),
            "profile_payload": getattr(self, "_battery_profile_payload", None),
            "settings_payload": getattr(self, "_battery_settings_payload", None),
            "status_payload": getattr(self, "_battery_status_payload", None),
            "grid_control_check_payload": getattr(
                self, "_grid_control_check_payload", None
            ),
            "dry_contacts_payload": getattr(
                self, "_dry_contact_settings_payload", None
            ),
            "backup_history_payload": getattr(
                self, "_battery_backup_history_payload", None
            ),
            "hems_devices_payload": getattr(self, "_hems_devices_payload", None),
            "devices_inventory_payload": getattr(
                self, "_devices_inventory_payload", None
            ),
            "ac_battery_devices_payload": getattr(
                self, "_ac_battery_devices_payload", None
            ),
            "ac_battery_telemetry_payloads": getattr(
                self, "_ac_battery_telemetry_payloads", None
            ),
            "ac_battery_events_payloads": getattr(
                self, "_ac_battery_events_payloads", None
            ),
        }

    def heatpump_runtime_diagnostics(self) -> dict[str, object]:
        return self.heatpump_runtime.heatpump_runtime_diagnostics()

    def evse_diagnostics_payloads(self) -> dict[str, object]:
        """Return EVSE capability snapshots used by diagnostics."""

        charger_feature_flags = [
            {"serial": serial, "flags": dict(flags)}
            for serial, flags in sorted(
                getattr(self, "_evse_feature_flags_by_serial", {}).items()
            )
        ]
        charger_support_sources = []
        for serial, snapshot in sorted((self.data or {}).items()):
            if not isinstance(snapshot, dict):
                continue
            sources = {
                key: snapshot.get(f"{key}_source")
                for key in (
                    "charge_mode_supported",
                    "charging_amps_supported",
                    "storm_guard_supported",
                    "auth_feature_supported",
                    "rfid_feature_supported",
                    "plug_and_charge_supported",
                )
                if snapshot.get(f"{key}_source") is not None
            }
            if not sources:
                continue
            charger_support_sources.append({"serial": serial, "sources": sources})
        charger_runtime_sources = []
        for serial, snapshot in sorted((self.data or {}).items()):
            if not isinstance(snapshot, dict):
                continue
            runtime_sources = {
                key: snapshot.get(key)
                for key in (
                    "charge_mode_source",
                    "charge_mode_pref_source",
                    "charging_level_source",
                )
                if snapshot.get(key) is not None
            }
            if snapshot.get("charging_level") is not None:
                runtime_sources["charging_level"] = snapshot.get("charging_level")
            last_set_amps = self.last_set_amps.get(serial)
            if last_set_amps is not None:
                runtime_sources["last_set_amps"] = last_set_amps
            if runtime_sources:
                charger_runtime_sources.append(
                    {"serial": serial, "sources": runtime_sources}
                )
        charger_transition_history = [
            {"serial": serial, "transitions": copy_diagnostics_value(transitions)}
            for serial, transitions in sorted(
                getattr(self, "_evse_transition_snapshots", {}).items()
            )
            if transitions
        ]
        payload = getattr(self, "_evse_feature_flags_payload", None)
        payload_meta = payload.get("meta") if isinstance(payload, dict) else None
        payload_error = payload.get("error") if isinstance(payload, dict) else None
        return {
            "feature_flags_meta": (
                payload_meta if isinstance(payload_meta, dict) else None
            ),
            "feature_flags_error": (
                payload_error if isinstance(payload_error, dict) else None
            ),
            "site_feature_flags": dict(
                getattr(self, "_evse_site_feature_flags", {}) or {}
            ),
            "charger_feature_flags": charger_feature_flags,
            "charger_support_sources": charger_support_sources,
            "charger_runtime_sources": charger_runtime_sources,
            "charger_transition_history": charger_transition_history,
            "timeseries": self.evse_timeseries_diagnostics(),
        }

    def inverter_diagnostics_payloads(self) -> dict[str, object]:
        return self.inventory_runtime.inverter_diagnostics_payloads()

    def _system_dashboard_raw_payloads(  # pragma: no cover - compatibility shim
        self, canonical_type: str
    ) -> dict[str, dict[str, object]]:
        return self.inventory_runtime._system_dashboard_raw_payloads(canonical_type)

    def system_dashboard_envoy_detail(
        self,
    ) -> dict[str, object] | None:  # pragma: no cover
        return self.inventory_runtime.system_dashboard_envoy_detail()

    def system_dashboard_meter_detail(
        self, meter_kind: str
    ) -> dict[str, object] | None:
        return self.inventory_runtime.system_dashboard_meter_detail(meter_kind)

    def system_dashboard_battery_detail(self, serial: str) -> dict[str, object] | None:
        return self.inventory_runtime.system_dashboard_battery_detail(serial)

    def system_dashboard_diagnostics(self) -> dict[str, object]:
        return self.inventory_runtime.system_dashboard_diagnostics()

    def _issue_translation_placeholders(
        self, metrics: dict[str, object]
    ) -> dict[str, str]:
        return self.diagnostics.issue_translation_placeholders(metrics)

    def _issue_context(self) -> tuple[dict[str, object], dict[str, str]]:
        return self.diagnostics.issue_context()

    def _payload_health_state(self, name: str) -> dict[str, object]:
        return self.diagnostics.payload_health_state(name)

    def _mark_payload_endpoint_success(
        self,
        name: str,
        *,
        success_mono: float | None = None,
        success_utc: datetime | None = None,
    ) -> None:
        self.diagnostics.mark_payload_endpoint_success(
            name,
            success_mono=success_mono,
            success_utc=success_utc,
        )

    def _note_payload_endpoint_failure(
        self,
        name: str,
        *,
        error: str,
        signature: dict[str, object] | None = None,
        using_stale: bool = False,
    ) -> None:
        self.diagnostics.note_payload_endpoint_failure(
            name,
            error=error,
            signature=signature,
            using_stale=using_stale,
        )

    def _payload_endpoint_reusable(self, name: str, stale_after_s: float) -> bool:
        return self.diagnostics.payload_endpoint_reusable(name, stale_after_s)

    def _status_stale_window_s(self) -> float:
        return self.diagnostics.status_stale_window_s()

    def _status_payload_reusable(self, *, first_refresh: bool) -> bool:
        return (
            not first_refresh
            and isinstance(self._status_payload_cache, dict)
            and self._payload_endpoint_reusable("status", self._status_stale_window_s())
        )

    def payload_health_diagnostics(self) -> dict[str, object]:
        return self.diagnostics.payload_health_diagnostics()

    async def _async_run_timed_refresh_lookup(
        self,
        phase_timings: dict[str, float],
        timing_key: str,
        callback: Callable[[], object],
    ) -> object:
        started = time.monotonic()
        result = callback()
        if inspect.isawaitable(result):
            result = await result
        phase_timings[timing_key] = round(time.monotonic() - started, 3)
        return result

    async def _async_resolve_post_status_evse_enrichments(
        self,
        phase_timings: dict[str, float],
        *,
        records: list[tuple[str, dict]],
        charge_mode_candidates: list[str],
        first_refresh: bool,
    ) -> tuple[
        dict[str, str | None],
        dict[str, tuple[bool | None, bool]],
        dict[str, tuple[bool | None, bool | None, bool, bool]],
        dict[str, dict[str, object]],
    ]:
        if first_refresh:
            return {}, {}, {}, {}
        charger_config_keys = (
            DEFAULT_CHARGE_LEVEL_SETTING,
            PHASE_SWITCH_CONFIG_SETTING,
        )
        tasks: dict[str, asyncio.Task[object]] = {}
        now = time.monotonic()
        unique_candidates = list(dict.fromkeys(charge_mode_candidates))
        serials = [sn for sn, _obj in records]
        charge_modes = self.evse_runtime.resolve_cached_charge_modes(
            unique_candidates,
            now=now,
        )
        green_settings = self.evse_runtime.resolve_cached_green_battery_settings(
            serials,
            now=now,
        )
        auth_settings = self.evse_runtime.resolve_cached_auth_settings(
            serials,
            now=now,
        )
        charger_config = self.evse_runtime.resolve_cached_charger_config(
            serials,
            keys=charger_config_keys,
            now=now,
        )
        charge_mode_lookup_candidates = self.evse_runtime.charge_mode_lookup_candidates(
            unique_candidates,
            now=now,
        )
        if charge_mode_lookup_candidates:
            tasks["charge_modes"] = asyncio.create_task(
                self._async_run_timed_refresh_lookup(
                    phase_timings,
                    "charge_mode_s",
                    lambda: self.evse_runtime.async_resolve_charge_modes(
                        charge_mode_lookup_candidates
                    ),
                ),
                name=f"{DOMAIN}_resolve_charge_modes",
            )
        green_lookup_candidates = self.evse_runtime.green_battery_lookup_candidates(
            serials,
            now=now,
        )
        if green_lookup_candidates:
            tasks["green_settings"] = asyncio.create_task(
                self._async_run_timed_refresh_lookup(
                    phase_timings,
                    "green_settings_s",
                    lambda: self._async_resolve_green_battery_settings(
                        green_lookup_candidates
                    ),
                ),
                name=f"{DOMAIN}_resolve_green_battery_settings",
            )
        auth_lookup_candidates = self.evse_runtime.auth_settings_lookup_candidates(
            serials,
            now=now,
        )
        if auth_lookup_candidates:
            tasks["auth_settings"] = asyncio.create_task(
                self._async_run_timed_refresh_lookup(
                    phase_timings,
                    "auth_settings_s",
                    lambda: self._async_resolve_auth_settings(auth_lookup_candidates),
                ),
                name=f"{DOMAIN}_resolve_auth_settings",
            )
        charger_config_lookup_candidates = (
            self.evse_runtime.charger_config_lookup_candidates(
                serials,
                keys=charger_config_keys,
                now=now,
            )
        )
        if charger_config_lookup_candidates:
            tasks["charger_config"] = asyncio.create_task(
                self._async_run_timed_refresh_lookup(
                    phase_timings,
                    "charger_config_s",
                    lambda: self._async_resolve_charger_config(
                        charger_config_lookup_candidates,
                        keys=charger_config_keys,
                    ),
                ),
                name=f"{DOMAIN}_resolve_charger_config",
            )
        if not tasks:
            return charge_modes, green_settings, auth_settings, charger_config
        results = await asyncio.gather(*tasks.values())
        resolved = dict(zip(tasks.keys(), results, strict=False))
        charge_modes.update(resolved.get("charge_modes", {}))
        green_settings.update(resolved.get("green_settings", {}))
        auth_settings.update(resolved.get("auth_settings", {}))
        charger_config.update(resolved.get("charger_config", {}))
        return (
            charge_modes,
            green_settings,
            auth_settings,
            charger_config,
        )

    def _start_refresh_pipeline(self) -> RefreshPipelineContext:
        refresh_started_utc = dt_util.utcnow()
        if refresh_started_utc.tzinfo is None:
            refresh_started_utc = refresh_started_utc.replace(tzinfo=_tz.utc)
        reset_request_count = getattr(self.client, "reset_request_count", None)
        if callable(reset_request_count):
            reset_request_count()
        fallback_data: dict[str, dict] = {}
        if isinstance(self.data, dict):
            try:
                fallback_data = dict(self.data)
            except Exception:
                fallback_data = self.data
        return RefreshPipelineContext(
            started_mono=time.monotonic(),
            refresh_started_utc=refresh_started_utc,
            phase_timings={},
            fallback_data=fallback_data,
            first_refresh=not self._has_successful_refresh,
            auth_refresh_rejected_count_at_start=int(
                getattr(self, "_auth_refresh_rejected_count", 0)
            ),
        )

    async def _async_run_site_only_refresh_pipeline(
        self,
        context: RefreshPipelineContext,
    ) -> dict:
        phase_timings = context.phase_timings
        self._backoff_until = None
        self._clear_backoff_timer()
        self._clear_auth_repair_issues_on_success()
        self.diagnostics.clear_network_issue()
        self.diagnostics.clear_cloud_issue()
        self.diagnostics.clear_dns_issue()
        self.diagnostics.clear_rate_limited_issue()
        self._unauth_errors = 0
        self._rate_limit_hits = 0
        self._http_errors = 0
        self._network_errors = 0
        self._dns_failures = 0
        self._last_error = None
        self.backoff_ends_utc = None
        self._has_successful_refresh = True
        if context.first_refresh:
            self._start_first_refresh_followups(context)
        site_energy_start = time.monotonic()
        try:
            await self.energy._async_refresh_site_energy()
        except Exception:
            await self._async_cancel_first_refresh_followups(context)
            raise
        self.discovery_snapshot.sync_site_energy_discovery_state()
        self._sync_site_energy_issue()
        phase_timings["site_energy_s"] = round(time.monotonic() - site_energy_start, 3)
        if context.first_refresh:
            await self._async_await_first_refresh_followups(context)
        else:
            followup_plan = build_site_only_followup_plan(
                self,
                force_full=self.endpoint_manual_bypass_active(),
            )
            if followup_plan.stages:
                await self.refresh_runner.async_run_refresh_plan(
                    phase_timings,
                    plan=followup_plan,
                )
        self._clear_auth_refresh_rejection_state_if_unchanged(context)
        self._prune_runtime_caches(active_serials=(), keep_day_keys=())
        self._sync_battery_profile_pending_issue()
        self.last_success_utc = dt_util.utcnow()
        self.latency_ms = int((time.monotonic() - context.started_mono) * 1000)
        self._finish_refresh_pipeline(context)
        return {}

    def _record_status_refresh_success(
        self,
        context: RefreshPipelineContext,
    ) -> None:
        if self._unauth_errors:
            self._clear_auth_repair_issues_on_success()
        self._unauth_errors = 0
        self._rate_limit_hits = 0
        self._http_errors = 0
        self._payload_errors = 0
        self.diagnostics.clear_rate_limited_issue()
        if not getattr(context, "status_stale_network_failure", False):
            self.diagnostics.clear_network_issue()
            self._network_errors = 0
        self.diagnostics.clear_cloud_issue()
        self._backoff_until = None
        self._clear_backoff_timer()
        self._last_error = None
        if not getattr(context, "status_stale_dns_failure", False):
            self.diagnostics.clear_dns_issue()
            self._dns_failures = 0
        if not context.status_used_stale:
            self.last_success_utc = dt_util.utcnow()
            self.payload_using_stale = False
            self.payload_failure_kind = None
            self.last_failure_endpoint = None
        else:
            self._last_error = self.last_failure_description

    async def _async_run_post_status_refresh_pipeline(
        self,
        context: RefreshPipelineContext,
    ) -> None:
        if context.first_refresh:
            await self._async_await_first_refresh_followups(context)
        else:
            followup_plan = build_followup_plan(
                self,
                force_full=self.endpoint_manual_bypass_active(),
            )
            if followup_plan.stages:
                await self.refresh_runner.async_run_refresh_plan(
                    context.phase_timings,
                    plan=followup_plan,
                )
        self._clear_auth_refresh_rejection_state_if_unchanged(context)

    def _first_refresh_followup_calls(
        self,
    ) -> tuple[tuple[str, str, Callable[[], object], str | None], ...]:
        calls = []
        if self._first_refresh_storm_guard_followups_needed():
            calls.extend(
                (
                    (
                        "battery_site_settings_s",
                        "battery site settings",
                        lambda: self._async_refresh_battery_site_settings(),
                        "battery_site_settings",
                    ),
                    (
                        "storm_guard_s",
                        "storm guard",
                        lambda: self._async_refresh_storm_guard_profile(),
                        "storm_guard",
                    ),
                )
            )
        return tuple(calls)

    async def _async_run_first_refresh_followups(
        self,
        phase_timings: dict[str, float],
    ) -> None:
        calls = self._first_refresh_followup_calls()
        if not calls:
            return
        await self.refresh_runner.async_run_refresh_calls(
            phase_timings,
            calls=calls,
        )

    def _start_first_refresh_followups(
        self,
        context: RefreshPipelineContext,
    ) -> None:
        if context.first_refresh_followups_task is not None:
            return
        context.first_refresh_followups_task = asyncio.create_task(
            self._async_run_first_refresh_followups(context.phase_timings),
            name=f"{DOMAIN}_first_refresh_followups",
        )

    async def _async_await_first_refresh_followups(
        self,
        context: RefreshPipelineContext,
    ) -> None:
        task = context.first_refresh_followups_task
        if task is None:
            await self._async_run_first_refresh_followups(context.phase_timings)
            return
        try:
            await task
        finally:
            context.first_refresh_followups_task = None

    async def _async_cancel_first_refresh_followups(
        self,
        context: RefreshPipelineContext,
    ) -> None:
        task = context.first_refresh_followups_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        context.first_refresh_followups_task = None

    def _first_refresh_storm_guard_followups_needed(self) -> bool:
        if getattr(self, "_battery_has_encharge", None) is False:
            return False
        selected = getattr(self, "_selected_type_keys", None)
        if isinstance(selected, set) and selected:
            if self.site_only or not self.serials:
                return "envoy" in selected
            return bool({"encharge", "envoy", "iqevse"} & selected)
        return bool(self.site_id and (self.serials or self.site_only))

    async def _async_run_post_session_refresh_pipeline(
        self,
        context: RefreshPipelineContext,
        out: dict[str, dict],
        day_local_default: datetime,
    ) -> None:
        if context.first_refresh:
            return
        post_session_plan = build_post_session_followup_plan(
            self,
            day_local_default,
            force_full=self.endpoint_manual_bypass_active(),
        )
        if post_session_plan.stages:
            await self.refresh_runner.async_run_refresh_plan(
                context.phase_timings,
                plan=post_session_plan,
            )
        try:
            self.evse_timeseries.merge_charger_payloads(
                out, day_local=day_local_default
            )
        except Exception:
            pass
        self.discovery_snapshot.sync_site_energy_discovery_state()
        self._sync_site_energy_issue()
        self._sync_battery_profile_pending_issue()
        heatpump_plan = build_heatpump_followup_plan(
            self,
            force_full=self.endpoint_manual_bypass_active(),
        )
        if heatpump_plan.stages:
            await self.refresh_runner.async_run_refresh_plan(
                context.phase_timings,
                plan=heatpump_plan,
            )

    def _apply_refresh_polling_interval(self, polling_state: dict[str, object]) -> None:
        if self.config_entry is None:
            return
        target = polling_state["target"]
        if (
            not self.update_interval
            or int(self.update_interval.total_seconds()) != target
        ):
            new_interval = timedelta(seconds=target)
            self.update_interval = new_interval
            if hasattr(self, "async_set_update_interval"):
                try:
                    self.async_set_update_interval(new_interval)
                except Exception:
                    pass

    def _finish_refresh_pipeline(
        self,
        context: RefreshPipelineContext,
    ) -> None:
        phase_timings = context.phase_timings
        phase_timings["total_s"] = round(time.monotonic() - context.started_mono, 3)
        self._phase_timings = phase_timings.copy()
        request_count = getattr(self.client, "request_count", None)
        if isinstance(request_count, int):
            self._last_refresh_cloud_calls = request_count
            if context.fast_poll:
                self._last_fast_refresh_cloud_calls = request_count
            else:
                self._last_steady_refresh_cloud_calls = request_count
        else:
            request_count = None
        self._refresh_performance_history = record_refresh_performance_sample(
            getattr(self, "_refresh_performance_history", []),
            phase_timings,
            refresh_started_utc=context.refresh_started_utc,
            latency_ms=self.latency_ms,
            cloud_calls=request_count,
            fast_poll=context.fast_poll,
            first_refresh=context.first_refresh,
            payload_using_stale=getattr(context, "status_used_stale", False)
            or bool(getattr(self, "payload_using_stale", False)),
            manual_bypass=self.endpoint_manual_bypass_active(),
        )
        if context.first_refresh:
            self._bootstrap_phase_timings = phase_timings.copy()
        self._refresh_cached_topology()
        self.discovery_snapshot.schedule_save()

    async def _async_update_data(self) -> dict:
        context = self._start_refresh_pipeline()
        t0 = context.started_mono
        refresh_started_utc = context.refresh_started_utc
        phase_timings = context.phase_timings
        fallback_data = context.fallback_data
        first_refresh = context.first_refresh

        if self._auth_block_active():
            self._last_error = "auth_blocked"
            self.last_failure_utc = dt_util.utcnow()
            self.last_failure_status = None
            self.last_failure_description = self._blocked_auth_failure_message()
            self.last_failure_response = self.last_failure_description
            self.last_failure_source = "auth"
            self.last_failure_endpoint = None
            self._network_errors = 0
            self._http_errors = 0
            self._payload_errors = 0
            self.diagnostics.create_auth_block_issue()
            raise self._auth_block_update_failed()

        if self.site_only or not self.serials:
            return await self._async_run_site_only_refresh_pipeline(context)

        # Handle backoff window
        if self._backoff_until and time.monotonic() < self._backoff_until:
            retry_after = max(0.0, self._backoff_until - time.monotonic())
            raise UpdateFailed(
                "In backoff due to rate limiting or server errors",
                retry_after=retry_after,
            )

        if first_refresh:
            self._start_first_refresh_followups(context)
        status_refresh_succeeded = False
        try:
            status_start = time.monotonic()
            data = await self.client.status()
            phase_timings["status_s"] = round(time.monotonic() - status_start, 3)
            if isinstance(data, dict):
                self._status_payload_cache = dict(data)
            self._mark_payload_endpoint_success(
                "status",
                success_mono=time.monotonic(),
                success_utc=dt_util.utcnow(),
            )
            self.last_failure_endpoint = None
            self.payload_using_stale = False
            self.payload_failure_kind = None
            self._unauth_errors = 0
            self._clear_auth_repair_issues_on_success()
            status_refresh_succeeded = True
        except ConfigEntryAuthFailed:
            raise
        except Unauthorized as err:
            if self._activate_auth_block_from_login_wall(err):
                raise self._auth_block_update_failed() from err
            raise ConfigEntryAuthFailed from err
        except OptionalEndpointUnavailable as err:
            reason = (str(err) or "EVSE status endpoint unavailable").strip()
            can_reuse_status = self._status_payload_reusable(
                first_refresh=first_refresh
            )
            _LOGGER.debug(
                "EVSE status endpoint unavailable for site %s: %s",
                redact_site_id(self.site_id),
                redact_text(reason, site_ids=(self.site_id,)),
            )
            if can_reuse_status:
                self.payload_using_stale = True
                self._note_payload_endpoint_failure(
                    "status",
                    error=reason,
                    signature=None,
                    using_stale=True,
                )
                phase_timings["status_s"] = round(time.monotonic() - status_start, 3)
                data = dict(self._status_payload_cache)
                context.status_used_stale = True
            else:
                self.payload_using_stale = False
                self._note_payload_endpoint_failure(
                    "status",
                    error=reason,
                    signature=None,
                    using_stale=False,
                )
                phase_timings["status_s"] = round(time.monotonic() - status_start, 3)
                data = {"evChargerData": [], "ts": None}
            self._unauth_errors = 0
            self._payload_errors = 0
            self._network_errors = 0
            self._http_errors = 0
            self._clear_auth_repair_issues_on_success()
            status_refresh_succeeded = True
        except InvalidPayloadError as err:
            reason = (err.summary or str(err) or "Invalid JSON response").strip()
            signature = err.signature_dict()
            can_reuse_status = self._status_payload_reusable(
                first_refresh=first_refresh
            )
            self._last_error = reason
            self.last_failure_endpoint = err.endpoint
            self.payload_failure_kind = err.failure_kind
            self.last_failure_utc = dt_util.utcnow()
            self.last_failure_status = None
            self.last_failure_description = reason
            self.last_failure_response = reason
            self.last_failure_source = "payload"
            self._network_errors = 0
            self._http_errors = 0
            if can_reuse_status:
                self.payload_using_stale = True
                self._note_payload_endpoint_failure(
                    "status",
                    error=reason,
                    signature=signature,
                    using_stale=True,
                )
                phase_timings["status_s"] = round(time.monotonic() - status_start, 3)
                data = dict(self._status_payload_cache)
                context.status_used_stale = True
                self._payload_errors = 0
                self._backoff_until = None
                self._clear_backoff_timer()
                self.diagnostics.clear_cloud_issue()
                status_refresh_succeeded = True
            else:
                self.payload_using_stale = False
                self._note_payload_endpoint_failure(
                    "status",
                    error=reason,
                    signature=signature,
                    using_stale=False,
                )
                self._payload_errors += 1
                jitter = random.uniform(1.0, 2.5)
                backoff_multiplier = 2 ** min(self._payload_errors - 1, 3)
                slow_floor = self._slow_interval_floor()
                backoff = max(slow_floor, slow_floor * backoff_multiplier * jitter)
                self._backoff_until = time.monotonic() + backoff
                self._schedule_backoff_timer(backoff)
                if self._payload_errors >= 2:
                    self.diagnostics.report_cloud_issue()
                raise UpdateFailed(
                    f"Invalid API payload: {reason}",
                    retry_after=backoff,
                )
        except aiohttp.ClientResponseError as err:
            url = None
            try:
                url = getattr(err.request_info, "real_url", None)
            except Exception:
                url = None
            if is_scheduler_unavailable_error(err.message, err.status, url):
                self._note_scheduler_unavailable(
                    err, status=err.status, raw_payload=err.message
                )
                self._phase_timings = phase_timings.copy()
                return fallback_data
            # Respect Retry-After and create a warning issue on repeated 429
            self._last_error = f"HTTP {err.status}"
            self._network_errors = 0
            self._payload_errors = 0
            self._http_errors += 1
            delay = self._retry_after_delay(err) or 0.0
            # Exponential backoff anchored to configured slow poll interval
            jitter = random.uniform(1.0, 3.0)
            backoff_multiplier = 2 ** min(self._http_errors - 1, 3)
            slow_floor = self._slow_interval_floor()
            backoff = max(delay, slow_floor * backoff_multiplier * jitter)
            self._backoff_until = time.monotonic() + backoff
            self._schedule_backoff_timer(backoff)
            if err.status == 429:
                self._rate_limit_hits += 1
                if self._rate_limit_hits >= 2:
                    self.diagnostics.create_rate_limited_issue()
            else:
                is_server_error = 500 <= err.status < 600
                if is_server_error:
                    if self._http_errors >= 2:
                        self.diagnostics.report_cloud_issue()
                else:
                    self.diagnostics.clear_cloud_issue()
            raw_payload = redact_text(err.message, site_ids=(self.site_id,))
            description = _extract_error_description(raw_payload)
            reason = redact_text(err.message, site_ids=(self.site_id,))
            if not reason:
                reason = err.__class__.__name__
            now_utc = dt_util.utcnow()
            self.last_failure_utc = now_utc
            self.last_failure_status = err.status
            if description is None:
                try:
                    description = HTTPStatus(int(err.status)).phrase
                except Exception:
                    description = "HTTP error"
            self.last_failure_description = description
            self.last_failure_response = (
                raw_payload if raw_payload is not None else (reason or None)
            )
            self.last_failure_source = "http"
            self.last_failure_endpoint = str(url) if url is not None else None
            self.payload_using_stale = False
            self.payload_failure_kind = None
            raise UpdateFailed(
                f"Cloud error {err.status}: {reason}",
                retry_after=backoff,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            msg = redact_text(err, site_ids=(self.site_id,))
            if not msg:
                msg = err.__class__.__name__
            can_reuse_status = self._status_payload_reusable(
                first_refresh=first_refresh
            )
            self._last_error = msg
            self._network_errors += 1
            self._payload_errors = 0
            msg_lower = msg.lower()
            dns_failure = any(
                token in msg_lower
                for token in (
                    "dns",
                    "name or service not known",
                    "temporary failure in name resolution",
                    "resolv",
                )
            )
            if dns_failure:
                self._dns_failures += 1
            now_utc = dt_util.utcnow()
            self.last_failure_utc = now_utc
            self.last_failure_status = None
            self.last_failure_description = msg
            self.last_failure_response = None
            self.last_failure_source = "network"
            self.last_failure_endpoint = None
            if can_reuse_status:
                self.payload_using_stale = True
                self.payload_failure_kind = None
                self._note_payload_endpoint_failure(
                    "status",
                    error=msg,
                    signature=None,
                    using_stale=True,
                )
                phase_timings["status_s"] = round(time.monotonic() - status_start, 3)
                data = dict(self._status_payload_cache)
                context.status_used_stale = True
                context.status_stale_network_failure = True
                context.status_stale_dns_failure = bool(
                    dns_failure
                    or self._dns_failures
                    or getattr(self, "_dns_issue_reported", False)
                )
                if dns_failure and self._dns_failures >= 2:
                    self.diagnostics.report_dns_issue()
                status_refresh_succeeded = True
            else:
                backoff_multiplier = 2 ** min(self._network_errors - 1, 3)
                jitter = random.uniform(1.0, 2.5)
                slow_floor = self._slow_interval_floor()
                backoff = max(slow_floor, slow_floor * backoff_multiplier * jitter)
                self._backoff_until = time.monotonic() + backoff
                self._schedule_backoff_timer(backoff)
                if self._network_errors >= 3:
                    self.diagnostics.report_network_issue()
                if dns_failure and self._dns_failures >= 2:
                    self.diagnostics.report_dns_issue()
                self.payload_using_stale = False
                self.payload_failure_kind = None
                raise UpdateFailed(
                    f"Error communicating with API: {msg}",
                    retry_after=backoff,
                )
        finally:
            if not status_refresh_succeeded:
                await self._async_cancel_first_refresh_followups(context)
            self.latency_ms = int((time.monotonic() - t0) * 1000)

        self._record_status_refresh_success(context)
        await self._async_run_post_status_refresh_pipeline(context)

        prev_data = self.data if isinstance(self.data, dict) else {}
        self._has_successful_refresh = True
        out: dict[str, dict] = {}
        arr = data.get("evChargerData") or []
        data_ts = data.get("ts")
        records: list[tuple[str, dict]] = []
        charge_mode_candidates: list[str] = []
        for obj in arr:
            sn = str(obj.get("sn") or "")
            if not sn:
                continue
            self._ensure_serial_tracked(sn)
            records.append((sn, obj))
            if not self._has_embedded_charge_mode(obj):
                charge_mode_candidates.append(sn)

        if (
            not first_refresh
            and not charge_mode_candidates
            and records
            and not self.scheduler_available
            and not self._scheduler_backoff_active()
        ):
            try:
                await self._get_charge_mode(records[0][0])
            except Exception:
                pass
        (
            charge_modes,
            green_settings,
            auth_settings,
            charger_config,
        ) = await self._async_resolve_post_status_evse_enrichments(
            phase_timings,
            records=records,
            charge_mode_candidates=charge_mode_candidates,
            first_refresh=first_refresh,
        )

        _as_bool = _coerce_boolish
        _as_optional_bool = _coerce_optional_boolish
        _as_float = _coerce_floatish
        _as_int = _coerce_intish
        _as_text = _coerce_textish
        _as_int_list = _coerce_int_list
        _sec = _coerce_epoch_seconds

        def _support_value_and_source(
            runtime_value: bool | None,
            feature_flag_value: bool | None,
        ) -> tuple[bool | None, str]:
            if runtime_value is not None:
                return runtime_value, "runtime"
            if feature_flag_value is not None:
                return feature_flag_value, "feature_flag"
            return None, "unknown"

        for sn, obj in records:
            conn0 = (obj.get("connectors") or [{}])[0]
            previous_entry = None
            if isinstance(self.data, dict):
                previous_entry = self.data.get(sn)
            has_embedded_charge_mode = self._has_embedded_charge_mode(obj)
            raw_safe_limit = conn0.get("safeLimitState")
            if raw_safe_limit is None:
                raw_safe_limit = conn0.get("safe_limit_state")
            if raw_safe_limit is None:
                raw_safe_limit = obj.get("safeLimitState")
            if isinstance(raw_safe_limit, bool):
                safe_limit_state = int(raw_safe_limit)
            else:
                safe_limit_state = _as_int(raw_safe_limit)
            charging_level = None
            charging_level_source = None
            for key in ("chargingLevel", "charging_level", "charginglevel"):
                if key in obj and obj.get(key) is not None:
                    coerced_level = _as_int(obj.get(key))
                    if coerced_level is None:
                        continue
                    charging_level = coerced_level
                    charging_level_source = "status_payload"
                    break
            if charging_level is None:
                charging_level = _as_int(self.last_set_amps.get(sn))
                if charging_level is not None:
                    charging_level_source = "last_set_amps"
            # On initial load or after restart, seed the local last_set_amps
            # so UI controls (number entity) reflect the current setpoint
            # instead of defaulting to 0/min.
            safe_limit_active = (
                safe_limit_state is not None and int(safe_limit_state) != 0
            )
            skip_seed = safe_limit_active and charging_level_source == "status_payload"
            if (
                sn not in self.last_set_amps
                and charging_level is not None
                and not skip_seed
            ):
                try:
                    self.set_last_set_amps(sn, int(charging_level))
                except Exception:
                    pass
            sch = obj.get("sch_d") or {}
            sch_info0 = (sch.get("info") or [{}])[0]
            sess = obj.get("session_d") or {}
            smart_ev = obj.get("smartEV")
            if not isinstance(smart_ev, dict):
                smart_ev = {}
            # Derive last reported if not provided by API
            last_rpt = (
                obj.get("lst_rpt_at")
                or obj.get("lastReportedAt")
                or obj.get("last_reported_at")
            )
            if not last_rpt and data_ts is not None:
                try:
                    # Handle ISO string, seconds, or milliseconds epoch
                    if isinstance(data_ts, str):
                        if data_ts.endswith("Z[UTC]") or data_ts.endswith("Z"):
                            # Strip [UTC] if present; HA will display local time
                            s = data_ts.replace("[UTC]", "").replace("Z", "")
                            last_rpt = (
                                datetime.fromisoformat(s)
                                .replace(tzinfo=_tz.utc)
                                .isoformat()
                            )
                        elif data_ts.isdigit():
                            v = int(data_ts)
                            if v > 10**12:
                                v = v // 1000
                            last_rpt = datetime.fromtimestamp(v, tz=_tz.utc).isoformat()
                    elif isinstance(data_ts, (int, float)):
                        v = int(data_ts)
                        if v > 10**12:
                            v = v // 1000
                        last_rpt = datetime.fromtimestamp(v, tz=_tz.utc).isoformat()
                except Exception:
                    last_rpt = None
            sampled_at = _charger_sample_datetime(last_rpt)
            sampled_at_utc = sampled_at.isoformat() if sampled_at is not None else None
            sampled_at_ts = sampled_at.timestamp() if sampled_at is not None else None

            # Commissioned key variations
            commissioned_val = obj.get("commissioned")
            if commissioned_val is None:
                commissioned_val = obj.get("isCommissioned") or conn0.get(
                    "commissioned"
                )

            connector_status = obj.get("connectorStatusType") or conn0.get(
                "connectorStatusType"
            )
            connector_status_info = conn0.get("connectorStatusInfo")
            connector_status_norm = None
            suspended_by_evse = False
            if isinstance(connector_status, str):
                connector_status_norm = connector_status.strip().upper()
            reported_charging_flag = _as_bool(obj.get("charging"))
            charging_now_flag = reported_charging_flag
            if connector_status_norm == SUSPENDED_EVSE_STATUS:
                suspended_by_evse = True
            if connector_status_norm:
                if connector_status_norm == SUSPENDED_EVSE_STATUS:
                    charging_now_flag = False
                elif connector_status_norm in ACTIVE_CONNECTOR_STATUSES or any(
                    connector_status_norm.startswith(prefix)
                    for prefix in ACTIVE_SUSPENDED_PREFIXES
                ):
                    charging_now_flag = True
            actual_charging_flag = evse_power_is_actively_charging(
                connector_status_norm,
                reported_charging_flag,
                suspended_by_evse=suspended_by_evse,
            )
            self._record_actual_charging(sn, actual_charging_flag)
            pending_expectation = self._pending_charging.get(sn)
            if pending_expectation:
                target_state, expires_at = pending_expectation
                now_mono = time.monotonic()
                if actual_charging_flag == target_state or now_mono > expires_at:
                    self._pending_charging.pop(sn, None)
                else:
                    charging_now_flag = target_state

            # Keep preference state stable when scheduler lookups temporarily omit it.
            charge_mode_pref_source = None
            charge_mode_resolution = charge_modes.get(sn)
            charge_mode_pref = None
            charge_mode_value, charge_mode_source = self._charge_mode_resolution_parts(
                charge_mode_resolution
            )
            if charge_mode_value is not None:
                charge_mode_pref = self._normalize_charge_mode_preference(
                    charge_mode_value
                )
                if charge_mode_pref is not None:
                    charge_mode_pref_source = charge_mode_source
            if charge_mode_pref is None:
                charge_mode_pref = self._schedule_type_charge_mode_preference(
                    sch_info0.get("type")
                )
                if charge_mode_pref is not None:
                    charge_mode_pref_source = "schedule_type_fallback"
            if charge_mode_pref is None:
                charge_mode_pref = self._cached_charge_mode_preference(sn)
                if charge_mode_pref is not None:
                    charge_mode_pref_source = "cache"
            if charge_mode_pref is None:
                charge_mode_pref = self._battery_profile_charge_mode_preference(sn)
                if charge_mode_pref is not None:
                    charge_mode_pref_source = "battery_profile_fallback"

            charge_mode_source = None
            charge_mode = None
            for charge_mode_candidate in (
                obj.get("mode"),
                obj.get("chargeMode"),
                obj.get("chargingMode"),
                obj.get("charge_mode"),
                (obj.get("sch_d") or {}).get("mode"),
            ):
                charge_mode = self._normalize_effective_charge_mode(
                    charge_mode_candidate
                )
                if charge_mode is not None:
                    charge_mode_source = "explicit_status"
                    break
            if charge_mode is None:
                if charge_mode_pref:
                    charge_mode = charge_mode_pref
                    charge_mode_source = charge_mode_pref_source
                elif charging_now_flag:
                    charge_mode = "IMMEDIATE"
                    charge_mode_source = "charging_inference"
                else:
                    charge_mode = "IDLE"
                    charge_mode_source = "idle_inference"

            green_setting = green_settings.get(sn)
            green_enabled: bool | None = None
            green_supported: bool | None = None
            if green_setting is not None:
                green_enabled, green_supported = green_setting

            auth_setting = auth_settings.get(sn)
            app_auth_enabled: bool | None = None
            rfid_auth_enabled: bool | None = None
            app_auth_supported = False
            rfid_auth_supported = False
            auth_required: bool | None = None
            if auth_setting is not None:
                (
                    app_auth_enabled,
                    rfid_auth_enabled,
                    app_auth_supported,
                    rfid_auth_supported,
                ) = auth_setting
                if app_auth_supported or rfid_auth_supported:
                    values = [
                        value
                        for value in (app_auth_enabled, rfid_auth_enabled)
                        if value is not None
                    ]
                    if values:
                        auth_required = any(values)

            charge_mode_support, charge_mode_support_source = _support_value_and_source(
                (
                    True
                    if charge_mode_pref is not None or has_embedded_charge_mode
                    else None
                ),
                self.evse_feature_flag_enabled("evse_charging_mode", sn),
            )
            charging_amps_hint = self.evse_feature_flag_enabled(
                "max_current_config_support", sn
            )
            if charging_amps_hint is None:
                charging_amps_hint = self.evse_feature_flag_enabled(
                    "evse_charge_level_control", sn
                )
            charging_amps_support, charging_amps_support_source = (
                _support_value_and_source(
                    True if charging_level is not None else None,
                    charging_amps_hint,
                )
            )
            storm_guard_support, storm_guard_support_source = _support_value_and_source(
                (
                    True
                    if self._storm_guard_state is not None
                    or self._storm_evse_enabled is not None
                    else None
                ),
                self.evse_feature_flag_enabled("evse_storm_guard", sn),
            )
            auth_feature_support, auth_feature_support_source = (
                _support_value_and_source(
                    app_auth_supported if auth_setting is not None else None,
                    self.evse_feature_flag_enabled("evse_authentication", sn),
                )
            )
            rfid_feature_support, rfid_feature_support_source = (
                _support_value_and_source(
                    rfid_auth_supported if auth_setting is not None else None,
                    self.evse_feature_flag_enabled("iqevse_rfid", sn),
                )
            )
            plug_and_charge_support, plug_and_charge_support_source = (
                _support_value_and_source(
                    None,
                    self.evse_feature_flag_enabled("plug_and_charge", sn),
                )
            )

            # Determine a stable session end when not charging
            charging_now = charging_now_flag
            if (
                sn in self._last_charging
                and self._last_charging.get(sn)
                and not charging_now
            ):
                # Transition charging -> not charging: capture a fixed end time
                try:
                    if isinstance(data_ts, (int, float)) or (
                        isinstance(data_ts, str) and data_ts.isdigit()
                    ):
                        val = _sec(data_ts)
                        if val is not None:
                            self._session_end_fix[sn] = val
                        else:
                            self._session_end_fix[sn] = int(time.time())
                    else:
                        self._session_end_fix[sn] = int(time.time())
                except Exception:
                    self._session_end_fix[sn] = int(time.time())
            elif charging_now:
                # Clear fixed end when charging resumes
                self._session_end_fix.pop(sn, None)
            self._last_charging[sn] = charging_now

            session_end = None
            if not charging_now:
                # Prefer fixed end captured at stop; fall back to plug-out timestamp
                session_end = self._session_end_fix.get(sn)
                if session_end is None and sess.get("plg_out_at") is not None:
                    session_end = _sec(sess.get("plg_out_at"))

            # Session energy normalization: many deployments report Wh in e_c
            session_energy_wh = _as_float(sess.get("e_c"))
            ses_kwh = session_energy_wh
            if isinstance(ses_kwh, (int, float)):
                try:
                    if ses_kwh > 200:
                        ses_kwh = round(float(ses_kwh) / 1000.0, 2)
                    else:
                        ses_kwh = round(float(ses_kwh), 2)
                except Exception:
                    ses_kwh = session_energy_wh
            else:
                ses_kwh = sess.get("e_c")

            display_name = obj.get("displayName") or obj.get("name")
            if display_name is not None:
                try:
                    display_name = str(display_name)
                except Exception:
                    display_name = None
            session_charge_level = None
            for key in (
                "chargeLevel",
                "charge_level",
                "chargingLevel",
                "charging_level",
            ):
                raw_level = sess.get(key)
                if raw_level is not None:
                    session_charge_level = _as_int(raw_level)
                    break
            raw_miles = sess.get("miles")
            session_miles = _as_float(raw_miles, precision=3)
            if session_miles is None:
                session_miles = raw_miles

            session_cost = None
            for key in ("session_cost", "sessionCost"):
                session_cost = _as_float(sess.get(key), precision=3)
                if session_cost is not None:
                    break
            schedule_days = _as_int_list(sch_info0.get("days"))
            schedule_remind = _as_optional_bool(sch_info0.get("remindFlag"))
            if schedule_remind is None:
                schedule_remind = _as_optional_bool(sch_info0.get("reminderEnabled"))
            session_auth_status = _as_int(
                sess.get("auth_status")
                if sess.get("auth_status") is not None
                else sess.get("authStatus")
            )
            session_auth_type = _as_text(
                sess.get("auth_type")
                if sess.get("auth_type") is not None
                else sess.get("authType")
            )
            session_auth_identifier = _as_text(
                sess.get("auth_id")
                if sess.get("auth_id") is not None
                else sess.get("authIdentifier")
            )
            session_auth_token = _as_text(
                sess.get("auth_token")
                if sess.get("auth_token") is not None
                else sess.get("authToken")
            )
            config_values = charger_config.get(sn) or {}

            entry = {
                "sn": sn,
                "name": obj.get("name"),
                "display_name": display_name,
                "connected": _as_bool(obj.get("connected")),
                "plugged": _as_bool(obj.get("pluggedIn")),
                "charging": charging_now_flag,
                "actual_charging": actual_charging_flag,
                "faulted": _as_bool(obj.get("faulted")),
                "connector_status": connector_status,
                "connector_reason": conn0.get("connectorStatusReason"),
                "connector_status_info": connector_status_info,
                "safe_limit_state": safe_limit_state,
                "dlb_active": (
                    _as_bool(conn0.get("dlbActive"))
                    if conn0.get("dlbActive") is not None
                    else None
                ),
                "suspended_by_evse": suspended_by_evse,
                "session_energy_wh": session_energy_wh,
                "session_kwh": ses_kwh,
                "session_miles": session_miles,
                # Normalize session start epoch if needed
                "session_start": _sec(
                    sess.get("start_time")
                    if sess.get("start_time") is not None
                    else sess.get("strt_chrg")
                ),
                "session_end": session_end,
                "session_plug_in_at": sess.get("plg_in_at"),
                "session_plug_out_at": sess.get("plg_out_at"),
                "session_auth_status": session_auth_status,
                "session_auth_type": session_auth_type,
                "session_auth_identifier": session_auth_identifier,
                "session_auth_token_present": bool(session_auth_token),
                "last_reported_at": last_rpt,
                "sampled_at_utc": sampled_at_utc,
                "sampled_at_ts": sampled_at_ts,
                "fetched_at_utc": refresh_started_utc.isoformat(),
                "offline_since": obj.get("offlineAt"),
                "commissioned": _as_bool(commissioned_val),
                "schedule_status": sch.get("status"),
                "schedule_type": sch_info0.get("type") or sch.get("status"),
                "schedule_start": sch_info0.get("startTime"),
                "schedule_end": sch_info0.get("endTime"),
                "schedule_slot_id": _as_text(sch_info0.get("id")),
                "schedule_days": schedule_days,
                "schedule_reminder_enabled": schedule_remind,
                "schedule_reminder_min": _as_int(sch_info0.get("remindTime")),
                "charge_mode": charge_mode,
                "charge_mode_source": charge_mode_source,
                # Expose scheduler preference explicitly for entities that care
                "charge_mode_pref": charge_mode_pref,
                "charge_mode_pref_source": charge_mode_pref_source,
                "charging_level": charging_level,
                "charging_level_source": charging_level_source,
                "storm_guard_state": self._storm_guard_state,
                "storm_evse_enabled": self._storm_evse_enabled,
                "session_charge_level": session_charge_level,
                "session_cost": session_cost,
                "off_grid_state": _as_text(obj.get("offGrid")),
                "ev_manufacturer_name": _as_text(obj.get("evManufacturerName")),
                "ev_details_set": _as_optional_bool(obj.get("isEVDetailsSet")),
                "smart_ev_has_token": _as_optional_bool(smart_ev.get("hasToken")),
                "smart_ev_has_details": _as_optional_bool(smart_ev.get("hasEVDetails")),
                "operating_v": self._operating_v.get(sn),
                "nominal_v": self._nominal_v,
                "charge_mode_supported": charge_mode_support,
                "charge_mode_supported_source": charge_mode_support_source,
                "charging_amps_supported": charging_amps_support,
                "charging_amps_supported_source": charging_amps_support_source,
                "storm_guard_supported": storm_guard_support,
                "storm_guard_supported_source": storm_guard_support_source,
                "auth_feature_supported": auth_feature_support,
                "auth_feature_supported_source": auth_feature_support_source,
                "rfid_feature_supported": rfid_feature_support,
                "rfid_feature_supported_source": rfid_feature_support_source,
                "plug_and_charge_supported": plug_and_charge_support,
                "plug_and_charge_supported_source": plug_and_charge_support_source,
            }
            if isinstance(previous_entry, dict):
                previous_lifetime_kwh = _power_as_float(
                    previous_entry.get("lifetime_kwh")
                )
                if previous_lifetime_kwh is not None:
                    entry.setdefault("lifetime_kwh", previous_lifetime_kwh)
            if PHASE_SWITCH_CONFIG_SETTING in config_values:
                entry["phase_switch_config"] = config_values[
                    PHASE_SWITCH_CONFIG_SETTING
                ]
            if DEFAULT_CHARGE_LEVEL_SETTING in config_values:
                entry["default_charge_level"] = config_values[
                    DEFAULT_CHARGE_LEVEL_SETTING
                ]
            if green_supported is not None:
                entry["green_battery_supported"] = green_supported
                if green_supported:
                    entry["green_battery_enabled"] = green_enabled
            if auth_setting is not None:
                entry["app_auth_supported"] = app_auth_supported
                entry["rfid_auth_supported"] = rfid_auth_supported
                entry["app_auth_enabled"] = app_auth_enabled
                entry["rfid_auth_enabled"] = rfid_auth_enabled
                entry["auth_required"] = auth_required
            self._record_evse_transition_snapshot(sn, previous_entry, entry)

            out[sn] = entry

        self._sync_desired_charging(out)

        polling_state = self._determine_polling_state(out)
        context.fast_poll = bool(polling_state["want_fast"])
        summary_force = self.summary.prepare_refresh(
            want_fast=polling_state["want_fast"],
            target_interval=float(polling_state["target"]),
        )

        # Enrich with summary v2 data
        summary_start = time.monotonic()
        summary = await self.summary.async_fetch(force=summary_force)
        phase_timings["summary_s"] = round(time.monotonic() - summary_start, 3)
        if summary:
            for item in summary:
                sn = str(item.get("serialNumber") or "")
                if not sn:
                    continue
                self._ensure_serial_tracked(sn)
                cur = out.setdefault(sn, {})
                cur.setdefault("nominal_v", self._nominal_v)
                prev_sn = prev_data.get(sn) if isinstance(prev_data, dict) else None
                # Max current capability and phase/status
                cur["max_current"] = item.get("maxCurrent")
                cld = item.get("chargeLevelDetails") or {}
                try:
                    cur["min_amp"] = (
                        int(str(cld.get("min"))) if cld.get("min") is not None else None
                    )
                except Exception:
                    cur["min_amp"] = None
                try:
                    cur["max_amp"] = (
                        int(str(cld.get("max"))) if cld.get("max") is not None else None
                    )
                except Exception:
                    cur["max_amp"] = None
                try:
                    cur["amp_granularity"] = (
                        int(str(cld.get("granularity")))
                        if cld.get("granularity") is not None
                        else None
                    )
                except Exception:
                    cur["amp_granularity"] = None
                if any(
                    cur.get(key) is not None
                    for key in (
                        "charging_level",
                        "min_amp",
                        "max_amp",
                        "max_current",
                        "amp_granularity",
                    )
                ):
                    cur["charging_amps_supported"] = True
                    cur["charging_amps_supported_source"] = "runtime"
                cur["phase_mode"] = item.get("phaseMode")
                cur["status"] = item.get("status")
                supports_use_battery = _as_optional_bool(item.get("supportsUseBattery"))
                if supports_use_battery is not None:
                    cur["green_battery_supported"] = supports_use_battery
                    if not supports_use_battery:
                        cur.pop("green_battery_enabled", None)
                default_charge_level = cld.get("defaultChargeLevel")
                if default_charge_level is not None:
                    cur["default_charge_level"] = default_charge_level
                conn = item.get("activeConnection")
                if isinstance(conn, str):
                    conn = conn.strip()
                if conn:
                    cur["connection"] = conn
                net_cfg = item.get("networkConfig")
                ip_addr = None
                mac_addr = None
                interface_count = 0
                entries: list = []
                if isinstance(net_cfg, dict):
                    entries = [net_cfg]
                elif isinstance(net_cfg, list):
                    entries = net_cfg
                elif isinstance(net_cfg, str):
                    raw = net_cfg.strip()
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        parsed = []
                        raw_body = raw.strip("[]\n ")
                        for line in raw_body.splitlines():
                            line = line.strip().strip(",")
                            if line.startswith('"') and line.endswith('"'):
                                line = line[1:-1]
                            if line:
                                parsed.append(line)
                    entries = parsed if isinstance(parsed, list) else []
                for entry in entries:
                    parts: dict[str, object] = {}
                    if isinstance(entry, dict):
                        parts = entry
                    elif isinstance(entry, str):
                        for piece in entry.split(","):
                            if "=" in piece:
                                k, v = piece.split("=", 1)
                                parts[k.strip()] = v.strip()
                    if not parts:
                        continue
                    interface_count += 1
                    candidate = parts.get("ipaddr") or parts.get("ip")
                    candidate_mac = parts.get("mac_addr")
                    if candidate_mac is None:
                        candidate_mac = parts.get("mac")
                    if candidate_mac is not None and mac_addr is None:
                        mac_addr = candidate_mac
                    if candidate and ip_addr is None:
                        ip_addr = candidate
                    if candidate and str(parts.get("connectionStatus")) in (
                        "1",
                        "true",
                        "True",
                    ):
                        ip_addr = candidate
                        if candidate_mac is not None:
                            mac_addr = candidate_mac
                        break
                if isinstance(ip_addr, str) and not ip_addr:
                    ip_addr = None
                if isinstance(mac_addr, str) and not mac_addr:
                    mac_addr = None
                if ip_addr:
                    cur["ip_address"] = str(ip_addr)
                if mac_addr:
                    cur["mac_address"] = str(mac_addr)
                if interface_count > 0:
                    cur["network_interface_count"] = interface_count
                interval = item.get("reportingInterval")
                if interval is not None:
                    try:
                        cur["reporting_interval"] = int(str(interval))
                    except Exception:
                        pass
                if item.get("dlbEnabled") is not None:
                    cur["dlb_enabled"] = _as_bool(item.get("dlbEnabled"))
                # Commissioning: prefer explicit commissioningStatus from summary
                if item.get("commissioningStatus") is not None:
                    cur["commissioned"] = bool(item.get("commissioningStatus"))
                if item.get("commissioningStatus") is not None:
                    cur["commissioning_status"] = _as_int(
                        item.get("commissioningStatus")
                    )
                # Last reported: prefer summary if present
                if item.get("lastReportedAt"):
                    cur["last_reported_at"] = item.get("lastReportedAt")
                if item.get("timezone") is not None:
                    cur["charger_timezone"] = _as_text(item.get("timezone"))
                if item.get("isConnected") is not None:
                    cur["is_connected"] = _as_optional_bool(item.get("isConnected"))
                if item.get("isLocallyConnected") is not None:
                    cur["is_locally_connected"] = _as_optional_bool(
                        item.get("isLocallyConnected")
                    )
                if item.get("hoControl") is not None:
                    cur["ho_control"] = _as_optional_bool(item.get("hoControl"))
                if item.get("warrantyStartDate") is not None:
                    cur["warranty_start_date"] = item.get("warrantyStartDate")
                if item.get("warrantyDueDate") is not None:
                    cur["warranty_due_date"] = item.get("warrantyDueDate")
                if item.get("warrantyPeriod") is not None:
                    cur["warranty_period_years"] = _as_int(item.get("warrantyPeriod"))
                if item.get("createdAt") is not None:
                    cur["created_at"] = item.get("createdAt")
                if item.get("breakerRating") is not None:
                    cur["breaker_rating"] = _as_int(item.get("breakerRating"))
                if item.get("ratedCurrent") is not None:
                    cur["rated_current"] = _as_int(item.get("ratedCurrent"))
                if item.get("gridType") is not None:
                    cur["grid_type"] = _as_int(item.get("gridType"))
                if item.get("phaseCount") is not None:
                    cur["phase_count"] = _as_int(item.get("phaseCount"))
                if item.get("wifiConfig") is not None:
                    cur["wifi_config"] = _as_text(item.get("wifiConfig"))
                if item.get("cellularConfig") is not None:
                    cur["cellular_config"] = _as_text(item.get("cellularConfig"))
                if item.get("defaultRoute") is not None:
                    cur["default_route"] = _as_text(item.get("defaultRoute"))
                if item.get("wiringConfiguration") is not None:
                    cur["wiring_configuration"] = item.get("wiringConfiguration")
                fval = item.get("functionalValDetails")
                if isinstance(fval, dict):
                    if fval.get("state") is not None:
                        cur["functional_validation_state"] = _as_int(fval.get("state"))
                    if fval.get("lastUpdatedTimestamp") is not None:
                        cur["functional_validation_updated_at"] = _sec(
                            fval.get("lastUpdatedTimestamp")
                        )
                gateway_connectivity = item.get("gatewayConnectivityDetails")
                if isinstance(gateway_connectivity, list):
                    cur["gateway_connection_count"] = len(gateway_connectivity)
                    connected_count = 0
                    gateway_details: list[dict[str, int]] = []
                    gateway_last_connection_at: int | None = None
                    for gateway in gateway_connectivity:
                        if not isinstance(gateway, dict):
                            continue
                        gateway_status = _as_int(gateway.get("gwConnStatus"))
                        gateway_failure_reason = _as_int(
                            gateway.get("gwConnFailureReason")
                        )
                        gateway_last_conn_time = _sec(gateway.get("lastConnTime"))
                        if gateway_status == 0:
                            connected_count += 1
                        detail: dict[str, int] = {}
                        if gateway_status is not None:
                            detail["status"] = gateway_status
                        if gateway_failure_reason is not None:
                            detail["failure_reason"] = gateway_failure_reason
                        if gateway_last_conn_time is not None:
                            detail["last_connection_at"] = gateway_last_conn_time
                            if (
                                gateway_last_connection_at is None
                                or gateway_last_conn_time > gateway_last_connection_at
                            ):
                                gateway_last_connection_at = gateway_last_conn_time
                        if detail:
                            gateway_details.append(detail)
                    cur["gateway_connected_count"] = connected_count
                    if gateway_details:
                        cur["gateway_connectivity_details"] = gateway_details
                    if gateway_last_connection_at is not None:
                        cur["gateway_last_connection_at"] = gateway_last_connection_at
                # Capture operating voltage for better power estimation
                ov = item.get("operatingVoltage")
                if ov is not None:
                    try:
                        self._operating_v[sn] = int(round(float(str(ov))))
                    except Exception:
                        pass
                # Expose operating voltage in the mapped data when known
                if self._operating_v.get(sn) is not None:
                    cur["operating_v"] = self._operating_v.get(sn)
                # Lifetime energy for Energy Dashboard (kWh) with glitch guard
                if item.get("lifeTimeConsumption") is not None:
                    filtered = self.energy._apply_lifetime_guard(
                        sn,
                        item.get("lifeTimeConsumption"),
                        prev_sn,
                    )
                    if filtered is not None:
                        cur["lifetime_kwh"] = filtered
                elif isinstance(prev_sn, dict):
                    previous_lifetime_kwh = _power_as_float(prev_sn.get("lifetime_kwh"))
                    if previous_lifetime_kwh is not None:
                        cur["lifetime_kwh"] = previous_lifetime_kwh
                for key_src, key_dst in (
                    ("firmwareVersion", "firmware_version"),
                    ("systemVersion", "system_version"),
                    ("applicationVersion", "application_version"),
                    ("processorBoardVersion", "processor_board_version"),
                    ("powerBoardVersion", "power_board_version"),
                    ("kernelVersion", "kernel_version"),
                    ("bootloaderVersion", "bootloader_version"),
                ):
                    val = item.get(key_src)
                    if val is not None and key_dst not in cur:
                        cur[key_dst] = val
                # Optional device metadata if provided by summary v2
                for key_src, key_dst in (
                    ("firmwareVersion", "sw_version"),
                    ("systemVersion", "sw_version"),
                    ("applicationVersion", "sw_version"),
                    ("softwareVersion", "sw_version"),
                    ("processorBoardVersion", "hw_version"),
                    ("powerBoardVersion", "hw_version"),
                    ("hwVersion", "hw_version"),
                    ("hardwareVersion", "hw_version"),
                    ("modelId", "model_id"),
                    ("sku", "model_id"),
                    ("model", "model_name"),
                    ("modelName", "model_name"),
                    ("partNumber", "part_number"),
                    ("kernelVersion", "kernel_version"),
                    ("bootloaderVersion", "bootloader_version"),
                ):
                    val = item.get(key_src)
                    if val is not None and key_dst not in cur:
                        cur[key_dst] = val
                # Prefer displayName from summary v2 for user-facing names
                if item.get("displayName"):
                    cur["display_name"] = str(item.get("displayName"))
            self._seed_nominal_voltage_option_from_api()

        for sn, cur in out.items():
            previous_entry = prev_data.get(sn) if isinstance(prev_data, dict) else None
            snapshot = build_evse_power_snapshot(
                cur,
                previous_entry if isinstance(previous_entry, dict) else None,
                self.evse_state._evse_power_snapshots.get(sn, {}),
                self.nominal_voltage,
            )
            self.evse_state._evse_power_snapshots[sn] = snapshot
            cur.update(snapshot)

        # Attach session history using cached data, deferring expensive fetches when possible
        sessions_start = time.monotonic()
        try:
            day_ref = dt_util.now()
        except Exception:
            day_ref = datetime.now(tz=_tz.utc)
        try:
            day_local_default = dt_util.as_local(day_ref)
        except Exception:
            if day_ref.tzinfo is None:
                day_ref = day_ref.replace(tzinfo=_tz.utc)
            day_local_default = dt_util.as_local(day_ref)

        now_mono = time.monotonic()
        immediate_by_day: dict[tuple[str, float | None], list[str]] = {}
        background_by_day: dict[tuple[str, float | None], list[str]] = {}
        day_locals: dict[str, datetime] = {}
        for sn, cur in out.items():
            history_day = self._session_history_day(cur, day_local_default)
            day_key = history_day.strftime("%Y-%m-%d")
            day_locals.setdefault(day_key, history_day)
            view = self.session_history.get_cache_view(sn, day_key, now_mono)
            sessions_cached = view.sessions or []
            cur["energy_today_sessions"] = sessions_cached
            cur["energy_today_sessions_kwh"] = self._sum_session_energy(sessions_cached)
            view_needs_refresh = bool(getattr(view, "needs_refresh", False))
            view_blocked = bool(getattr(view, "blocked", False))
            view_has_valid_cache = bool(getattr(view, "has_valid_cache", False))
            soft_ttl = self._session_history_soft_ttl(cur)
            hard_ttl = self._session_history_hard_ttl(cur)
            session_cache_ttl = float(
                self._session_history_cache_ttl
                or DEFAULT_SESSION_HISTORY_INTERVAL_MIN * 60
            )
            max_cache_age = soft_ttl if soft_ttl < session_cache_ttl else None
            cache_age_raw = getattr(view, "cache_age", None)
            cache_age = (
                float(cache_age_raw)
                if isinstance(cache_age_raw, Real)
                and not isinstance(cache_age_raw, bool)
                else None
            )
            needs_refresh = (
                cache_age >= soft_ttl
                if view_has_valid_cache and cache_age is not None
                else view_needs_refresh
            )
            if not needs_refresh or view_blocked:
                continue
            if not view_has_valid_cache:
                # Failed/no-cache lookups should retry on the session-history cadence,
                # not on every fast coordinator poll.
                if cache_age is not None and cache_age < session_cache_ttl:
                    continue
                if first_refresh:
                    background_by_day.setdefault((day_key, max_cache_age), []).append(
                        sn
                    )
                    continue
                immediate_by_day.setdefault((day_key, max_cache_age), []).append(sn)
                continue
            if cache_age is None:
                if first_refresh:
                    background_by_day.setdefault((day_key, max_cache_age), []).append(
                        sn
                    )
                    continue
                target = (
                    immediate_by_day
                    if self._session_history_prioritize_inline_refresh(cur)
                    else background_by_day
                )
                target.setdefault((day_key, max_cache_age), []).append(sn)
                continue
            if first_refresh:
                background_by_day.setdefault((day_key, max_cache_age), []).append(sn)
                continue
            # Refresh active or recently-ended sessions sooner in the background,
            # but still force an inline catch-up once cached data ages too far.
            if cache_age is None or cache_age >= hard_ttl:
                immediate_by_day.setdefault((day_key, max_cache_age), []).append(sn)
                continue
            background_by_day.setdefault((day_key, max_cache_age), []).append(sn)
        # Prune after day-keys are known so historical session-day entries in use
        # by current chargers are retained for normal TTL behavior.
        self._prune_runtime_caches(active_serials=out.keys(), keep_day_keys=day_locals)

        async def _async_fetch_immediate_session_updates(
            day_key: str,
            max_cache_age: float | None,
            serials: list[str],
        ) -> dict[str, list[dict]]:
            if max_cache_age is None:
                return await self._async_enrich_sessions(
                    serials,
                    day_locals.get(day_key, day_local_default),
                    in_background=False,
                )
            return await self._async_enrich_sessions(
                serials,
                day_locals.get(day_key, day_local_default),
                in_background=False,
                max_cache_age=max_cache_age,
            )

        immediate_items = tuple(immediate_by_day.items())
        if len(immediate_items) == 1:
            (day_key, max_cache_age), serials = immediate_items[0]
            immediate_updates = (
                await _async_fetch_immediate_session_updates(
                    day_key, max_cache_age, serials
                ),
            )
        elif immediate_items:
            tasks: list[asyncio.Task[dict[str, list[dict]]]] = []
            async with asyncio.TaskGroup() as task_group:
                for (day_key, max_cache_age), serials in immediate_items:
                    task = task_group.create_task(
                        _async_fetch_immediate_session_updates(
                            day_key, max_cache_age, serials
                        ),
                        name=f"{DOMAIN}_session_history_{day_key}",
                    )
                    tasks.append(task)
            immediate_updates = tuple(task.result() for task in tasks)
        else:
            immediate_updates = ()

        for updates in immediate_updates:
            for sn, sessions in updates.items():
                cur = out.get(sn)
                if cur is None:
                    continue
                cur["energy_today_sessions"] = sessions
                cur["energy_today_sessions_kwh"] = self._sum_session_energy(sessions)
        for (day_key, max_cache_age), serials in background_by_day.items():
            if max_cache_age is None:
                self._schedule_session_enrichment(
                    serials,
                    day_locals.get(day_key, day_local_default),
                )
            else:
                self._schedule_session_enrichment(
                    serials,
                    day_locals.get(day_key, day_local_default),
                    max_cache_age=max_cache_age,
                )
        phase_timings["sessions_s"] = round(time.monotonic() - sessions_start, 3)
        self._sync_session_history_issue()

        await self._async_run_post_session_refresh_pipeline(
            context,
            out,
            day_local_default,
        )
        self._apply_refresh_polling_interval(polling_state)

        self._finish_refresh_pipeline(context)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Coordinator refresh timings for site %s: %s",
                redact_site_id(self.site_id),
                self._phase_timings,
            )

        return out

    def _sync_desired_charging(self, data: dict[str, dict]) -> None:
        self.evse_runtime.sync_desired_charging(data)

    async def _async_auto_resume(self, sn: str, snapshot: dict | None = None) -> None:
        await self.evse_runtime.async_auto_resume(sn, snapshot)

    def _determine_polling_state(self, data: dict[str, dict]) -> dict[str, object]:
        return self.evse_runtime.determine_polling_state(data)

    def _has_embedded_charge_mode(self, obj: dict) -> bool:
        """Check whether the status payload already exposes a charge mode."""
        if not isinstance(obj, dict):
            return False
        for key in ("mode", "chargeMode", "chargingMode", "charge_mode"):
            val = obj.get(key)
            if val is not None:
                return True
        sch = obj.get("sch_d")
        if isinstance(sch, dict):
            if sch.get("mode") or sch.get("status"):
                return True
            info = sch.get("info")
            if isinstance(info, list):
                for entry in info:
                    if isinstance(entry, dict) and (
                        entry.get("type") or entry.get("mode") or entry.get("status")
                    ):
                        return True
        return False

    @staticmethod
    def _coerce_utc_datetime(value: object) -> datetime | None:
        """Return a timezone-aware UTC datetime when the value is parseable."""

        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=_tz.utc)
            return value.astimezone(_tz.utc)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        if not text:
            return None
        parsed = dt_util.parse_datetime(text)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_tz.utc)
        return parsed.astimezone(_tz.utc)

    @staticmethod
    def _format_auth_blocked_until(value: datetime | None) -> str | None:
        """Return a compact UTC timestamp for repair-issue placeholders."""

        if not isinstance(value, datetime):
            return None
        try:
            return value.astimezone(_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:  # noqa: BLE001
            try:
                return value.isoformat()
            except Exception:
                return str(value)

    def _persist_auth_block_state(self) -> None:
        """Persist the long Enphase auth block metadata on the config entry."""

        config_entry = getattr(self, "config_entry", None)
        if not config_entry:
            return
        merged = dict(config_entry.data)
        if self._auth_blocked_until_utc is None:
            merged.pop(CONF_AUTH_BLOCKED_UNTIL, None)
        else:
            merged[CONF_AUTH_BLOCKED_UNTIL] = self._auth_blocked_until_utc.isoformat()
        if self._auth_block_reason:
            merged[CONF_AUTH_BLOCK_REASON] = self._auth_block_reason
        else:
            merged.pop(CONF_AUTH_BLOCK_REASON, None)
        self._async_update_config_entry_data_internal(
            merged,
            reason="auth_block",
        )

    def _persist_auth_refresh_suspension_state(self) -> None:
        """Persist stored-credential auto-refresh suspension metadata."""

        config_entry = getattr(self, "config_entry", None)
        if not config_entry:
            return
        merged = dict(config_entry.data)
        if self._auth_refresh_suspended_until_utc is None:
            merged.pop(CONF_AUTH_REFRESH_SUSPENDED_UNTIL, None)
        else:
            merged[CONF_AUTH_REFRESH_SUSPENDED_UNTIL] = (
                self._auth_refresh_suspended_until_utc.isoformat()
            )
        self._async_update_config_entry_data_internal(
            merged,
            reason="auth_refresh_suspension",
        )

    def _persist_hems_auth_circuit_state(self) -> None:
        """Persist optional HEMS auth circuit metadata on the config entry."""

        config_entry = getattr(self, "config_entry", None)
        if not config_entry:
            return
        merged = dict(config_entry.data)
        updates = {
            CONF_HEMS_AUTH_BACKOFF_UNTIL: (
                self._hems_auth_backoff_ends_utc.isoformat()
                if isinstance(self._hems_auth_backoff_ends_utc, datetime)
                else None
            ),
            CONF_HEMS_AUTH_FAILURE_COUNT: (
                self._hems_auth_failure_count
                if int(getattr(self, "_hems_auth_failure_count", 0) or 0) > 0
                else None
            ),
            CONF_HEMS_AUTH_LAST_FAILURE_UTC: (
                self._hems_auth_last_failure_utc.isoformat()
                if isinstance(self._hems_auth_last_failure_utc, datetime)
                else None
            ),
            CONF_HEMS_AUTH_LAST_SUCCESS_UTC: (
                self._hems_auth_last_success_utc.isoformat()
                if isinstance(self._hems_auth_last_success_utc, datetime)
                else None
            ),
            CONF_HEMS_AUTH_LAST_ENDPOINT: self._hems_auth_last_endpoint,
            CONF_HEMS_AUTH_LAST_STATUS: self._hems_auth_last_status,
            CONF_HEMS_AUTH_LAST_REASON: self._hems_auth_last_reason,
        }
        for key, value in updates.items():
            if value in (None, ""):
                merged.pop(key, None)
            else:
                merged[key] = value
        self._async_update_config_entry_data_internal(
            merged,
            reason="hems_auth_circuit",
        )

    def _async_update_config_entry_data_internal(
        self,
        merged: dict[str, object],
        *,
        reason: str,
    ) -> bool:
        """Persist coordinator-owned config-entry data without reloading the entry."""

        config_entry = getattr(self, "config_entry", None)
        if not config_entry:
            return False
        old_data = dict(config_entry.data)
        changed_keys = sorted(
            key
            for key in set(old_data) | set(merged)
            if old_data.get(key) != merged.get(key)
        )
        if not changed_keys:
            return False
        self._mark_internal_config_entry_data_update()
        try:
            changed = self.hass.config_entries.async_update_entry(
                config_entry, data=merged
            )
        except Exception:
            self._unmark_internal_config_entry_data_update()
            raise
        if not changed:
            self._unmark_internal_config_entry_data_update()
            return False
        _LOGGER.debug(
            "Persisted internal Enphase config-entry state for site %s (%s): keys=%s",
            redact_site_id(str(getattr(self, "site_id", ""))),
            reason,
            changed_keys,
        )
        return True

    def _mark_internal_config_entry_data_update(self) -> None:
        """Avoid reloading this entry for coordinator-owned data persistence."""

        config_entry = getattr(self, "config_entry", None)
        runtime_data = getattr(config_entry, "runtime_data", None)
        if hasattr(runtime_data, "reload_suppression_count"):
            runtime_data.reload_suppression_count = (
                int(getattr(runtime_data, "reload_suppression_count", 0)) + 1
            )

    def _unmark_internal_config_entry_data_update(self) -> None:
        """Undo a reload-suppression mark when Home Assistant made no data change."""

        config_entry = getattr(self, "config_entry", None)
        runtime_data = getattr(config_entry, "runtime_data", None)
        if hasattr(runtime_data, "reload_suppression_count"):
            runtime_data.reload_suppression_count = max(
                0,
                int(getattr(runtime_data, "reload_suppression_count", 0)) - 1,
            )

    def _clear_auth_refresh_rejection_state(self) -> None:
        """Clear the short rejected-refresh streak and cooldown state."""

        self._auth_refresh_rejected_count = 0
        self._auth_refresh_rejected_until = None
        self._auth_refresh_rejected_ends_utc = None

    def _clear_auth_refresh_rejection_state_if_unchanged(
        self, context: RefreshPipelineContext
    ) -> None:
        """Clear stale refresh rejections only when this refresh added none."""

        if self._auth_refresh_suspended_active():
            return
        if (
            int(getattr(self, "_auth_refresh_rejected_count", 0))
            != context.auth_refresh_rejected_count_at_start
        ):
            return
        self._clear_auth_refresh_rejection_state()

    def _clear_auth_refresh_suspension(self, *, persist: bool = True) -> None:
        """Clear stored-credential auto-refresh suspension state."""

        had_state = bool(getattr(self, "_auth_refresh_suspended_until_utc", None))
        self._clear_auth_refresh_rejection_state()
        self._auth_refresh_suspended_until_utc = None
        if persist and had_state:
            self._persist_auth_refresh_suspension_state()

    def _auth_refresh_suspended_active(self) -> bool:
        """Return True while stored-credential auto-refresh is suspended."""

        suspended_until = getattr(self, "_auth_refresh_suspended_until_utc", None)
        if not isinstance(suspended_until, datetime):
            return False
        if suspended_until > dt_util.utcnow():
            return True
        self._clear_auth_refresh_suspension()
        return False

    def _note_auth_refresh_suspended(self, *, suspended_until: datetime) -> None:
        """Persist a long suspension after repeated stored-credential rejection."""

        self._auth_refresh_suspended_until_utc = suspended_until.astimezone(_tz.utc)
        self._persist_auth_refresh_suspension_state()
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.create_reauth_issue()

    def _clear_auth_repair_issues_on_success(self) -> None:
        """Clear auth repairs unless auto-refresh suspension should keep reauth visible."""

        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is None:
            return
        if self._auth_refresh_suspended_active():
            diagnostics.clear_auth_block_issue()
            return
        diagnostics.clear_reauth_issue()

    def _clear_auth_block(self, *, persist: bool = True) -> None:
        """Clear the Enphase auth-block state and its repair issue."""

        had_state = bool(
            getattr(self, "_auth_blocked_until_utc", None)
            or getattr(self, "_auth_block_reason", None)
        )
        self._auth_blocked_until_utc = None
        self._auth_block_reason = None
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.clear_auth_block_issue()
        if persist and had_state:
            self._persist_auth_block_state()

    def _auth_block_active(self) -> bool:
        """Return True while Enphase appears to be blocking API authentication."""

        blocked_until = getattr(self, "_auth_blocked_until_utc", None)
        if not isinstance(blocked_until, datetime):
            return False
        if blocked_until > dt_util.utcnow():
            return True
        self._clear_auth_block()
        return False

    def _note_auth_blocked(
        self,
        *,
        blocked_until: datetime,
        reason: str,
    ) -> None:
        """Persist a long auth block after Enphase serves the browser login wall."""

        self._auth_blocked_until_utc = blocked_until.astimezone(_tz.utc)
        self._auth_block_reason = str(reason).strip() or None
        self._last_error = "auth_blocked"
        self._persist_auth_block_state()
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.create_auth_block_issue()

    def _activate_auth_block_from_login_wall(self, err: Unauthorized) -> bool:
        """Persist a long auth block when a login wall follows a rejected refresh."""

        if not isinstance(err, EnphaseLoginWallUnauthorized):
            return False
        if not self._auth_refresh_rejected_active():
            return False
        if self._auth_block_active():
            diagnostics = getattr(self, "diagnostics", None)
            if diagnostics is not None:
                diagnostics.create_auth_block_issue()
            return True
        self.auth_refresh_runtime.note_login_wall_block(
            reason="login_wall_after_refresh_reject"
        )
        _LOGGER.warning(
            "Enphase login wall detected for site %s while auth refresh cooldown was active; blocking automatic retries for %s seconds",
            redact_site_id(self.site_id),
            int(AUTH_BLOCKED_COOLDOWN_S),
        )
        return True

    def _hems_auth_backoff_remaining_s(self) -> int | None:
        """Return remaining optional HEMS auth backoff seconds."""

        backoff_until = getattr(self, "_hems_auth_backoff_until", None)
        if not isinstance(backoff_until, (int, float)):
            return None
        remaining = float(backoff_until) - time.monotonic()
        if remaining <= 0:
            self._clear_hems_auth_circuit(persist=True, reset_failure_count=False)
            return None
        return max(1, int(remaining + 0.999))

    def _hems_auth_circuit_active(self) -> bool:
        """Return True while optional HEMS/Heat Pump polling is paused."""

        return self._hems_auth_backoff_remaining_s() is not None

    def _hems_auth_backoff_delay_s(self) -> float:
        """Return the next HEMS auth backoff duration."""

        count = max(1, int(getattr(self, "_hems_auth_failure_count", 0) or 0))
        index = min(count - 1, len(HEMS_AUTH_BACKOFF_STEPS_S) - 1)
        return float(HEMS_AUTH_BACKOFF_STEPS_S[index])

    def _hems_auth_exception_details(
        self, err: BaseException
    ) -> tuple[int | None, str] | None:
        """Return auth-like HEMS failure details for errors that should isolate HEMS."""

        if isinstance(err, EnphaseLoginWallUnauthorized):
            return getattr(err, "status", 200), "login_wall"
        if isinstance(err, Unauthorized):
            return getattr(err, "status", 401), "unauthorized"
        if isinstance(err, aiohttp.ClientResponseError) and err.status in (401, 403):
            return int(err.status), "forbidden" if err.status == 403 else "unauthorized"
        return None

    def _hems_auth_repair_context_available(self) -> bool:
        """Return True when HEMS auth failures should be surfaced to the user."""

        selected = getattr(self, "_selected_type_keys", None)
        if isinstance(selected, (set, list, tuple)) and selected:
            return any(normalize_type_key(key) == "heatpump" for key in selected)
        inventory_view = getattr(self, "inventory_view", None)
        has_type = getattr(inventory_view, "has_type", None)
        if callable(has_type):
            try:
                if has_type("heatpump"):
                    return True
            except Exception:  # noqa: BLE001 - defensive diagnostics guard
                pass
        if bool(getattr(self, "_heatpump_known_present", False)):
            return True
        runtime = getattr(self, "heatpump_runtime", None)
        established = getattr(runtime, "heatpump_entities_established", None)
        if callable(established):
            try:
                return bool(established())
            except Exception:  # noqa: BLE001 - defensive diagnostics guard
                return False
        return False

    def _note_hems_auth_failure(
        self,
        err: BaseException,
        *,
        endpoint: str,
    ) -> bool:
        """Open the optional HEMS auth circuit for HEMS-only auth failures."""

        details = self._hems_auth_exception_details(err)
        if details is None:
            return False
        status, reason = details
        self._hems_auth_failure_count = (
            int(getattr(self, "_hems_auth_failure_count", 0) or 0) + 1
        )
        delay = self._hems_auth_backoff_delay_s()
        now = dt_util.utcnow()
        self._hems_auth_last_failure_utc = now
        self._hems_auth_last_endpoint = redact_text(
            endpoint,
            site_ids=(self.site_id,),
            max_length=256,
        )
        self._hems_auth_last_status = status
        self._hems_auth_last_reason = reason
        self._hems_auth_backoff_until = time.monotonic() + delay
        self._hems_auth_backoff_ends_utc = now + timedelta(seconds=delay)
        self._last_error = "hems_auth_degraded"
        self._persist_hems_auth_circuit_state()
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            repairs_enabled = bool(
                getattr(diagnostics, "degraded_service_repair_issues_enabled", True)
            )
            if self._hems_auth_repair_context_available() or not repairs_enabled:
                diagnostics.create_hems_auth_degraded_issue()
            else:
                clear_issue = getattr(
                    diagnostics, "clear_hems_auth_degraded_issue", None
                )
                if callable(clear_issue):
                    clear_issue()
        _LOGGER.warning(
            "Pausing optional HEMS polling for site %s after HEMS auth failure: endpoint=%s status=%s reason=%s failure_count=%s retry_at=%s",
            redact_site_id(self.site_id),
            self._hems_auth_last_endpoint,
            status,
            reason,
            self._hems_auth_failure_count,
            self._format_auth_blocked_until(self._hems_auth_backoff_ends_utc)
            or self._hems_auth_backoff_ends_utc,
        )
        return True

    def _note_hems_auth_success(self, *, endpoint: str | None = None) -> None:
        """Record successful optional HEMS polling and clear any HEMS auth circuit."""

        had_circuit = bool(
            getattr(self, "_hems_auth_backoff_until", None)
            or getattr(self, "_hems_auth_last_reason", None)
            or int(getattr(self, "_hems_auth_failure_count", 0) or 0)
        )
        self._clear_hems_auth_circuit(persist=False, reset_failure_count=True)
        if endpoint:
            self._hems_auth_last_endpoint = redact_text(
                endpoint,
                site_ids=(self.site_id,),
                max_length=256,
            )
        self._hems_auth_last_success_utc = dt_util.utcnow()
        if had_circuit:
            _LOGGER.info(
                "Optional HEMS polling recovered for site %s",
                redact_site_id(self.site_id),
            )
            self._persist_hems_auth_circuit_state()

    def _clear_hems_auth_circuit(
        self,
        *,
        persist: bool = True,
        reset_failure_count: bool = True,
    ) -> None:
        """Clear optional HEMS auth backoff state without touching global auth."""

        had_state = bool(
            getattr(self, "_hems_auth_backoff_until", None)
            or getattr(self, "_hems_auth_backoff_ends_utc", None)
            or getattr(self, "_hems_auth_last_reason", None)
            or getattr(self, "_hems_auth_last_status", None) is not None
            or getattr(self, "_hems_auth_last_failure_utc", None)
            or int(getattr(self, "_hems_auth_failure_count", 0) or 0)
        )
        self._hems_auth_backoff_until = None
        self._hems_auth_backoff_ends_utc = None
        if reset_failure_count:
            self._hems_auth_last_endpoint = None
            self._hems_auth_last_status = None
            self._hems_auth_last_reason = None
            self._hems_auth_last_failure_utc = None
            self._hems_auth_failure_count = 0
        diagnostics = getattr(self, "diagnostics", None)
        clear_issue = getattr(diagnostics, "clear_hems_auth_degraded_issue", None)
        if callable(clear_issue):
            clear_issue()
        if persist and had_state:
            self._persist_hems_auth_circuit_state()

    def _skip_hems_polling_due_to_auth_circuit(self, *, endpoint: str) -> bool:
        """Return True when a HEMS poll should fail fast due to active backoff."""

        remaining = self._hems_auth_backoff_remaining_s()
        if remaining is None:
            return False
        _LOGGER.debug(
            "Skipping optional HEMS polling for site %s while auth circuit is active: endpoint=%s remaining_s=%s",
            redact_site_id(self.site_id),
            redact_text(endpoint, site_ids=(self.site_id,), max_length=256),
            remaining,
        )
        return True

    def _hems_auth_manual_clear_retry_after_seconds(self) -> int | None:
        """Return remaining seconds for manual HEMS backoff clear throttling."""

        cooldown_until = getattr(self, "_hems_auth_manual_clear_until", None)
        if not isinstance(cooldown_until, (int, float)):
            return None
        remaining = float(cooldown_until) - time.monotonic()
        if remaining > 0:
            return max(1, int(remaining + 0.999))
        self._hems_auth_manual_clear_until = None
        return None

    async def async_clear_hems_auth_backoff(self) -> dict[str, object]:
        """Clear optional HEMS auth backoff without attempting password login."""

        retry_after = self._hems_auth_manual_clear_retry_after_seconds()
        if retry_after is not None:
            response: dict[str, object] = {
                "site_id": str(self.site_id),
                "success": False,
                "reason": "manual_clear_cooldown_active",
                "retry_after_seconds": retry_after,
            }
            if isinstance(self._hems_auth_backoff_ends_utc, datetime):
                response["hems_backoff_until"] = (
                    self._hems_auth_backoff_ends_utc.isoformat()
                )
            return response
        self._hems_auth_manual_clear_until = (
            time.monotonic() + HEMS_AUTH_MANUAL_CLEAR_COOLDOWN_S
        )
        was_active = self._hems_auth_circuit_active()
        backoff_until = (
            self._hems_auth_backoff_ends_utc.isoformat()
            if isinstance(self._hems_auth_backoff_ends_utc, datetime)
            else None
        )
        self._clear_hems_auth_circuit(persist=True, reset_failure_count=False)
        await self.async_request_refresh()
        response = {
            "site_id": str(self.site_id),
            "success": True,
            "reason": "cleared" if was_active else "not_active",
        }
        if backoff_until:
            response["hems_backoff_until"] = backoff_until
        return response

    def _blocked_auth_failure_message(self) -> str:
        """Return the user-facing auth-block failure message."""

        blocked_until = self._format_auth_blocked_until(self._auth_blocked_until_utc)
        if blocked_until:
            return (
                "Enphase authentication is temporarily blocked; wait until "
                f"{blocked_until} before automatic retries resume. Manual "
                "reauthentication can clear the block earlier if Enphase accepts it."
            )
        return "Enphase authentication is temporarily blocked; retry later."

    def _auth_block_retry_after_s(self) -> int | None:
        """Return remaining auth-block seconds for coordinator retry scheduling."""

        blocked_until = getattr(self, "_auth_blocked_until_utc", None)
        if not isinstance(blocked_until, datetime):
            return None
        remaining = (blocked_until - dt_util.utcnow()).total_seconds()
        if remaining <= 0:
            return None
        return max(1, int(remaining + 0.999))

    def _auth_block_update_failed(self) -> UpdateFailed:
        """Return a recoverable update failure while Enphase auth is blocked."""

        return UpdateFailed(
            self._blocked_auth_failure_message(),
            retry_after=self._auth_block_retry_after_s(),
        )

    async def _attempt_auto_refresh(self) -> bool:
        """Attempt to refresh authentication using stored credentials.

        Implementation lives on :class:`~custom_components.enphase_ev.auth_refresh_runtime.AuthRefreshRuntime`.
        """

        return await self.auth_refresh_runtime.attempt_auto_refresh()

    async def async_try_reauth_now(self) -> ManualAuthRefreshResult:
        """Attempt one manual stored-credential reauthentication."""

        return await self.auth_refresh_runtime.attempt_manual_refresh()

    def _clear_auth_refresh_task(self, task: asyncio.Task[bool]) -> None:
        """Clear the shared auth-refresh task once it completes (delegates to ``AuthRefreshRuntime``)."""

        self.auth_refresh_runtime.clear_auth_refresh_task(task)

    def _auth_refresh_rejected_active(self) -> bool:
        """Return True while stored-credential refresh is in cooldown (delegates to ``AuthRefreshRuntime``)."""

        return self.auth_refresh_runtime.auth_refresh_rejected_active()

    def _note_auth_refresh_rejected(self, message: str) -> None:
        """Start a cooldown after stored credentials are rejected (delegates to ``AuthRefreshRuntime``)."""

        self.auth_refresh_runtime.note_auth_refresh_rejected(message)

    def _auth_refresh_recent_success_active(self) -> bool:
        """Return True when a recent successful refresh can satisfy stale 401s (delegates to ``AuthRefreshRuntime``)."""

        return self.auth_refresh_runtime.auth_refresh_recent_success_active()

    async def _async_run_auto_refresh(self) -> bool:
        """Run one stored-credential refresh attempt for all concurrent waiters (delegates to ``AuthRefreshRuntime``)."""

        return await self.auth_refresh_runtime.async_run_auto_refresh()

    async def _handle_client_unauthorized(self) -> bool:
        """Handle client Unauthorized responses and retry when possible."""

        self._last_error = "unauthorized"
        self._unauth_errors += 1
        if await self._attempt_auto_refresh():
            self._unauth_errors = 0
            self.diagnostics.clear_reauth_issue()
            return True

        if self._auth_block_active():
            self.diagnostics.create_auth_block_issue()
            raise self._auth_block_update_failed()

        if self._unauth_errors >= 2:
            self.diagnostics.create_reauth_issue()

        raise ConfigEntryAuthFailed

    async def async_start_charging(
        self,
        sn: str,
        *,
        requested_amps: int | float | str | None = None,
        connector_id: int | None = 1,
        hold_seconds: float = 90.0,
        allow_unplugged: bool = False,
        fallback_amps: int | float | str | None = None,
    ) -> object:
        return await self.evse_runtime.async_start_charging(
            sn,
            requested_amps=requested_amps,
            connector_id=connector_id,
            hold_seconds=hold_seconds,
            allow_unplugged=allow_unplugged,
            fallback_amps=fallback_amps,
        )

    async def async_stop_charging(
        self,
        sn: str,
        *,
        hold_seconds: float = 90.0,
        fast_seconds: int = 60,
        allow_unplugged: bool = True,
    ) -> object:
        return await self.evse_runtime.async_stop_charging(
            sn,
            hold_seconds=hold_seconds,
            fast_seconds=fast_seconds,
            allow_unplugged=allow_unplugged,
        )

    def schedule_amp_restart(self, sn: str, delay: float = AMP_RESTART_DELAY_S) -> None:
        self.evse_runtime.schedule_amp_restart(sn, delay)

    async def _async_restart_after_amp_change(self, sn: str, delay: float) -> None:
        await self.evse_runtime.async_restart_after_amp_change(sn, delay)

    async def async_trigger_ocpp_message(self, sn: str, message: str) -> object:
        """Trigger an OCPP message with auth retry and fast follow-up poll."""
        sn_str = str(sn)

        result = await self.client.trigger_message(sn_str, message)
        self.kick_fast(60)
        await self.async_request_refresh()
        return result

    def _persist_tokens(self, tokens: AuthTokens) -> None:
        if not self.config_entry:
            return
        merged = dict(self.config_entry.data)
        updates = {
            CONF_COOKIE: tokens.cookie or "",
            CONF_EAUTH: tokens.access_token,
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_SESSION_ID: tokens.session_id,
            CONF_TOKEN_EXPIRES_AT: tokens.token_expires_at,
            CONF_AUTH_REFRESH_SUSPENDED_UNTIL: None,
            CONF_AUTH_BLOCKED_UNTIL: None,
            CONF_AUTH_BLOCK_REASON: None,
            CONF_HEMS_AUTH_BACKOFF_UNTIL: None,
            CONF_HEMS_AUTH_FAILURE_COUNT: None,
            CONF_HEMS_AUTH_LAST_FAILURE_UTC: None,
            CONF_HEMS_AUTH_LAST_ENDPOINT: None,
            CONF_HEMS_AUTH_LAST_STATUS: None,
            CONF_HEMS_AUTH_LAST_REASON: None,
        }
        for key, value in updates.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        self._clear_auth_refresh_rejection_state()
        self._auth_refresh_suspended_until_utc = None
        self._auth_blocked_until_utc = None
        self._auth_block_reason = None
        self._clear_hems_auth_circuit(persist=False, reset_failure_count=True)
        diagnostics = getattr(self, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.clear_reauth_issue()
        self._async_update_config_entry_data_internal(
            merged,
            reason="tokens",
        )

    def kick_fast(self, seconds: int = 60) -> None:
        self.evse_runtime.kick_fast(seconds)

    def _streaming_active(self) -> bool:
        return self.evse_runtime.streaming_active()

    def _clear_streaming_state(self) -> None:
        self.evse_runtime.clear_streaming_state()

    def _streaming_response_ok(self, response: object) -> bool:
        return EvseRuntime.streaming_response_ok(response)

    def _streaming_duration_s(self, response: object) -> float:
        return EvseRuntime.streaming_duration_s(response)

    async def async_start_streaming(
        self,
        *,
        manual: bool = False,
        serial: str | None = None,
        expected_state: bool | None = None,
    ) -> None:
        await self.evse_runtime.async_start_streaming(
            manual=manual,
            serial=serial,
            expected_state=expected_state,
        )

    async def async_stop_streaming(self, *, manual: bool = False) -> None:
        await self.evse_runtime.async_stop_streaming(manual=manual)

    def _schedule_stream_stop(self, *, force: bool = False) -> None:
        self.evse_runtime.schedule_stream_stop(force=force)

    def _record_actual_charging(self, sn: str, charging: bool | None) -> None:
        self.evse_runtime.record_actual_charging(sn, charging)

    def set_charging_expectation(
        self,
        sn: str,
        should_charge: bool,
        hold_for: float = 90.0,
    ) -> None:
        self.evse_runtime.set_charging_expectation(sn, should_charge, hold_for)

    def _slow_interval_floor(self) -> int:
        return self.evse_runtime.slow_interval_floor()

    def _clear_backoff_timer(self) -> None:
        if self._backoff_cancel:
            try:
                self._backoff_cancel()
            except Exception:
                pass
            self._backoff_cancel = None
        self.backoff_ends_utc = None

    def _scheduler_backoff_active(self) -> bool:
        """Return True when scheduler requests are in backoff."""
        backoff_until = getattr(self, "_scheduler_backoff_until", None)
        return bool(backoff_until and time.monotonic() < backoff_until)

    def scheduler_backoff_active(self) -> bool:
        """Public wrapper for scheduler backoff state."""

        return self._scheduler_backoff_active()

    def _scheduler_backoff_delay(self) -> float:
        # Scheduler outages are usually service-side and transient. Tie the
        # retry floor to the slow poll interval so repair issues remain visible
        # without hammering the optional scheduler service.
        slow_floor = float(self._slow_interval_floor())
        backoff_multiplier = 2 ** min(self._scheduler_failures - 1, 3)
        return max(30.0, min(600.0, slow_floor * backoff_multiplier))

    @property
    def scheduler_available(self) -> bool:
        """Return True when scheduler-dependent features are usable."""
        return bool(
            getattr(self, "_scheduler_available", True)
            and not self._scheduler_backoff_active()
        )

    @property
    def scheduler_last_error(self) -> str | None:
        return getattr(self, "_scheduler_last_error", None)

    def _mark_scheduler_available(self) -> None:
        if getattr(self, "_scheduler_available", True) and not getattr(
            self, "_scheduler_issue_reported", False
        ):
            return
        self._scheduler_available = True
        self._scheduler_failures = 0
        self._scheduler_last_error = None
        self._scheduler_last_failure_utc = None
        self._scheduler_backoff_until = None
        self._scheduler_backoff_ends_utc = None
        self.diagnostics.clear_scheduler_issue()

    def mark_scheduler_available(self) -> None:
        """Public wrapper used by entities and helper managers."""

        self._mark_scheduler_available()

    def _note_scheduler_unavailable(
        self,
        err: Exception | str | None = None,
        *,
        status: int | None = None,
        raw_payload: str | None = None,
    ) -> None:
        """Record scheduler outage and raise a repair issue."""
        reason = redact_text(err, site_ids=(self.site_id,)) if err else ""
        if not reason:
            reason = "Scheduler unavailable"
        self._scheduler_available = False
        self._scheduler_failures += 1
        self._scheduler_last_error = reason
        self._scheduler_last_failure_utc = dt_util.utcnow()
        delay = self._scheduler_backoff_delay()
        self._scheduler_backoff_until = time.monotonic() + delay
        try:
            self._scheduler_backoff_ends_utc = dt_util.utcnow() + timedelta(
                seconds=delay
            )
        except Exception:
            self._scheduler_backoff_ends_utc = None
        self._last_error = reason
        self.last_failure_utc = self._scheduler_last_failure_utc
        self.last_failure_status = status
        self.last_failure_description = "Scheduler unavailable"
        self.last_failure_response = (
            redact_text(raw_payload, site_ids=(self.site_id,))
            if raw_payload
            else reason
        )
        self.last_failure_source = "scheduler"
        self.diagnostics.report_scheduler_issue()

    def note_scheduler_unavailable(
        self,
        err: Exception | str | None = None,
        *,
        status: int | None = None,
        raw_payload: str | None = None,
    ) -> None:
        """Public wrapper used by entities and helper managers."""

        self._note_scheduler_unavailable(err, status=status, raw_payload=raw_payload)

    def _auth_settings_backoff_active(self) -> bool:
        """Return True when auth settings requests are in backoff."""
        backoff_until = getattr(self, "_auth_settings_backoff_until", None)
        return bool(backoff_until and time.monotonic() < backoff_until)

    def _auth_settings_backoff_delay(self) -> float:
        # Auth settings share the EVSE control plane, so use the same bounded
        # exponential shape as scheduler backoff when the optional endpoint is
        # unavailable.
        slow_floor = float(self._slow_interval_floor())
        backoff_multiplier = 2 ** min(self._auth_settings_failures - 1, 3)
        return max(30.0, min(600.0, slow_floor * backoff_multiplier))

    @property
    def auth_settings_available(self) -> bool:
        """Return True when auth settings features are usable."""
        return bool(
            getattr(self, "_auth_settings_available", True)
            and not self._auth_settings_backoff_active()
        )

    @property
    def auth_settings_last_error(self) -> str | None:
        return getattr(self, "_auth_settings_last_error", None)

    @staticmethod
    def _normalize_battery_profile_key(value: object) -> str | None:
        return BatteryRuntime.normalize_battery_profile_key(value)

    def _battery_profile_label(self, profile: str | None) -> str | None:
        return translated_battery_profile_label(profile, hass=self.hass)

    @staticmethod
    def _normalize_battery_sub_type(value: object) -> str | None:
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return normalized or None

    @staticmethod
    def normalize_battery_sub_type(value: object) -> str | None:
        return EnphaseCoordinator._normalize_battery_sub_type(value)

    @staticmethod
    def _coerce_optional_int(value: object) -> int | None:
        return helper_coerce_optional_int(value)

    @staticmethod
    def coerce_optional_int(value: object) -> int | None:
        return EnphaseCoordinator._coerce_optional_int(value)

    @staticmethod
    def _coerce_optional_float(value: object) -> float | None:
        return coerce_optional_float(value)

    @staticmethod
    def _coerce_optional_kwh(value: object) -> float | None:
        coerced = EnphaseCoordinator._coerce_optional_float(value)
        if coerced is None:
            return None
        try:
            return round(coerced, 2)
        except Exception:  # noqa: BLE001
            return coerced

    @staticmethod
    def _coerce_optional_text(value: object) -> str | None:
        return coerce_optional_text(value)

    @staticmethod
    def coerce_optional_text(value: object) -> str | None:
        return EnphaseCoordinator._coerce_optional_text(value)

    @staticmethod
    def _normalize_battery_grid_mode(value: object) -> str | None:
        if value is None:
            return None
        try:
            raw = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        return raw

    @staticmethod
    def _battery_grid_mode_key(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower().replace("-", "").replace("_", "")
        except Exception:  # noqa: BLE001
            return None
        return normalized or None

    def _battery_grid_mode_label(self, mode: str | None) -> str | None:
        return battery_grid_mode_label(mode, hass=self.hass)

    @staticmethod
    def _normalize_minutes_of_day(value: object) -> int | None:
        if value is None:
            return None
        try:
            minutes = int(str(value).strip())
        except Exception:  # noqa: BLE001
            return None
        if minutes < 0 or minutes >= 24 * 60:
            return None
        return minutes

    @staticmethod
    def normalize_minutes_of_day(value: object) -> int | None:
        return EnphaseCoordinator._normalize_minutes_of_day(value)

    @staticmethod
    def _minutes_of_day_to_time(value: int | None) -> dt_time | None:
        if value is None:
            return None
        normalized = EnphaseCoordinator._normalize_minutes_of_day(value)
        if normalized is None:
            return None
        hours = normalized // 60
        minutes = normalized % 60
        return dt_time(hour=hours, minute=minutes)

    @staticmethod
    def _time_to_minutes_of_day(value: dt_time | None) -> int | None:
        if value is None:
            return None
        try:
            return int(value.hour) * 60 + int(value.minute)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def time_to_minutes_of_day(value: dt_time | None) -> int | None:
        return EnphaseCoordinator._time_to_minutes_of_day(value)

    @staticmethod
    def _redact_battery_payload(value: object) -> object:
        return redact_battery_payload(value)

    def redact_battery_payload(self, value: object) -> object:
        return self._redact_battery_payload(value)

    @property
    def battery_pending_age_seconds(self) -> int | None:
        requested_at = getattr(self, "_battery_pending_requested_at", None)
        if requested_at is None:
            return None
        try:
            now = dt_util.utcnow()
            age = int((now - requested_at).total_seconds())
        except Exception:
            return None
        return age if age >= 0 else 0

    @property
    def battery_profile(self) -> str | None:
        return getattr(self, "_battery_profile", None)

    @property
    def battery_live_profile(self) -> str | None:
        return getattr(self, "_battery_live_profile", None)

    @property
    def battery_effective_profile(self) -> str | None:
        optimistic_profile = self.battery_runtime.optimistic_battery_profile()
        if optimistic_profile is not None:
            return optimistic_profile
        return getattr(self, "_battery_profile", None) or self.battery_live_profile

    @property
    def battery_profile_pending(self) -> bool:
        return getattr(self, "_battery_pending_profile", None) is not None

    @property
    def battery_pending_requested_at(self) -> datetime | None:
        return getattr(self, "_battery_pending_requested_at", None)

    @property
    def battery_effective_backup_percentage(self) -> int | None:
        optimistic_reserve = self.battery_runtime.optimistic_battery_reserve()
        if optimistic_reserve is not None:
            return optimistic_reserve
        return getattr(self, "_battery_backup_percentage", None)

    @property
    def battery_effective_operation_mode_sub_type(self) -> str | None:
        optimistic_sub_type = self.battery_runtime.optimistic_battery_sub_type()
        if optimistic_sub_type is not None:
            return optimistic_sub_type
        return getattr(self, "_battery_operation_mode_sub_type", None)

    @property
    def battery_selected_profile(self) -> str | None:
        return getattr(self, "_battery_pending_profile", None) or (
            self.battery_effective_profile
        )

    @property
    def battery_selected_backup_percentage(self) -> int | None:
        return (
            getattr(self, "_battery_pending_reserve", None)
            if getattr(self, "_battery_pending_reserve", None) is not None
            else self.battery_effective_backup_percentage
        )

    @property
    def battery_selected_operation_mode_sub_type(self) -> str | None:
        return (
            getattr(self, "_battery_pending_sub_type", None)
            if getattr(self, "_battery_pending_profile", None)
            in {"cost_savings", "ai_optimisation"}
            else self.battery_effective_operation_mode_sub_type
        )

    @property
    def battery_pending_profile(self) -> str | None:
        return getattr(self, "_battery_pending_profile", None)

    @property
    def battery_pending_backup_percentage(self) -> int | None:
        return getattr(self, "_battery_pending_reserve", None)

    @property
    def battery_pending_operation_mode_sub_type(self) -> str | None:
        return getattr(self, "_battery_pending_sub_type", None)

    @property
    def battery_has_encharge(self) -> bool | None:
        return getattr(self, "_battery_has_encharge", None)

    @property
    def battery_has_acb(self) -> bool | None:
        return getattr(self, "_battery_has_acb", None)

    @property
    def battery_is_charging_modes_enabled(self) -> bool | None:
        return getattr(self, "_battery_is_charging_modes_enabled", None)

    @property
    def battery_show_battery_backup_percentage(self) -> bool | None:
        return getattr(self, "_battery_show_battery_backup_percentage", None)

    @property
    def battery_is_emea(self) -> bool | None:
        return getattr(self, "_battery_is_emea", None)

    @property
    def battery_show_storm_guard(self) -> bool | None:
        return getattr(self, "_battery_show_storm_guard", None)

    @property
    def battery_show_production(self) -> bool | None:
        return getattr(self, "_battery_show_production", None)

    @property
    def battery_show_consumption(self) -> bool | None:
        return getattr(self, "_battery_show_consumption", None)

    @property
    def battery_has_enpower(self) -> bool | None:
        return getattr(self, "_battery_has_enpower", None)

    @property
    def battery_country_code(self) -> str | None:
        return getattr(self, "_battery_country_code", None)

    @property
    def battery_region(self) -> str | None:
        return getattr(self, "_battery_region", None)

    @property
    def battery_locale(self) -> str | None:
        return getattr(self, "_battery_locale", None)

    @property
    def battery_timezone(self) -> str | None:
        return getattr(self, "_battery_timezone", None)

    @property
    def battery_feature_details(self) -> dict[str, object]:
        details = getattr(self, "_battery_feature_details", None)
        if not isinstance(details, dict):
            return {}
        return dict(details)

    @property
    def battery_user_is_owner(self) -> bool | None:
        return getattr(self, "_battery_user_is_owner", None)

    @property
    def battery_user_is_installer(self) -> bool | None:
        return getattr(self, "_battery_user_is_installer", None)

    @property
    def battery_write_access_confirmed(self) -> bool:
        owner = self.battery_user_is_owner
        installer = self.battery_user_is_installer
        return owner is True or installer is True

    @staticmethod
    def _battery_control_to_dict(
        value: BatteryControlCapability | None,
    ) -> dict[str, bool | None] | None:
        if value is None:
            return None
        return {
            "show": value.show,
            "enabled": value.enabled,
            "locked": value.locked,
            "show_day_schedule": value.show_day_schedule,
            "schedule_supported": value.schedule_supported,
            "force_schedule_supported": value.force_schedule_supported,
            "force_schedule_opted": value.force_schedule_opted,
        }

    @staticmethod
    def _battery_control_field(
        value: BatteryControlCapability | None, field_name: str
    ) -> bool | None:
        if value is None:
            return None
        return getattr(value, field_name)

    @property
    def battery_site_status_code(self) -> str | None:
        return getattr(self, "_battery_site_status_code", None)

    @property
    def battery_site_status_text(self) -> str | None:
        return getattr(self, "_battery_site_status_text", None)

    @property
    def battery_site_status_severity(self) -> str | None:
        return getattr(self, "_battery_site_status_severity", None)

    @property
    def battery_aggregate_charge_pct(self) -> float | None:
        value = getattr(self, "_battery_aggregate_charge_pct", None)
        if value is None:
            return None
        try:
            return float(value)
        except Exception:  # noqa: BLE001
            return None

    @property
    def battery_aggregate_status(self) -> str | None:
        value = getattr(self, "_battery_aggregate_status", None)
        if value is None:
            return None
        try:
            text = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @property
    def battery_aggregate_status_details(self) -> dict[str, object]:
        details = getattr(self, "_battery_aggregate_status_details", None)
        if not isinstance(details, dict):
            return {}
        return dict(details)

    @property
    def battery_status_payload(self) -> dict[str, object] | None:
        payload = getattr(self, "_battery_status_payload", None)
        if not isinstance(payload, dict):
            return None
        return dict(payload)

    @property
    def battery_backup_history_events(self) -> list[dict[str, object]]:
        events = getattr(self, "_battery_backup_history_events", None)
        if not isinstance(events, list):
            return []
        out: list[dict[str, object]] = []
        for item in events:
            if isinstance(item, dict):
                out.append(dict(item))
        return out

    @property
    def battery_status_summary(self) -> dict[str, object]:
        details = dict(getattr(self, "_battery_aggregate_status_details", {}) or {})
        details["aggregate_charge_pct"] = self.battery_aggregate_charge_pct
        details["aggregate_status"] = self.battery_aggregate_status
        details["battery_order"] = self.iter_battery_serials()
        return details

    @property
    def ac_battery_aggregate_status(self) -> str | None:
        value = getattr(self, "_ac_battery_aggregate_status", None)
        if value is None:
            return None
        try:
            text = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @property
    def ac_battery_status_summary(self) -> dict[str, object]:
        details = dict(getattr(self, "_ac_battery_aggregate_status_details", {}) or {})
        details["aggregate_status"] = self.ac_battery_aggregate_status
        details["battery_order"] = self.iter_ac_battery_serials()
        details["power_w"] = getattr(self, "_ac_battery_power_w", None)
        return details

    @property
    def ac_battery_summary_sample_utc(self) -> datetime | None:
        value = getattr(self, "_ac_battery_summary_sample_utc", None)
        return value if isinstance(value, datetime) else None

    @property
    def ac_battery_selected_sleep_min_soc(self) -> int | None:
        value = getattr(self, "_ac_battery_selected_sleep_min_soc", None)
        return helper_coerce_optional_int(value)

    @property
    def ac_battery_sleep_state(self) -> str | None:
        value = getattr(self, "_ac_battery_sleep_state", None)
        if value is None:
            return None
        try:
            text = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        return text or None

    @property
    def ac_battery_control_pending(self) -> bool:
        return bool(getattr(self, "_ac_battery_control_pending", False))

    @property
    def battery_profile_option_keys(self) -> list[str]:
        options: list[str] = ["self-consumption"]
        if getattr(self, "_battery_show_savings_mode", None):
            options.append("cost_savings")
        if getattr(self, "_battery_show_ai_optimisation_mode", None):
            options.append("ai_optimisation")
        elif getattr(self, "_battery_show_ai_opti_savings_mode", None):
            options.append("ai_optimisation")
        if getattr(self, "_battery_show_full_backup", None):
            options.append("backup_only")
        current = getattr(self, "_battery_profile", None)
        if current:
            options.append(current)
        live = getattr(self, "_battery_live_profile", None)
        if live:
            options.append(live)
        pending = getattr(self, "_battery_pending_profile", None)
        if pending:
            options.append(pending)
        return [item for item in dict.fromkeys(options) if item]

    @property
    def battery_profile_option_labels(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        for key in self.battery_profile_option_keys:
            label = self._battery_profile_label(key)
            if label:
                labels[key] = label
        return labels

    @property
    def battery_profile_display(self) -> str | None:
        return self._battery_profile_label(self.battery_selected_profile)

    @property
    def battery_effective_profile_display(self) -> str | None:
        return self._battery_profile_label(self.battery_effective_profile)

    @property
    def battery_controls_available(self) -> bool:
        if getattr(self, "_battery_has_encharge", None) is False:
            return False
        if getattr(self, "_battery_profile", None) is not None:
            return True
        return bool(self.battery_profile_option_keys)

    @property
    def battery_system_task(self) -> bool | None:
        return getattr(self, "_battery_system_task", None)

    @property
    def battery_profile_selection_available(self) -> bool:
        if not self.battery_controls_available:
            return False
        if self.battery_system_task is True:
            return False
        owner = self.battery_user_is_owner
        installer = self.battery_user_is_installer
        return not (owner is False and installer is False)

    @property
    def savings_use_battery_after_peak(self) -> bool | None:
        profile = self.battery_selected_profile
        if profile != "cost_savings":
            return None
        subtype = self.battery_selected_operation_mode_sub_type
        if subtype is None:
            return False
        return subtype == SAVINGS_OPERATION_MODE_SUBTYPE

    @property
    def savings_use_battery_switch_available(self) -> bool:
        if not self.battery_profile_selection_available:
            return False
        if getattr(self, "_battery_show_savings_mode", None) is False:
            return False
        return self.battery_selected_profile == "cost_savings"

    @property
    def battery_reserve_editable(self) -> bool:
        if not self.battery_profile_selection_available:
            return False
        reserve_show = getattr(self, "_battery_show_battery_backup_percentage", None)
        cfg_show = self.battery_cfg_control_show
        is_emea = getattr(self, "_battery_is_emea", None)
        rbd_control = getattr(self, "_battery_rbd_control", None)
        rbd_show = self._battery_control_field(rbd_control, "show")
        if reserve_show is False:
            return False
        if self._battery_control_field(rbd_control, "locked") is True:
            return False
        if is_emea is True:
            if cfg_show is False:
                return False
            if cfg_show is None and rbd_show is False:
                return False
        else:
            if reserve_show is None:
                if rbd_show is False:
                    return False
                if rbd_show is None and cfg_show is False:
                    return False
            elif rbd_show is False and reserve_show is not True:
                return False
        profile = self.battery_selected_profile
        if profile is None:
            return False
        return profile != "backup_only"

    @property
    def battery_reserve_min(self) -> int:
        profile = self.battery_selected_profile
        if profile == "backup_only":
            return 100
        value = self._coerce_optional_int(
            getattr(self, "_battery_backup_percentage_min", None)
        )
        if value is None:
            return self._battery_min_soc_floor()
        return max(0, min(100, int(value)))

    @property
    def battery_reserve_max(self) -> int:
        profile = self.battery_selected_profile
        if profile == "backup_only":
            return 100
        value = self._coerce_optional_int(
            getattr(self, "_battery_backup_percentage_max", None)
        )
        if value is None:
            return 100
        return max(0, min(100, int(value)))

    @property
    def battery_grid_mode(self) -> str | None:
        return getattr(self, "_battery_grid_mode", None)

    @property
    def battery_mode_display(self) -> str | None:
        return self._battery_grid_mode_label(self.battery_grid_mode)

    @property
    def battery_charge_from_grid_allowed(self) -> bool | None:
        key = self._battery_grid_mode_key(self.battery_grid_mode)
        permissions = BATTERY_GRID_MODE_PERMISSIONS.get(key or "")
        if permissions is None:
            return None
        return permissions[0]

    @property
    def battery_discharge_to_grid_allowed(self) -> bool | None:
        key = self._battery_grid_mode_key(self.battery_grid_mode)
        permissions = BATTERY_GRID_MODE_PERMISSIONS.get(key or "")
        if permissions is None:
            return None
        return permissions[1]

    @property
    def charge_from_grid_control_available(self) -> bool:
        if getattr(self, "_battery_has_encharge", None) is False:
            return False
        if self.battery_system_task is True:
            return False
        # Prefer cfgControl.show (used by Enlighten app) over the
        # legacy hideChargeFromGrid flag which is unreliable on EMEA
        # sites. cfgControl.enabled appears to reflect current toggle
        # state on some homeowner payloads, so it is not authoritative
        # for control availability.
        cfg_show = self.battery_cfg_control_show
        if cfg_show is not None:
            if cfg_show is False:
                return False
        else:
            if getattr(self, "_battery_hide_charge_from_grid", None) is True:
                return False
        if self.battery_cfg_control_locked is True:
            return False
        owner = self.battery_user_is_owner
        installer = self.battery_user_is_installer
        if owner is False and installer is False:
            return False
        if getattr(self, "_battery_charge_from_grid", None) is not None:
            return True
        if (
            getattr(self, "_battery_charge_from_grid_schedule_enabled", None)
            is not None
        ):
            return True
        if getattr(self, "_battery_cfg_schedule_limit", None) is not None:
            return True
        if getattr(self, "_battery_cfg_schedule_id", None) is not None:
            return True
        begin = getattr(self, "_battery_charge_begin_time", None)
        end = getattr(self, "_battery_charge_end_time", None)
        return begin is not None and end is not None

    @property
    def battery_charge_from_grid_enabled(self) -> bool | None:
        return getattr(self, "_battery_charge_from_grid", None)

    @property
    def battery_charge_from_grid_schedule_enabled(self) -> bool | None:
        return getattr(self, "_battery_charge_from_grid_schedule_enabled", None)

    @property
    def charge_from_grid_schedule_supported(self) -> bool:
        if not self.charge_from_grid_control_available:
            return False
        show_day_schedule = self.battery_cfg_control_show_day_schedule
        if show_day_schedule is False:
            return False
        schedule_supported = self.battery_cfg_control_schedule_supported
        if schedule_supported is not None:
            return schedule_supported
        begin = getattr(self, "_battery_charge_begin_time", None)
        end = getattr(self, "_battery_charge_end_time", None)
        return begin is not None and end is not None

    @property
    def charge_from_grid_schedule_available(self) -> bool:
        return self.charge_from_grid_schedule_supported

    @property
    def charge_from_grid_force_schedule_supported(self) -> bool:
        if not self.charge_from_grid_control_available:
            return False
        force_schedule_supported = self.battery_cfg_control_force_schedule_supported
        if force_schedule_supported is not None:
            return force_schedule_supported
        if getattr(self, "_battery_cfg_schedule_limit", None) is not None:
            return True
        if getattr(self, "_battery_cfg_schedule_id", None) is not None:
            return True
        if (
            getattr(self, "_battery_charge_from_grid_schedule_enabled", None)
            is not None
        ):
            return True
        return True

    @property
    def charge_from_grid_force_schedule_available(self) -> bool:
        return self.charge_from_grid_force_schedule_supported

    def _battery_schedule_control_available(self, control: object) -> bool:
        if getattr(self, "_battery_has_encharge", None) is False:
            return False
        if self.battery_system_task is True:
            return False
        if self._battery_control_field(control, "show") is False:
            return False
        if self._battery_control_field(control, "locked") is True:
            return False
        owner = self.battery_user_is_owner
        installer = self.battery_user_is_installer
        if owner is False and installer is False:
            return False
        return True

    def _battery_schedule_supported(
        self,
        control: object,
        *,
        schedule_id: object,
        start_minutes: object,
        end_minutes: object,
        schedule_status: object = None,
    ) -> bool:
        if self._battery_schedule_control_available(control):
            show_day_schedule = self._battery_control_field(
                control, "show_day_schedule"
            )
            if show_day_schedule is False:
                return False
            schedule_supported = self._battery_control_field(
                control, "schedule_supported"
            )
            if schedule_supported is not None:
                return schedule_supported
        elif isinstance(schedule_status, str) and schedule_status.strip():
            return True
        else:
            return False
        if isinstance(schedule_status, str) and schedule_status.strip():
            return True
        return (
            schedule_id is not None
            and start_minutes is not None
            and end_minutes is not None
        )

    def _battery_schedule_available(
        self,
        control: object,
        *,
        schedule_id: object,
        start_minutes: object,
        end_minutes: object,
    ) -> bool:
        if not self._battery_schedule_supported(
            control,
            schedule_id=schedule_id,
            start_minutes=start_minutes,
            end_minutes=end_minutes,
        ):
            return False
        return (
            schedule_id is not None
            and start_minutes is not None
            and end_minutes is not None
        )

    @property
    def battery_charge_from_grid_start_time(self) -> dt_time | None:
        return self._minutes_of_day_to_time(
            getattr(self, "_battery_charge_begin_time", None)
        )

    @property
    def battery_charge_from_grid_end_time(self) -> dt_time | None:
        return self._minutes_of_day_to_time(
            getattr(self, "_battery_charge_end_time", None)
        )

    @property
    def battery_cfg_schedule_limit(self) -> int | None:
        """Return the CFG schedule charge limit (max SoC %) from /schedules."""
        return getattr(self, "_battery_cfg_schedule_limit", None)

    @property
    def battery_cfg_schedule_status(self) -> str | None:
        """Return the CFG schedule sync status (``pending`` or ``active``)."""
        return getattr(self, "_battery_cfg_schedule_status", None)

    @property
    def battery_cfg_schedule_pending(self) -> bool:
        """Return True if a CFG schedule change is pending Envoy sync."""
        return self.battery_cfg_schedule_status == "pending"

    @property
    def battery_settings_write_age_seconds(self) -> float | None:
        value = getattr(self, "_battery_settings_last_write_mono", None)
        if not isinstance(value, (int, float)):
            return None
        try:
            age = time.monotonic() - float(value)
        except Exception:  # noqa: BLE001
            return None
        return age if age >= 0 else 0.0

    @property
    def battery_settings_write_pending(self) -> bool:
        age = self.battery_settings_write_age_seconds
        return age is not None and age < FAST_TOGGLE_POLL_HOLD_S

    @property
    def discharge_to_grid_schedule_supported(self) -> bool:
        return self._battery_schedule_supported(
            getattr(self, "_battery_dtg_control", None),
            schedule_id=getattr(self, "_battery_dtg_schedule_id", None),
            start_minutes=getattr(self, "_battery_dtg_begin_time", None),
            end_minutes=getattr(self, "_battery_dtg_end_time", None),
            schedule_status=getattr(self, "_battery_dtg_schedule_status", None),
        )

    @property
    def discharge_to_grid_schedule_available(self) -> bool:
        return self._battery_schedule_available(
            getattr(self, "_battery_dtg_control", None),
            schedule_id=getattr(self, "_battery_dtg_schedule_id", None),
            start_minutes=getattr(self, "_battery_dtg_begin_time", None),
            end_minutes=getattr(self, "_battery_dtg_end_time", None),
        )

    def _battery_schedule_effective_enabled(self, schedule_type: str) -> bool | None:
        normalized = str(schedule_type).lower()
        schedule_enabled = getattr(
            self, f"_battery_{normalized}_schedule_enabled", None
        )
        schedule_id = getattr(self, f"_battery_{normalized}_schedule_id", None)
        schedule_status = getattr(self, f"_battery_{normalized}_schedule_status", None)

        if normalized == "dtg":
            control_enabled = self.battery_dtg_control_enabled
        elif normalized == "rbd":
            control_enabled = self.battery_rbd_control_enabled
        elif normalized == "cfg":
            control_enabled = self.battery_cfg_control_force_schedule_opted
        else:
            control_enabled = None

        if control_enabled is False or schedule_enabled is False:
            return False
        if control_enabled is not None:
            return control_enabled
        if schedule_enabled is not None:
            return schedule_enabled
        toggle_target = getattr(
            self, f"_battery_{normalized}_toggle_target_enabled", None
        )
        if normalized in {"dtg", "rbd"} and toggle_target is not None:
            return toggle_target
        if schedule_id is not None:
            return None
        if (
            normalized in {"dtg", "rbd"}
            and isinstance(schedule_status, str)
            and schedule_status.strip()
        ):
            return False
        return None

    @property
    def battery_discharge_to_grid_schedule_enabled(self) -> bool | None:
        return self._battery_schedule_effective_enabled("dtg")

    @property
    def battery_discharge_to_grid_start_time(self) -> dt_time | None:
        minutes = getattr(self, "_battery_dtg_begin_time", None)
        if minutes is None:
            minutes = getattr(self, "_battery_dtg_control_begin_time", None)
        return self._minutes_of_day_to_time(minutes)

    @property
    def battery_discharge_to_grid_end_time(self) -> dt_time | None:
        minutes = getattr(self, "_battery_dtg_end_time", None)
        if minutes is None:
            minutes = getattr(self, "_battery_dtg_control_end_time", None)
        return self._minutes_of_day_to_time(minutes)

    @property
    def battery_dtg_schedule_limit(self) -> int | None:
        return getattr(self, "_battery_dtg_schedule_limit", None)

    @property
    def battery_dtg_schedule_status(self) -> str | None:
        return getattr(self, "_battery_dtg_schedule_status", None)

    @property
    def battery_dtg_schedule_pending(self) -> bool:
        return self.battery_dtg_schedule_status == "pending"

    @property
    def restrict_battery_discharge_schedule_supported(self) -> bool:
        return self._battery_schedule_supported(
            getattr(self, "_battery_rbd_control", None),
            schedule_id=getattr(self, "_battery_rbd_schedule_id", None),
            start_minutes=getattr(self, "_battery_rbd_begin_time", None),
            end_minutes=getattr(self, "_battery_rbd_end_time", None),
            schedule_status=getattr(self, "_battery_rbd_schedule_status", None),
        )

    @property
    def restrict_battery_discharge_schedule_available(self) -> bool:
        return self._battery_schedule_available(
            getattr(self, "_battery_rbd_control", None),
            schedule_id=getattr(self, "_battery_rbd_schedule_id", None),
            start_minutes=getattr(self, "_battery_rbd_begin_time", None),
            end_minutes=getattr(self, "_battery_rbd_end_time", None),
        )

    @property
    def battery_restrict_battery_discharge_schedule_enabled(self) -> bool | None:
        return self._battery_schedule_effective_enabled("rbd")

    @property
    def battery_restrict_battery_discharge_start_time(self) -> dt_time | None:
        minutes = getattr(self, "_battery_rbd_begin_time", None)
        if minutes is None:
            minutes = getattr(self, "_battery_rbd_control_begin_time", None)
        return self._minutes_of_day_to_time(minutes)

    @property
    def battery_restrict_battery_discharge_end_time(self) -> dt_time | None:
        minutes = getattr(self, "_battery_rbd_end_time", None)
        if minutes is None:
            minutes = getattr(self, "_battery_rbd_control_end_time", None)
        return self._minutes_of_day_to_time(minutes)

    @property
    def battery_rbd_schedule_limit(self) -> int | None:
        return getattr(self, "_battery_rbd_schedule_limit", None)

    @property
    def battery_rbd_schedule_status(self) -> str | None:
        return getattr(self, "_battery_rbd_schedule_status", None)

    @property
    def battery_rbd_schedule_pending(self) -> bool:
        return self.battery_rbd_schedule_status == "pending"

    @property
    def battery_shutdown_level(self) -> int | None:
        return getattr(self, "_battery_very_low_soc", None)

    @property
    def battery_shutdown_level_min(self) -> int:
        value = getattr(self, "_battery_very_low_soc_min", None)
        if value is None:
            return self._battery_min_soc_floor()
        return int(value)

    @property
    def battery_shutdown_level_max(self) -> int:
        value = getattr(self, "_battery_very_low_soc_max", None)
        return value if value is not None else 100

    @property
    def battery_shutdown_level_available(self) -> bool:
        state = self.battery_state
        envoy_supports_vls = getattr(
            state,
            "_battery_envoy_supports_vls",
            getattr(self, "_battery_envoy_supports_vls", None),
        )
        if envoy_supports_vls is False:
            return False
        very_low_soc = getattr(
            state,
            "_battery_very_low_soc",
            getattr(self, "_battery_very_low_soc", None),
        )
        if very_low_soc is not None:
            return True
        battery_limit_support = getattr(
            state,
            "_battery_limit_support",
            getattr(self, "_battery_limit_support", None),
        )
        if battery_limit_support is False:
            return False
        return False

    def _battery_min_soc_floor(self) -> int:
        value = self._coerce_optional_int(
            getattr(self, "_battery_very_low_soc_min", None)
        )
        if value is None:
            return BATTERY_MIN_SOC_FALLBACK
        return max(0, min(100, int(value)))

    def _grid_control_is_stale(self) -> bool:
        raw_supported = getattr(self, "_grid_control_supported", None)
        if raw_supported is None:
            return True
        last_success = getattr(self, "_grid_control_check_last_success_mono", None)
        if not isinstance(last_success, (int, float)):
            return False
        age = time.monotonic() - float(last_success)
        if age < 0:
            return False
        return age >= GRID_CONTROL_CHECK_STALE_AFTER_S

    @property
    def grid_control_supported(self) -> bool | None:
        raw_supported = getattr(self, "_grid_control_supported", None)
        if raw_supported is None:
            return None
        if self._grid_control_is_stale():
            return None
        return raw_supported

    @property
    def grid_control_disable(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        return getattr(self, "_grid_control_disable", None)

    @property
    def grid_control_active_download(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        return getattr(self, "_grid_control_active_download", None)

    @property
    def grid_control_sunlight_backup_system_check(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        return getattr(self, "_grid_control_sunlight_backup_system_check", None)

    @property
    def grid_control_grid_outage_check(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        return getattr(self, "_grid_control_grid_outage_check", None)

    @property
    def grid_control_user_initiated_toggle(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        return getattr(self, "_grid_control_user_initiated_toggle", None)

    @property
    def grid_toggle_pending(self) -> bool:
        return self.grid_control_user_initiated_toggle is True

    @property
    def grid_toggle_blocked_reasons(self) -> list[str]:
        if self.grid_control_supported is not True:
            return []
        reasons: list[str] = []
        if self.grid_control_disable is True:
            reasons.append("disable_grid_control")
        if self.grid_control_active_download is True:
            reasons.append("active_download")
        if self.grid_control_sunlight_backup_system_check is True:
            reasons.append("sunlight_backup_system_check")
        if self.grid_control_grid_outage_check is True:
            reasons.append("grid_outage_check")
        return reasons

    @property
    def grid_toggle_allowed(self) -> bool | None:
        if self.grid_control_supported is not True:
            return None
        if self.grid_toggle_pending:
            return False
        flags = [
            self.grid_control_disable,
            self.grid_control_active_download,
            self.grid_control_sunlight_backup_system_check,
            self.grid_control_grid_outage_check,
        ]
        if any(value is True for value in flags):
            return False
        if any(value is None for value in flags):
            return None
        return True

    def _dry_contact_settings_is_stale(self) -> bool:
        raw_supported = getattr(self, "_dry_contact_settings_supported", None)
        if raw_supported is None:
            return True
        last_success = getattr(self, "_dry_contact_settings_last_success_mono", None)
        if not isinstance(last_success, (int, float)):
            return False
        age = time.monotonic() - float(last_success)
        if age < 0:
            return False
        return age >= DRY_CONTACT_SETTINGS_STALE_AFTER_S

    @property
    def dry_contact_settings_supported(self) -> bool | None:
        raw_supported = getattr(self, "_dry_contact_settings_supported", None)
        if raw_supported is None:
            return None
        if self._dry_contact_settings_is_stale():
            return None
        return raw_supported

    def dry_contact_settings_entries(self) -> list[dict[str, object]]:
        entries = getattr(self, "_dry_contact_settings_entries", [])
        if not isinstance(entries, list):
            return []
        return [
            self.battery_runtime._copy_dry_contact_settings_entry(entry)
            for entry in entries
            if isinstance(entry, dict)
        ]

    def dry_contact_unmatched_settings(self) -> list[dict[str, object]]:
        entries = getattr(self, "_dry_contact_unmatched_settings", [])
        if not isinstance(entries, list):
            return []
        return [
            self.battery_runtime._copy_dry_contact_settings_entry(entry)
            for entry in entries
            if isinstance(entry, dict)
        ]

    @staticmethod
    def _normalize_grid_mode_value(value: object) -> str | None:
        text = EnphaseCoordinator._coerce_optional_text(value)
        if text is None:
            return None
        upper = text.upper()
        if "OFF_GRID" in upper:
            return "off_grid"
        if "ON_GRID" in upper:
            return "on_grid"
        return None

    @property
    def grid_mode_raw_states(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        data = self.data if isinstance(self.data, dict) else {}
        for payload in data.values():
            if not isinstance(payload, dict):
                continue
            raw = self._coerce_optional_text(payload.get("off_grid_state"))
            if not raw:
                continue
            if raw in seen:
                continue
            seen.add(raw)
            out.append(raw)
        return sorted(out)

    @property
    def grid_mode(self) -> str | None:
        raw_states = self.grid_mode_raw_states
        if not raw_states:
            return None
        normalized = {
            mode
            for mode in (self._normalize_grid_mode_value(state) for state in raw_states)
            if mode is not None
        }
        if len(normalized) == 1:
            return next(iter(normalized))
        return "unknown"

    def _raise_grid_validation(
        self,
        key: str,
        *,
        placeholders: dict[str, object] | None = None,
        message: str | None = None,
    ) -> None:
        self.raise_grid_validation(
            key,
            placeholders=placeholders,
            message=message,
        )

    def raise_grid_validation(
        self,
        key: str,
        *,
        placeholders: dict[str, object] | None = None,
        message: str | None = None,
    ) -> None:
        raise_translated_service_validation(
            translation_domain=DOMAIN,
            translation_key=f"exceptions.{key}",
            translation_placeholders=placeholders,
            message=message,
        )

    def _grid_envoy_serial(self) -> str | None:  # pragma: no cover
        return self.battery_runtime.grid_envoy_serial()

    async def _async_assert_grid_toggle_allowed(self) -> None:  # pragma: no cover
        await self.battery_runtime.async_assert_grid_toggle_allowed()

    @property
    def storm_guard_state(self) -> str | None:
        return self._storm_guard_state

    @property
    def storm_guard_update_pending(self) -> bool:  # pragma: no cover
        pending_state = getattr(self, "_storm_guard_pending_state", None)
        if pending_state is None:
            return False
        effective_state = self._normalize_storm_guard_state(
            getattr(self, "_storm_guard_state", None)
        )
        if effective_state == pending_state:
            self._storm_guard_pending_state = None
            self._storm_guard_pending_expires_mono = None
            return False
        expires_at = getattr(self, "_storm_guard_pending_expires_mono", None)
        if expires_at is None or time.monotonic() >= float(expires_at):
            self._storm_guard_pending_state = None
            self._storm_guard_pending_expires_mono = None
            return False
        return getattr(self, "_storm_guard_pending_state", None) is not None

    @property
    def storm_evse_enabled(self) -> bool | None:
        return self._storm_evse_enabled

    @property
    def storm_alert_active(self) -> bool | None:
        return self._storm_alert_active

    @property
    def storm_alert_critical_override(self) -> bool | None:
        return getattr(self, "_storm_alert_critical_override", None)

    @property
    def storm_alerts(self) -> list[dict[str, object]]:
        alerts = getattr(self, "_storm_alerts", None)
        if not isinstance(alerts, list):
            return []
        out: list[dict[str, object]] = []
        for item in alerts:
            if isinstance(item, dict):
                out.append(dict(item))
        return out

    @property
    def heatpump_runtime_state(self) -> dict[str, object]:
        return self.heatpump_runtime.heatpump_runtime_state

    @property
    def heatpump_runtime_state_last_error(self) -> str | None:
        return self.heatpump_runtime.heatpump_runtime_state_last_error

    @property
    def heatpump_runtime_state_using_stale(self) -> bool:
        return self.heatpump_runtime.heatpump_runtime_state_using_stale

    @property
    def heatpump_runtime_state_last_success_utc(self) -> datetime | None:
        return self.heatpump_runtime.heatpump_runtime_state_last_success_utc

    @property
    def heatpump_daily_consumption(self) -> dict[str, object]:
        return self.heatpump_runtime.heatpump_daily_consumption

    @property
    def heatpump_daily_consumption_last_error(self) -> str | None:
        return self.heatpump_runtime.heatpump_daily_consumption_last_error

    @property
    def heatpump_daily_consumption_using_stale(self) -> bool:
        return self.heatpump_runtime.heatpump_daily_consumption_using_stale

    @property
    def heatpump_daily_consumption_last_success_utc(self) -> datetime | None:
        return self.heatpump_runtime.heatpump_daily_consumption_last_success_utc

    @property
    def heatpump_daily_split_last_error(self) -> str | None:
        return self.heatpump_runtime.heatpump_daily_split_last_error

    @property
    def heatpump_daily_split_using_stale(self) -> bool:
        return self.heatpump_runtime.heatpump_daily_split_using_stale

    @property
    def heatpump_daily_split_last_success_utc(self) -> datetime | None:
        return self.heatpump_runtime.heatpump_daily_split_last_success_utc

    @property
    def heatpump_power_w(self) -> float | None:
        return self.heatpump_runtime.heatpump_power_w

    @property
    def heatpump_power_sample_utc(self) -> datetime | None:
        return self.heatpump_runtime.heatpump_power_sample_utc

    @property
    def heatpump_power_start_utc(self) -> datetime | None:
        return self.heatpump_runtime.heatpump_power_start_utc

    @property
    def heatpump_power_device_uid(self) -> str | None:
        return self.heatpump_runtime.heatpump_power_device_uid

    @property
    def heatpump_power_source(self) -> str | None:
        return self.heatpump_runtime.heatpump_power_source

    @property
    def heatpump_power_raw_w(self) -> float | None:
        return self.heatpump_runtime.heatpump_power_raw_w

    @property
    def heatpump_power_window_seconds(self) -> float | None:
        return self.heatpump_runtime.heatpump_power_window_seconds

    @property
    def heatpump_power_validation(self) -> str | None:
        return self.heatpump_runtime.heatpump_power_validation

    @property
    def heatpump_power_smoothed(self) -> bool:
        return self.heatpump_runtime.heatpump_power_smoothed

    @property
    def heatpump_power_using_stale(self) -> bool:
        return self.heatpump_runtime.heatpump_power_using_stale

    @property
    def heatpump_power_last_success_utc(self) -> datetime | None:
        return self.heatpump_runtime.heatpump_power_last_success_utc

    @property
    def heatpump_power_last_error(self) -> str | None:
        return self.heatpump_runtime.heatpump_power_last_error

    @property
    def current_power_consumption_w(self) -> float | None:  # pragma: no cover
        value = getattr(self, "_current_power_consumption_w", None)
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    @property
    def current_power_consumption_sample_utc(self) -> datetime | None:
        value = getattr(self, "_current_power_consumption_sample_utc", None)
        if isinstance(value, datetime):
            return value
        return None

    @property
    def current_power_consumption_reported_units(
        self,
    ) -> str | None:  # pragma: no cover
        value = getattr(self, "_current_power_consumption_reported_units", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def current_power_consumption_reported_precision(
        self,
    ) -> int | None:  # pragma: no cover
        value = getattr(self, "_current_power_consumption_reported_precision", None)
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    @property
    def current_power_consumption_source(self) -> str | None:  # pragma: no cover
        value = getattr(self, "_current_power_consumption_source", None)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @property
    def battery_supports_mqtt(self) -> bool | None:
        return getattr(self, "_battery_supports_mqtt", None)

    @property
    def battery_profile_polling_interval(self) -> int | None:
        return getattr(self, "_battery_polling_interval_s", None)

    @property
    def battery_profile_evse_device(self) -> dict[str, object] | None:
        value = getattr(self, "_battery_profile_evse_device", None)
        if isinstance(value, dict):
            return dict(value)
        return None

    @property
    def battery_dtg_control(self) -> dict[str, bool | None] | None:
        value = getattr(self, "_battery_dtg_control", None)
        return self._battery_control_to_dict(value)

    @property
    def battery_cfg_control(self) -> dict[str, bool | None] | None:
        value = getattr(self, "_battery_cfg_control", None)
        return self._battery_control_to_dict(value)

    @property
    def battery_rbd_control(self) -> dict[str, bool | None] | None:
        value = getattr(self, "_battery_rbd_control", None)
        return self._battery_control_to_dict(value)

    @property
    def battery_cfg_control_show(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        field = self._battery_control_field(value, "show")
        if field is not None:
            return field
        return getattr(self, "_battery_cfg_control_show", None)

    @property
    def battery_cfg_control_enabled(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        field = self._battery_control_field(value, "enabled")
        if field is not None:
            return field
        return getattr(self, "_battery_cfg_control_enabled", None)

    @property
    def battery_dtg_control_enabled(self) -> bool | None:
        value = getattr(self, "_battery_dtg_control", None)
        return self._battery_control_field(value, "enabled")

    @property
    def battery_rbd_control_enabled(self) -> bool | None:
        value = getattr(self, "_battery_rbd_control", None)
        return self._battery_control_field(value, "enabled")

    @property
    def battery_cfg_control_locked(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        return self._battery_control_field(value, "locked")

    @property
    def battery_cfg_control_show_day_schedule(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        return self._battery_control_field(value, "show_day_schedule")

    @property
    def battery_cfg_control_schedule_supported(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        field = self._battery_control_field(value, "schedule_supported")
        if field is not None:
            return field
        return getattr(self, "_battery_cfg_control_schedule_supported", None)

    @property
    def battery_cfg_control_force_schedule_supported(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        field = self._battery_control_field(value, "force_schedule_supported")
        if field is not None:
            return field
        return getattr(self, "_battery_cfg_control_force_schedule_supported", None)

    @property
    def battery_cfg_control_force_schedule_opted(self) -> bool | None:
        value = getattr(self, "_battery_cfg_control", None)
        return self._battery_control_field(value, "force_schedule_opted")

    @property
    def battery_use_battery_for_self_consumption(self) -> bool | None:
        return getattr(self, "_battery_use_battery_for_self_consumption", None)

    def _mark_auth_settings_available(self) -> None:
        if self._auth_settings_available and not self._auth_settings_issue_reported:
            return
        self._auth_settings_available = True
        self._auth_settings_failures = 0
        self._auth_settings_last_error = None
        self._auth_settings_last_failure_utc = None
        self._auth_settings_backoff_until = None
        self._auth_settings_backoff_ends_utc = None
        self.diagnostics.clear_auth_settings_issue()

    def mark_auth_settings_available(self) -> None:
        """Public wrapper used by entities."""

        self._mark_auth_settings_available()

    def _note_auth_settings_unavailable(
        self,
        err: Exception | str | None = None,
    ) -> None:
        """Record auth settings outage and raise a repair issue."""
        reason = redact_text(err, site_ids=(self.site_id,)) if err else ""
        if not reason:
            reason = "Auth settings unavailable"
        self._auth_settings_available = False
        self._auth_settings_failures += 1
        self._auth_settings_last_error = reason
        self._auth_settings_last_failure_utc = dt_util.utcnow()
        delay = self._auth_settings_backoff_delay()
        self._auth_settings_backoff_until = time.monotonic() + delay
        try:
            self._auth_settings_backoff_ends_utc = dt_util.utcnow() + timedelta(
                seconds=delay
            )
        except Exception:
            self._auth_settings_backoff_ends_utc = None
        self.diagnostics.report_auth_settings_issue()

    def note_auth_settings_unavailable(
        self,
        err: Exception | str | None = None,
    ) -> None:
        """Public wrapper used by entities."""

        self._note_auth_settings_unavailable(err)

    def _sync_session_history_issue(self) -> None:
        self.diagnostics.sync_session_history_issue()

    def _sync_site_energy_issue(self) -> None:
        self.diagnostics.sync_site_energy_issue()

    def _sync_battery_profile_pending_issue(self) -> None:
        """Raise/clear repair issue when a BatteryConfig profile change stalls."""
        self.diagnostics.sync_battery_profile_pending_issue()

    def sync_battery_profile_pending_issue(self) -> None:
        self._sync_battery_profile_pending_issue()

    def _schedule_backoff_timer(self, delay: float) -> None:
        if delay <= 0:
            self._clear_backoff_timer()
            self._backoff_until = None
            self.backoff_ends_utc = None
            self.hass.async_create_task(
                self.async_request_refresh(), name=f"{DOMAIN}_backoff_refresh"
            )
            return
        self._clear_backoff_timer()
        try:
            self.backoff_ends_utc = dt_util.utcnow() + timedelta(seconds=delay)
        except Exception:
            self.backoff_ends_utc = None

        async def _resume(_now: datetime) -> None:
            # Home Assistant should recover from cloud throttling on its own;
            # the timer clears diagnostic state and triggers one refresh when
            # the backoff window ends.
            self._backoff_cancel = None
            self._backoff_until = None
            self.backoff_ends_utc = None
            await self.async_request_refresh()

        self._backoff_cancel = async_call_later(self.hass, delay, _resume)

    def set_last_set_amps(self, sn: str, amps: int) -> None:
        self.evse_runtime.set_last_set_amps(sn, amps)

    def require_plugged(self, sn: str) -> None:
        self.evse_runtime.require_plugged(sn)

    def _ensure_serial_tracked(self, serial: str) -> bool:
        return self.evse_runtime.ensure_serial_tracked(serial)

    def iter_serials(self) -> list[str]:
        """Return charger serials in a stable order for entity setup."""
        if getattr(self, "site_only", False):
            return []
        ordered: list[str] = []
        serial_order = getattr(self, "_serial_order", None)
        known_serials = getattr(self, "serials", None)
        if serial_order:
            ordered.extend(serial_order)
        elif known_serials:
            # Fallback for legacy configs where order could not be preserved
            ordered.extend(sorted(known_serials))
        source = self.data if isinstance(self.data, dict) else {}
        if isinstance(source, dict):
            ordered.extend(str(sn) for sn in source.keys())
        # Deduplicate while preserving order
        return [sn for sn in dict.fromkeys(ordered) if sn]

    def iter_battery_serials(self) -> list[str]:
        """Return active battery identities in a stable order."""

        order = getattr(self, "_battery_storage_order", None)
        snapshots = getattr(self, "_battery_storage_data", None)
        if not isinstance(order, list) or not isinstance(snapshots, dict):
            return []
        out: list[str] = []
        for item in order:
            try:
                key = str(item).strip()
            except Exception:  # noqa: BLE001
                continue
            if not key or key not in snapshots:
                continue
            out.append(key)
        return out

    def iter_ac_battery_serials(self) -> list[str]:
        """Return active AC Battery identities in a stable order."""

        order = getattr(self, "_ac_battery_order", None)
        snapshots = getattr(self, "_ac_battery_data", None)
        if not isinstance(order, list) or not isinstance(snapshots, dict):
            return []
        out: list[str] = []
        for item in order:
            try:
                key = str(item).strip()
            except Exception:  # noqa: BLE001
                continue
            if not key or key not in snapshots:
                continue
            out.append(key)
        return out

    def battery_storage(
        self, serial: str
    ) -> dict[str, object] | None:  # pragma: no cover
        """Return normalized battery snapshot for an active battery identity."""

        snapshots = getattr(self, "_battery_storage_data", None)
        if not isinstance(snapshots, dict):
            return None
        try:
            key = str(serial).strip()
        except Exception:  # noqa: BLE001
            return None
        if not key:
            return None
        payload = snapshots.get(key)
        if not isinstance(payload, dict):
            return None
        out = dict(payload)
        detail = self.system_dashboard_battery_detail(key)
        if isinstance(detail, dict):
            for detail_key, detail_value in detail.items():
                if detail_value is None:
                    continue
                out[detail_key] = detail_value
        return out

    def ac_battery_storage(self, serial: str) -> dict[str, object] | None:
        """Return normalized AC Battery snapshot for an active battery identity."""

        snapshots = getattr(self, "_ac_battery_data", None)
        if not isinstance(snapshots, dict):
            return None
        try:
            key = str(serial).strip()
        except Exception:  # noqa: BLE001
            return None
        if not key:
            return None
        payload = snapshots.get(key)
        if not isinstance(payload, dict):
            return None
        return dict(payload)

    async def async_ensure_ac_battery_diagnostics(self) -> None:
        if (
            isinstance(getattr(self, "_ac_battery_devices_payload", None), dict)
            and isinstance(getattr(self, "_ac_battery_telemetry_payloads", None), dict)
            and isinstance(getattr(self, "_ac_battery_events_payloads", None), dict)
        ):
            return
        await self.battery_runtime.async_refresh_ac_battery_devices(force=True)
        await self.battery_runtime.async_refresh_ac_battery_telemetry(force=True)
        await self.battery_runtime.async_refresh_ac_battery_events(force=True)

    async def async_set_ac_battery_sleep_mode(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_ac_battery_sleep_mode(enabled)

    async def async_set_ac_battery_target_soc(self, value: int) -> None:
        await self.battery_runtime.async_set_ac_battery_target_soc(value)

    def get_desired_charging(self, sn: str) -> bool | None:
        return self.evse_runtime.get_desired_charging(sn)

    def set_desired_charging(self, sn: str, desired: bool | None) -> None:
        self.evse_runtime.set_desired_charging(sn, desired)

    @staticmethod
    def _coerce_amp(value) -> int | None:
        return EvseRuntime.coerce_amp(value)

    def _amp_limits(self, sn: str) -> tuple[int | None, int | None]:
        return self.evse_runtime.amp_limits(sn)

    def _apply_amp_limits(self, sn: str, amps: int | float | str | None) -> int:
        return self.evse_runtime.apply_amp_limits(sn, amps)

    def pick_start_amps(
        self, sn: str, requested: int | float | str | None = None, fallback: int = 32
    ) -> int:
        return self.evse_runtime.pick_start_amps(sn, requested, fallback)

    async def _get_charge_mode(self, sn: str) -> str | None:
        return await self.evse_runtime.async_get_charge_mode(sn)

    async def _get_green_battery_setting(
        self, sn: str
    ) -> tuple[bool | None, bool] | None:
        return await self.evse_runtime.async_get_green_battery_setting(sn)

    async def _get_auth_settings(
        self, sn: str
    ) -> tuple[bool | None, bool | None, bool, bool] | None:
        return await self.evse_runtime.async_get_auth_settings(sn)

    def set_charge_mode_cache(self, sn: str, mode: str) -> None:
        self.evse_runtime.set_charge_mode_cache(sn, mode)

    def set_green_battery_cache(
        self, sn: str, enabled: bool, supported: bool = True
    ) -> None:
        self.evse_runtime.set_green_battery_cache(sn, enabled, supported)

    def set_app_auth_cache(self, sn: str, enabled: bool) -> None:
        self.evse_runtime.set_app_auth_cache(sn, enabled)

    def evse_feature_flag(self, key: str, sn: str | None = None) -> object | None:
        """Return a parsed EVSE feature flag for the site or charger."""

        return self.evse_feature_flags_runtime.feature_flag(key, sn)

    def evse_feature_flag_enabled(self, key: str, sn: str | None = None) -> bool | None:
        """Return a feature flag coerced to a tri-state boolean."""

        return self.evse_feature_flags_runtime.feature_flag_enabled(key, sn)

    @staticmethod
    def _coerce_evse_feature_flags_map(value: object) -> dict[str, object]:
        return EvseFeatureFlagsRuntime.coerce_evse_feature_flags_map(value)

    def _parse_evse_feature_flags_payload(self, payload: object) -> None:
        """Cache site and charger feature flags from the EVSE management payload."""

        self.evse_feature_flags_runtime.parse_payload(payload)

    async def _async_refresh_evse_feature_flags(self, *, force: bool = False) -> None:
        """Refresh EVSE feature flags used for capability gating."""

        await self.evse_feature_flags_runtime.async_refresh(force=force)

    @staticmethod
    def _coerce_optional_bool(value) -> bool | None:
        return coerce_optional_bool(value)

    @staticmethod
    def coerce_optional_bool(value: object) -> bool | None:
        return EnphaseCoordinator._coerce_optional_bool(value)

    @staticmethod
    def _parse_percent_value(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except Exception:  # noqa: BLE001
                return None
        if not isinstance(value, str):
            return None
        try:
            cleaned = value.strip().replace("%", "")
        except Exception:  # noqa: BLE001
            return None
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_battery_status_text(value: object) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip().lower()
        except Exception:  # noqa: BLE001
            return None
        if not text:
            return None
        normalized = " ".join(text.replace("_", " ").replace("-", " ").split())
        if not normalized:
            return None
        tokens = [token for token in normalized.split(" ") if token]
        token_set = set(tokens)
        if "normal" in token_set and "not" not in token_set:
            return "normal"
        if any(
            token in token_set
            for token in ("error", "fault", "critical", "failed", "alarm")
        ):
            return "error"
        if any(token in token_set for token in ("warning", "warn", "degraded")):
            return "warning"
        if "abnormal" in token_set:
            return "warning"
        if "not reporting" in normalized:
            return "warning"
        if "not" in token_set and "normal" in token_set:
            return "warning"
        return "unknown"

    @staticmethod
    def _battery_status_severity_value(status: str | None) -> int:
        if status is None:
            return BATTERY_STATUS_SEVERITY["unknown"]
        return BATTERY_STATUS_SEVERITY.get(status, BATTERY_STATUS_SEVERITY["unknown"])

    @staticmethod
    def _battery_storage_key(payload: dict[str, object]) -> str | None:
        serial = EnphaseCoordinator._coerce_optional_text(payload.get("serial_number"))
        if serial:
            return serial
        storage_id = EnphaseCoordinator._coerce_optional_int(payload.get("id"))
        if storage_id is not None:
            return f"id_{storage_id}"
        return None

    @staticmethod
    def _normalize_battery_id(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if not value.is_integer():
                return None
            return str(int(value))
        if not isinstance(value, str):
            return None
        try:
            cleaned = value.strip().replace(",", "")
        except Exception:  # noqa: BLE001
            return None
        if not cleaned:
            return None
        if cleaned[0] in ("+", "-"):
            sign = cleaned[0]
            digits = cleaned[1:]
        else:
            sign = ""
            digits = cleaned
        if not digits.isdigit():
            return None
        return f"{sign}{digits}"

    def _parse_battery_status_payload(
        self, payload: object
    ) -> None:  # pragma: no cover
        self.battery_runtime.parse_battery_status_payload(payload)

    @staticmethod
    def _normalize_storm_guard_state(value) -> str | None:  # pragma: no cover
        return BatteryRuntime.normalize_storm_guard_state(value)

    def _clear_storm_guard_pending(self) -> None:  # pragma: no cover
        self.battery_runtime.clear_storm_guard_pending()

    def _set_storm_guard_pending(self, target_state: str) -> None:  # pragma: no cover
        self.battery_runtime.set_storm_guard_pending(target_state)

    def _sync_storm_guard_pending(  # pragma: no cover - compatibility shim
        self, effective_state: str | None = None
    ) -> None:
        self.battery_runtime.sync_storm_guard_pending(effective_state)

    def _clear_battery_pending(self) -> None:
        self.battery_runtime.clear_battery_pending()

    def _set_battery_pending(  # pragma: no cover - compatibility shim
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None,
        require_exact_settings: bool = True,
    ) -> None:
        self.battery_runtime.set_battery_pending(
            profile=profile,
            reserve=reserve,
            sub_type=sub_type,
            require_exact_settings=require_exact_settings,
        )

    def _assert_battery_profile_write_allowed(self) -> None:  # pragma: no cover
        self.battery_runtime.assert_battery_profile_write_allowed()

    def _normalize_battery_reserve_for_profile(self, profile: str, reserve: int) -> int:
        return self.battery_runtime.normalize_battery_reserve_for_profile(
            profile, reserve
        )

    def _effective_profile_matches_pending(self) -> bool:
        return self.battery_runtime.effective_profile_matches_pending()

    def _remember_battery_reserve(
        self, profile: str | None, reserve: int | None
    ) -> None:
        self.battery_runtime.remember_battery_reserve(profile, reserve)

    def dry_contact_settings_matches(
        self, members: Iterable[dict[str, object]]
    ) -> tuple[list[dict[str, object] | None], list[dict[str, object]]]:
        return self.battery_runtime.dry_contact_settings_matches(list(members))

    def _battery_profile_devices_payload(self) -> list[dict[str, object]] | None:
        if not self._battery_profile_devices:
            return None
        payload: list[dict[str, object]] = []
        seen_uuids: set[str] = set()
        for item in self._battery_profile_devices:
            uuid = item.get("uuid")
            if uuid is None:
                continue
            try:
                uuid_text = str(uuid).strip()
            except Exception:  # noqa: BLE001
                continue
            if not uuid_text or uuid_text in seen_uuids:
                continue
            seen_uuids.add(uuid_text)
            entry: dict[str, object] = {
                "uuid": uuid_text,
                "deviceType": "iqEvse",
            }
            enabled = self._coerce_optional_bool(item.get("enable"))
            if enabled is not None:
                entry["enable"] = enabled
            charge_mode = item.get("chargeMode")
            if charge_mode is not None:
                entry["chargeMode"] = str(charge_mode)
            payload.append(entry)
        return payload or None

    def battery_profile_devices_payload(self) -> list[dict[str, object]] | None:
        return self._battery_profile_devices_payload()

    def _target_reserve_for_profile(self, profile: str) -> int:
        return self.battery_runtime.target_reserve_for_profile(profile)

    def _current_savings_sub_type(self) -> str | None:
        return self.battery_runtime.current_savings_sub_type()

    async def _async_apply_battery_profile(
        self,
        *,
        profile: str,
        reserve: int,
        sub_type: str | None = None,
        require_exact_pending_match: bool = True,
    ) -> None:
        await self.battery_runtime.async_apply_battery_profile(
            profile=profile,
            reserve=reserve,
            sub_type=sub_type,
            require_exact_pending_match=require_exact_pending_match,
        )

    def _assert_battery_settings_write_allowed(self) -> None:  # pragma: no cover
        self.battery_runtime.assert_battery_settings_write_allowed()

    def _current_charge_from_grid_schedule_window(self) -> tuple[int, int]:
        return self.battery_runtime.current_charge_from_grid_schedule_window()

    def _battery_itc_disclaimer_value(self) -> str:  # pragma: no cover
        return self.battery_runtime.battery_itc_disclaimer_value()

    async def _async_apply_battery_settings(  # pragma: no cover - compatibility shim
        self, payload: dict[str, object]
    ) -> None:
        await self.battery_runtime.async_apply_battery_settings(payload)

    def _raise_schedule_update_validation_error(  # pragma: no cover - compatibility shim
        self, err: aiohttp.ClientResponseError
    ) -> None:
        self.battery_runtime.raise_schedule_update_validation_error(err)

    async def _async_update_battery_schedule(  # pragma: no cover - compatibility shim
        self,
        schedule_id: str,
        *,
        start_time: str,
        end_time: str,
        limit: int,
        days: list[int],
        timezone: str,
    ) -> None:
        await self.battery_runtime.async_update_battery_schedule(
            schedule_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            days=days,
            timezone=timezone,
        )

    def parse_battery_status_payload(self, payload: object) -> None:  # pragma: no cover
        self.battery_runtime.parse_battery_status_payload(payload)

    def _backup_history_tzinfo(self) -> _tz | ZoneInfo:
        return self.battery_runtime.backup_history_tzinfo()

    def _parse_battery_backup_history_payload(
        self,
        payload: object,
    ) -> list[dict[str, object]] | None:
        return self.battery_runtime.parse_battery_backup_history_payload(payload)

    async def _async_refresh_battery_backup_history(
        self, *, force: bool = False
    ) -> None:
        await self.battery_runtime.async_refresh_battery_backup_history(force=force)

    async def _async_refresh_battery_settings(self, *, force: bool = False) -> None:
        await self.battery_runtime.async_refresh_battery_settings(force=force)

    async def _async_refresh_battery_schedules(self) -> None:
        await self.battery_runtime.async_refresh_battery_schedules()

    def parse_battery_schedules_payload(
        self, payload: object
    ) -> None:  # pragma: no cover
        self.battery_runtime.parse_battery_schedules_payload(payload)

    def _parse_battery_schedules_payload(
        self, payload: object
    ) -> None:  # pragma: no cover
        self.battery_runtime.parse_battery_schedules_payload(payload)

    async def _async_refresh_battery_site_settings(
        self, *, force: bool = False
    ) -> None:
        await self.battery_runtime.async_refresh_battery_site_settings(force=force)

    async def _async_refresh_grid_control_check(self, *, force: bool = False) -> None:
        await self.battery_runtime.async_refresh_grid_control_check(force=force)

    async def _async_refresh_dry_contact_settings(self, *, force: bool = False) -> None:
        await self.battery_runtime.async_refresh_dry_contact_settings(force=force)

    async def async_set_battery_reserve(self, reserve: int) -> None:
        await self.battery_runtime.async_set_battery_reserve(reserve)

    async def async_set_savings_use_battery_after_peak(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_savings_use_battery_after_peak(enabled)

    async def async_cancel_pending_profile_change(self) -> None:
        await self.battery_runtime.async_cancel_pending_profile_change()

    async def async_set_charge_from_grid(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_charge_from_grid(enabled)

    async def async_set_charge_from_grid_schedule_enabled(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_charge_from_grid_schedule_enabled(enabled)

    async def async_set_charge_from_grid_schedule_time(  # pragma: no cover - compatibility shim
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
    ) -> None:
        await self.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=start,
            end=end,
        )

    async def async_set_cfg_schedule_limit(
        self, limit: int
    ) -> None:  # pragma: no cover
        await self.battery_runtime.async_set_cfg_schedule_limit(limit)

    async def async_update_cfg_schedule(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
        limit: int | None = None,
    ) -> None:
        await self.battery_runtime.async_update_cfg_schedule(
            start=start,
            end=end,
            limit=limit,
        )

    async def async_set_discharge_to_grid_schedule_enabled(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_discharge_to_grid_schedule_enabled(enabled)

    async def async_set_discharge_to_grid_schedule_time(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
    ) -> None:
        await self.battery_runtime.async_set_discharge_to_grid_schedule_time(
            start=start,
            end=end,
        )

    async def async_set_discharge_to_grid_schedule_limit(self, limit: int) -> None:
        await self.battery_runtime.async_set_discharge_to_grid_schedule_limit(limit)

    async def async_set_restrict_battery_discharge_schedule_enabled(
        self, enabled: bool
    ) -> None:
        await self.battery_runtime.async_set_restrict_battery_discharge_schedule_enabled(
            enabled
        )

    async def async_set_restrict_battery_discharge_schedule_time(
        self,
        *,
        start: dt_time | None = None,
        end: dt_time | None = None,
    ) -> None:
        await self.battery_runtime.async_set_restrict_battery_discharge_schedule_time(
            start=start,
            end=end,
        )

    async def async_set_restrict_battery_discharge_schedule_limit(
        self, limit: int
    ) -> None:
        await self.battery_runtime.async_set_restrict_battery_discharge_schedule_limit(
            limit
        )

    async def async_request_grid_toggle_otp(self) -> None:
        await self.battery_runtime.async_request_grid_toggle_otp()

    async def async_set_grid_mode(self, mode: str, otp: str) -> None:
        await self.battery_runtime.async_set_grid_mode(mode, otp)

    async def async_set_grid_connection(
        self, enabled: bool, *, otp: str | None = None
    ) -> None:
        await self.battery_runtime.async_set_grid_connection(enabled, otp=otp)

    async def async_set_battery_shutdown_level(
        self, level: int
    ) -> None:  # pragma: no cover
        await self.battery_runtime.async_set_battery_shutdown_level(level)

    def _parse_storm_guard_profile(  # pragma: no cover - compatibility shim
        self, payload: object
    ) -> tuple[str | None, bool | None]:
        if not isinstance(payload, dict):
            return None, None
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        state = self._normalize_storm_guard_state(data.get("stormGuardState"))
        evse = self._coerce_optional_bool(data.get("evseStormEnabled"))
        return state, evse

    def _parse_storm_alert(self, payload: object) -> bool | None:  # pragma: no cover
        return self.battery_runtime.parse_storm_alert(payload)

    def _storm_alert_status_is_inactive(
        self, status: str | None
    ) -> bool:  # pragma: no cover
        return self.battery_runtime.storm_alert_status_is_inactive(status)

    def _storm_alert_is_active(
        self, alert: dict[str, object]
    ) -> bool:  # pragma: no cover
        return self.battery_runtime.storm_alert_is_active(alert)

    async def _async_refresh_storm_guard_profile(self, *, force: bool = False) -> None:
        await self.battery_runtime.async_refresh_storm_guard_profile(force=force)

    async def async_refresh_storm_guard_profile(self, *, force: bool = False) -> None:
        await self._async_refresh_storm_guard_profile(force=force)

    async def _async_refresh_storm_alert(
        self,
        *,
        force: bool = False,
        raise_on_error: bool = False,
    ) -> None:
        if raise_on_error:
            await self.battery_runtime.async_refresh_storm_alert(
                force=force,
                raise_on_error=True,
            )
            return
        await self.battery_runtime.async_refresh_storm_alert(force=force)

    async def async_refresh_storm_alert(
        self,
        *,
        force: bool = False,
        raise_on_error: bool = False,
    ) -> None:
        if raise_on_error:
            await self._async_refresh_storm_alert(force=force, raise_on_error=True)
            return
        await self._async_refresh_storm_alert(force=force)

    async def async_opt_out_all_storm_alerts(self) -> None:
        await self.battery_runtime.async_opt_out_all_storm_alerts()

    async def async_set_storm_guard_enabled(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_storm_guard_enabled(enabled)

    async def async_set_storm_evse_enabled(self, enabled: bool) -> None:
        await self.battery_runtime.async_set_storm_evse_enabled(enabled)

    async def _async_resolve_green_battery_settings(
        self, serials: Iterable[str]
    ) -> dict[str, tuple[bool | None, bool]]:
        return await self.evse_runtime.async_resolve_green_battery_settings(serials)

    async def _async_resolve_auth_settings(
        self, serials: Iterable[str]
    ) -> dict[str, tuple[bool | None, bool | None, bool, bool]]:
        return await self.evse_runtime.async_resolve_auth_settings(serials)

    async def _async_resolve_charger_config(
        self,
        serials: Iterable[str],
        *,
        keys: Iterable[str],
    ) -> dict[str, dict[str, object]]:
        return await self.evse_runtime.async_resolve_charger_config(
            serials,
            keys=keys,
        )

    def _resolve_charge_mode_pref(self, sn: str) -> str | None:
        return self.evse_runtime.resolve_charge_mode_pref(sn)

    def _cached_charge_mode_preference(
        self, sn: str, *, now: float | None = None
    ) -> str | None:
        return self.evse_runtime.cached_charge_mode_preference(sn, now=now)

    @staticmethod
    def _schedule_type_charge_mode_preference(schedule_type: object) -> str | None:
        return EvseRuntime.schedule_type_charge_mode_preference(schedule_type)

    def _battery_profile_charge_mode_preference(self, sn: str) -> str | None:
        return self.evse_runtime.battery_profile_charge_mode_preference(sn)

    @staticmethod
    def _normalize_charge_mode_preference(value: object) -> str | None:
        return EvseRuntime.normalize_charge_mode_preference(value)

    def _normalize_effective_charge_mode(self, value: object) -> str | None:
        return self.evse_runtime.normalize_effective_charge_mode(value)

    def _charge_mode_start_preferences(self, sn: str) -> ChargeModeStartPreferences:
        return self.evse_runtime.charge_mode_start_preferences(sn)

    async def _ensure_charge_mode(self, sn: str, target_mode: str) -> None:
        await self.evse_runtime.async_ensure_charge_mode(sn, target_mode)


install_state_descriptors(EnphaseCoordinator)
