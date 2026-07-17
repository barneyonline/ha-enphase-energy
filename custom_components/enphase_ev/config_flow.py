"""Handle Enphase Enlighten authentication, discovery, and options flows."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from typing import Any, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import selector
from homeassistant.helpers.translation import (
    async_get_cached_translations,
    async_get_translations,
)

from .api import (
    AuthTokens,
    EnlightenAuthInvalidCredentials,
    EnlightenAuthInvalidOTP,
    EnlightenAuthMFARequired,
    EnlightenAuthOTPBlocked,
    EnlightenAuthTooManySessions,
    EnlightenAuthUnavailable,
    async_authenticate,
    async_fetch_hems_devices,
    async_fetch_devices_inventory,
    async_fetch_battery_site_settings,
    async_fetch_inverters_inventory,
    async_fetch_chargers,
    async_resend_login_otp,
    async_validate_login_otp,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AUTH_BLOCK_REASON,
    CONF_AUTH_BLOCKED_UNTIL,
    CONF_AUTH_REFRESH_SUSPENDED_UNTIL,
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_EMAIL,
    CONF_HEMS_AUTH_BACKOFF_UNTIL,
    CONF_HEMS_AUTH_FAILURE_COUNT,
    CONF_HEMS_AUTH_LAST_ENDPOINT,
    CONF_HEMS_AUTH_LAST_FAILURE_UTC,
    CONF_HEMS_AUTH_LAST_REASON,
    CONF_HEMS_AUTH_LAST_STATUS,
    CONF_HEMS_AUTH_LAST_SUCCESS_UTC,
    CONF_HEATPUMP_DISCOVERY_HANDLED,
    CONF_INCLUDE_INVERTERS,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SERIALS,
    CONF_SESSION_ID,
    CONF_SITE_ID,
    CONF_SITE_NAME,
    CONF_SITE_ONLY,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_BATTERY_SCHEDULES_ENABLED,
    DEFAULT_API_TIMEOUT,
    DEFAULT_DEGRADED_SERVICE_REPAIR_ISSUES,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
    DEFAULT_MICROINVERTER_POWER_ENABLED,
    DEFAULT_PRICING_EDITS_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCHEDULE_SYNC_ENABLED,
    DEFAULT_SYSTEM_EVENT_REPAIR_ISSUES,
    DEFAULT_SLOW_POLL_INTERVAL,
    DEFAULT_WEATHER_ENABLED,
    DOMAIN,
    MAX_API_TIMEOUT,
    MAX_POLL_INTERVAL,
    MAX_SESSION_HISTORY_INTERVAL_MIN,
    MIN_API_TIMEOUT,
    MIN_FAST_POLL_INTERVAL,
    MIN_SESSION_HISTORY_INTERVAL_MIN,
    MIN_SLOW_POLL_INTERVAL,
    OPT_API_TIMEOUT,
    OPT_BATTERY_SCHEDULES_ENABLED,
    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
    OPT_MICROINVERTER_POWER_ENABLED,
    OPT_PRICING_EDITS_ENABLED,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
    OPT_SESSION_HISTORY_INTERVAL,
    OPT_WEATHER_ENABLED,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
    OPT_SCHEDULE_SYNC_ENABLED,
    OPT_SYSTEM_EVENT_REPAIR_ISSUES,
)
from .device_types import (
    ONBOARDING_SUPPORTED_TYPE_KEYS,
    active_type_serials_from_inventory,
    active_type_keys_from_inventory,
    member_is_retired,
    normalize_type_key,
)
from .envoy_history import (
    EnvoyHistoryCandidate,
    EnvoyHistorySource,
    EnvoyHistoryTarget,
    candidate_options,
    discover_enphase_targets,
    discover_external_migration_candidates,
    discover_envoy_sources,
    execute_takeover,
    format_completed_preview,
    format_mapping_preview,
    format_selection_preview,
    format_warning_preview,
    migration_flow_fields,
    selection_uses_source,
    selected_mappings,
    skip_option_value,
    source_by_entry_id,
    source_options,
    suggest_mappings,
    validate_selected_mappings,
)
from .grid_profile_runtime import (
    ALL_PROFILES_OPTION,
    COMMONLY_USED_OPTION,
    SUPPORT_DENIED,
    GridProfile,
    GridProfileRuntime,
)
from .runtime_data import EnphaseConfigEntry
from .log_redaction import redact_site_id, redact_text
from .runtime_helpers import normalize_poll_intervals
from .voltage import coerce_nominal_voltage, resolve_nominal_voltage_for_hass

_LOGGER = logging.getLogger(__name__)

MFA_RESEND_DELAY_SECONDS = 30
CONF_OTP = "otp"
CONF_RESEND_CODE = "resend_code"
CONF_TYPE_ENVOY = "type_envoy"
CONF_TYPE_ENCHARGE = "type_encharge"
CONF_TYPE_AC_BATTERY = "type_ac_battery"
CONF_TYPE_IQEVSE = "type_iqevse"
CONF_TYPE_HEATPUMP = "type_heatpump"
CONF_TYPE_MICROINVERTER = "type_microinverter"
CONF_DEVICE_CATEGORIES_SECTION = "devices"
CONF_DEVICE_FEATURES_SECTION = "device_features"
CONF_MIGRATION_SOURCE_ENTRY = "selected_envoy_source"
CONF_MIGRATION_BACKUP_CONFIRMED = "backup_confirmed"
CONF_MIGRATION_CONFIRM_REASSIGN = "confirm_reassign"
CONF_MIGRATION_DISABLE_ARCHIVED = "disable_archived_envoy_sensors"
CONF_GRID_PROFILE_REGION = "grid_profile_region"
CONF_GRID_PROFILE_COMMONLY_USED = "grid_profile_commonly_used"
CONF_GRID_PROFILE_ID = "grid_profile_id"
CONF_GRID_PROFILE_CONFIRM_APPLY = "confirm_apply"
CONF_GRID_MODE = "mode"
CONF_GRID_MODE_CONFIRM = "confirm"

_GRID_PROFILE_LABEL_PREFIX = f"component.{DOMAIN}.selector.grid_profile_status.options."
_GRID_MODE_LABEL_PREFIX = f"component.{DOMAIN}.selector.grid_mode.options."
_GRID_CONTROL_BLOCK_REASON_LABEL_PREFIX = (
    f"component.{DOMAIN}.selector.grid_control_block_reason.options."
)

_TYPE_FIELD_BY_KEY: dict[str, str] = {
    "envoy": CONF_TYPE_ENVOY,
    "encharge": CONF_TYPE_ENCHARGE,
    "ac_battery": CONF_TYPE_AC_BATTERY,
    "iqevse": CONF_TYPE_IQEVSE,
    "heatpump": CONF_TYPE_HEATPUMP,
    "microinverter": CONF_TYPE_MICROINVERTER,
}


def _battery_site_settings_has_acb(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if isinstance(data, dict):
        payload = data
    value = payload.get("hasAcb")
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    try:
        text = str(value).strip().lower()
    except Exception:  # noqa: BLE001
        return False
    return text in {"1", "true", "yes", "on"}


def _site_entry_title(site_id: str) -> str:
    return f"Site: {site_id}"


def _coerce_int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int | None:
    number = _coerce_int_value(value)
    if number is None:
        return None
    if minimum <= number <= maximum:
        return number
    return None


def _clamped_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    number = _coerce_int_value(value)
    if number is None:
        return default
    return min(maximum, max(minimum, number))


def _hems_devices_groups(payload: object) -> list[dict[str, Any]]:
    """Return grouped HEMS members from the dedicated HEMS inventory payload."""

    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict):
        devices = result.get("devices")
        if isinstance(devices, list):
            return [grouped for grouped in devices if isinstance(grouped, dict)]
        if isinstance(devices, dict):
            return [devices]
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    hems_devices = (
        data.get("hems-devices")
        if data.get("hems-devices") is not None
        else data.get("hems_devices")
    )
    if not isinstance(hems_devices, dict):
        return []
    return [hems_devices]


def _hems_heatpump_available(payload: object) -> bool:
    """Return True when dedicated HEMS inventory exposes active heat-pump members."""

    for grouped in _hems_devices_groups(payload):
        for key in ("heat-pump", "heat_pump", "heatpump"):
            members = grouped.get(key)
            if not isinstance(members, list):
                continue
            if any(
                isinstance(member, dict) and not member_is_retired(member)
                for member in members
            ):
                return True
    return False


def _legacy_microinverters_available(payload: object) -> bool:
    """Return True when legacy inverter inventory exposes active members."""

    if not isinstance(payload, dict):
        return False
    inverters = payload.get("inverters")
    if not isinstance(inverters, list):
        result = payload.get("result")
        if isinstance(result, dict):
            inverters = result.get("inverters")
    if not isinstance(inverters, list):
        return False
    return any(
        isinstance(member, dict) and not member_is_retired(member)
        for member in inverters
    )


class EnphaseEVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg,misc]
    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._auth_tokens: AuthTokens | None = None
        self._sites: dict[str, str | None] = {}
        self._selected_site_id: str | None = None
        self._chargers: list[tuple[str, str | None]] = []
        self._chargers_loaded = False
        self._available_type_keys: list[str] = []
        self._inventory_iqevse_serials: list[str] = []
        self._type_keys_loaded = False
        self._inventory_unknown = False
        self._email: str | None = None
        self._remember_password = False
        self._password: str | None = None
        self._reconfigure_entry: EnphaseConfigEntry | None = None
        self._reauth_entry: EnphaseConfigEntry | None = None
        self._site_only = False
        self._include_inverters = True
        self._mfa_tokens: AuthTokens | None = None
        self._mfa_resend_available_at: float | None = None
        self._mfa_code_sent = False
        self._pending_user_errors: dict[str, str] | None = None

    @callback  # type: ignore[untyped-decorator]
    def _async_update_entry_and_abort(
        self,
        entry: EnphaseConfigEntry,
        *,
        data: Mapping[str, Any],
        reason: str,
        title: str | None = None,
    ) -> FlowResult:
        has_update_listeners = bool(getattr(entry, "update_listeners", ()))
        if has_update_listeners and callable(
            update_and_abort := getattr(self, "async_update_and_abort", None)
        ):
            if title is not None:
                return update_and_abort(
                    entry,
                    title=title,
                    data=data,
                    reason=reason,
                )
            return update_and_abort(entry, data=data, reason=reason)

        update_reload_and_abort = getattr(self, "async_update_reload_and_abort", None)
        if callable(update_reload_and_abort):
            if title is not None:
                return update_reload_and_abort(
                    entry,
                    title=title,
                    data=data,
                    reason=reason,
                )
            return update_reload_and_abort(entry, data=data, reason=reason)

        update_kwargs: dict[str, Any] = {"data": data}
        if title is not None:
            update_kwargs["title"] = title
        self.hass.config_entries.async_update_entry(entry, **update_kwargs)
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(entry.entry_id),
            f"reload {entry.domain} config entry {entry.entry_id}",
        )
        return self.async_abort(reason=reason)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is None and self._pending_user_errors:
            errors = self._pending_user_errors
            self._pending_user_errors = None

        if user_input is not None:
            self._pending_user_errors = None
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            remember = bool(user_input.get(CONF_REMEMBER_PASSWORD, False))
            self._clear_mfa()

            session = async_get_clientsession(self.hass)
            try:
                tokens, sites = await async_authenticate(session, email, password)
            except EnlightenAuthInvalidCredentials:
                errors["base"] = "invalid_auth"
            except EnlightenAuthTooManySessions:
                _LOGGER.warning(
                    "Enlighten rejected login because too many account sessions are active"
                )
                errors["base"] = "too_many_active_sessions"
            except EnlightenAuthMFARequired as err:
                self._email = email
                self._remember_password = remember
                self._password = password if remember else None
                if isinstance(err.tokens, AuthTokens) and err.tokens.raw_cookies:
                    # Enphase MFA validation depends on cookies from the first
                    # login response, not just the OTP entered on the next form.
                    self._start_mfa(err.tokens)
                    return await self.async_step_mfa()
                errors["base"] = "mfa_required"
            except EnlightenAuthUnavailable:
                errors["base"] = "service_unavailable"
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Unexpected error during Enlighten authentication: %s",
                    redact_text(err),
                )
                errors["base"] = "unknown"
            else:
                self._email = email
                self._remember_password = remember
                self._password = password if remember else None
                return await self._handle_auth_success(tokens, sites)

        defaults = {
            CONF_EMAIL: self._email or "",
            CONF_REMEMBER_PASSWORD: self._remember_password,
        }

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL, default=defaults[CONF_EMAIL]): selector(
                    {"text": {"type": "email"}}
                ),
                vol.Required(CONF_PASSWORD): selector({"text": {"type": "password"}}),
                vol.Optional(
                    CONF_REMEMBER_PASSWORD, default=defaults[CONF_REMEMBER_PASSWORD]
                ): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    def _start_mfa(self, tokens: AuthTokens) -> None:
        self._mfa_tokens = tokens
        self._mfa_resend_available_at = None
        self._mfa_code_sent = False

    def _clear_mfa(self) -> None:
        self._mfa_tokens = None
        self._mfa_resend_available_at = None
        self._mfa_code_sent = False

    def _mfa_can_resend(self) -> bool:
        if self._mfa_resend_available_at is None:
            return True
        return time.monotonic() >= self._mfa_resend_available_at

    async def _handle_auth_success(
        self, tokens: AuthTokens, sites: list[Any]
    ) -> FlowResult:
        self._auth_tokens = tokens
        self._sites = {site.site_id: site.name for site in sites}

        if self._reconfigure_entry:
            current_site = self._reconfigure_entry.data.get(CONF_SITE_ID)
            if current_site:
                current_site_id = str(current_site)
                if self._selected_site_id != current_site_id:
                    self._reset_discovery_cache()
                self._selected_site_id = current_site_id

        if len(self._sites) == 1 and not self._reconfigure_entry:
            selected_site = next(iter(self._sites))
            if self._selected_site_id != selected_site:
                self._reset_discovery_cache()
            self._selected_site_id = selected_site
            return await self.async_step_devices()
        return await self.async_step_site()

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if not self._mfa_tokens or not self._mfa_tokens.raw_cookies or not self._email:
            return self.async_abort(reason="unknown")

        errors: dict[str, str] = {}

        if user_input is None and not self._mfa_code_sent:
            errors = await self._send_mfa_code()
            self._mfa_code_sent = True
            if errors.get("base") == "invalid_auth":
                return await self._restart_login_with_error("invalid_auth")

        if user_input is not None:
            resend = bool(user_input.get(CONF_RESEND_CODE, False))
            otp = str(user_input.get(CONF_OTP, "")).strip()

            session = async_get_clientsession(self.hass)

            if resend:
                if not self._mfa_can_resend():
                    errors["base"] = "resend_wait"
                else:
                    errors = await self._send_mfa_code()
                    if errors.get("base") == "invalid_auth":
                        return await self._restart_login_with_error("invalid_auth")
            else:
                if not otp:
                    errors["base"] = "otp_required"
                else:
                    try:
                        tokens, sites = await async_validate_login_otp(
                            session,
                            self._email,
                            otp,
                            self._mfa_tokens.raw_cookies,
                        )
                    except EnlightenAuthInvalidOTP:
                        _LOGGER.warning("MFA code rejected by Enlighten")
                        errors["base"] = "otp_invalid"
                    except EnlightenAuthOTPBlocked:
                        _LOGGER.warning("MFA validation blocked by Enlighten")
                        errors["base"] = "otp_blocked"
                    except EnlightenAuthTooManySessions:
                        _LOGGER.warning(
                            "Enlighten rejected MFA validation because too many account sessions are active"
                        )
                        errors["base"] = "too_many_active_sessions"
                    except EnlightenAuthInvalidCredentials:
                        return await self._restart_login_with_error("invalid_auth")
                    except EnlightenAuthUnavailable:
                        _LOGGER.warning(
                            "Enlighten MFA validation temporarily unavailable"
                        )
                        errors["base"] = "service_unavailable"
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning(
                            "Unexpected error during Enlighten MFA validation: %s",
                            redact_text(err),
                        )
                        errors["base"] = "unknown"
                    else:
                        self._clear_mfa()
                        return await self._handle_auth_success(tokens, sites)

        schema = vol.Schema(
            {
                vol.Optional(CONF_OTP, default=""): selector(
                    {"text": {"type": "text"}}
                ),
                vol.Optional(CONF_RESEND_CODE, default=False): bool,
            }
        )
        return self.async_show_form(step_id="mfa", data_schema=schema, errors=errors)

    async def _send_mfa_code(self) -> dict[str, str]:
        if not self._mfa_tokens or not self._mfa_tokens.raw_cookies:
            return {"base": "unknown"}
        session = async_get_clientsession(self.hass)
        try:
            updated = await async_resend_login_otp(
                session, self._mfa_tokens.raw_cookies
            )
        except EnlightenAuthOTPBlocked:
            _LOGGER.warning("Enlighten MFA resend blocked")
            return {"base": "otp_blocked"}
        except EnlightenAuthTooManySessions:
            _LOGGER.warning(
                "Enlighten rejected MFA resend because too many account sessions are active"
            )
            return {"base": "too_many_active_sessions"}
        except EnlightenAuthInvalidCredentials:
            return {"base": "invalid_auth"}
        except EnlightenAuthUnavailable:
            _LOGGER.warning("Enlighten MFA resend temporarily unavailable")
            return {"base": "service_unavailable"}
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Unexpected error during Enlighten MFA resend: %s",
                redact_text(err),
            )
            return {"base": "unknown"}

        self._mfa_tokens = updated
        # Enphase can temporarily block repeated OTP sends, so the flow keeps a
        # local delay even when the backend response does not expose one.
        self._mfa_resend_available_at = time.monotonic() + MFA_RESEND_DELAY_SECONDS
        return {}

    async def _restart_login_with_error(self, error: str) -> FlowResult:
        _LOGGER.warning("Enlighten MFA session no longer valid; restarting login flow")
        self._clear_mfa()
        self._pending_user_errors = {"base": error}
        return await self.async_step_user()

    async def async_step_site(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if self._reconfigure_entry and self._selected_site_id and user_input is None:
            # Keep the existing site locked during reconfigure; skip the picker UX.
            return await self.async_step_devices()

        errors: dict[str, str] = {}

        if user_input is not None:
            site_id_raw = user_input.get(CONF_SITE_ID)
            site_id = str(site_id_raw).strip() if site_id_raw is not None else ""
            if site_id:
                if not site_id.isdigit():
                    errors["base"] = "site_invalid"
                else:
                    if self._selected_site_id != site_id:
                        self._reset_discovery_cache()
                    self._selected_site_id = site_id
                    if self._selected_site_id not in self._sites:
                        self._sites[self._selected_site_id] = None
                    return await self.async_step_devices()
            else:
                errors["base"] = "site_required"

        options = [
            {
                "value": site_id,
                "label": f"{name} ({site_id})" if name else site_id,
            }
            for site_id, name in self._sites.items()
        ]

        if options:
            default_site_id = None
            if self._selected_site_id and self._selected_site_id in self._sites:
                default_site_id = self._selected_site_id
            else:
                default_site_id = options[0]["value"]
            schema = vol.Schema(
                {
                    vol.Required(CONF_SITE_ID, default=default_site_id): selector(
                        {"select": {"options": options, "multiple": False}}
                    )
                }
            )
        else:
            schema = vol.Schema({vol.Required(CONF_SITE_ID): str})

        return self.async_show_form(step_id="site", data_schema=schema, errors=errors)

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        await self._ensure_device_selection_data()
        discovered_serials = self._discovered_serials()
        available_type_keys = self._available_type_keys_for_form(discovered_serials)
        default_selected_type_keys = self._default_selected_type_keys(
            available_type_keys
        )
        if (
            "microinverter" in _TYPE_FIELD_BY_KEY
            and "microinverter" not in available_type_keys
        ):
            available_type_keys.append("microinverter")

        default_scan = self._default_scan_interval()
        schema_fields: dict[vol.Marker, object] = {}
        for type_key in available_type_keys:
            field_key = _TYPE_FIELD_BY_KEY[type_key]
            schema_fields[
                vol.Optional(field_key, default=type_key in default_selected_type_keys)
            ] = bool
        schema_fields[vol.Optional(CONF_SCAN_INTERVAL, default=default_scan)] = vol.All(
            vol.Coerce(int),
            vol.Range(min=MIN_SLOW_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
        )
        schema = vol.Schema(schema_fields)

        if user_input is not None:
            selected_type_keys = self._selected_type_keys_from_user_input(
                user_input,
                available_type_keys,
                default_selected_type_keys=default_selected_type_keys,
            )
            selected_type_keys = self._merged_selected_type_keys_for_unknown_inventory(
                selected_type_keys, visible_type_keys=available_type_keys
            )
            scan_interval = _bounded_int(
                user_input.get(CONF_SCAN_INTERVAL, default_scan),
                minimum=MIN_SLOW_POLL_INTERVAL,
                maximum=MAX_POLL_INTERVAL,
            )
            if scan_interval is None:
                return self.async_show_form(
                    step_id="devices",
                    data_schema=self.add_suggested_values_to_schema(schema, user_input),
                    errors={CONF_SCAN_INTERVAL: "unknown"},
                )
            selected_serials = []
            if "iqevse" in selected_type_keys:
                selected_serials = self._selected_iqevse_serials(discovered_serials)
            include_inverters = "microinverter" in selected_type_keys
            site_only_selected = "iqevse" not in selected_type_keys
            self._site_only = site_only_selected
            self._include_inverters = include_inverters
            return await self._finalize_login_entry(
                selected_serials,
                scan_interval,
                site_only_selected,
                include_inverters=include_inverters,
                selected_type_keys=selected_type_keys,
                heatpump_visible="heatpump" in available_type_keys,
            )

        errors: dict[str, str] = {}
        if self._inventory_unknown:
            errors["base"] = "service_unavailable"
        return self.async_show_form(
            step_id="devices",
            data_schema=schema,
            errors=errors,
        )

    async def _finalize_login_entry(
        self,
        serials: list[str],
        scan_interval: int,
        site_only: bool = False,
        *,
        include_inverters: bool = True,
        selected_type_keys: list[str] | None = None,
        heatpump_visible: bool = False,
    ) -> FlowResult:
        if not self._auth_tokens or not self._selected_site_id:
            return self.async_abort(reason="unknown")

        if selected_type_keys is None:
            selected_type_keys = self._legacy_selected_type_keys(
                serials,
                include_inverters,
                site_only=site_only,
            )

        site_name = self._sites.get(self._selected_site_id)
        data = {
            CONF_SITE_ID: self._selected_site_id,
            CONF_SITE_NAME: site_name,
            CONF_SERIALS: serials,
            CONF_SCAN_INTERVAL: scan_interval,
            CONF_COOKIE: self._auth_tokens.cookie,
            CONF_EAUTH: self._auth_tokens.access_token,
            CONF_ACCESS_TOKEN: self._auth_tokens.access_token,
            CONF_SESSION_ID: self._auth_tokens.session_id,
            CONF_TOKEN_EXPIRES_AT: self._auth_tokens.token_expires_at,
            CONF_REMEMBER_PASSWORD: self._remember_password,
            CONF_EMAIL: self._email,
            CONF_SITE_ONLY: bool(site_only),
            CONF_INCLUDE_INVERTERS: bool(include_inverters),
            CONF_SELECTED_TYPE_KEYS: list(dict.fromkeys(selected_type_keys)),
        }
        # Stored credentials are optional and only retained when the user opted
        # in; cookies/tokens are still needed for normal cloud refreshes.
        prior_heatpump_discovery_handled = bool(
            self._reconfigure_entry
            and self._reconfigure_entry.data.get(CONF_HEATPUMP_DISCOVERY_HANDLED, False)
        )
        if heatpump_visible or prior_heatpump_discovery_handled:
            data[CONF_HEATPUMP_DISCOVERY_HANDLED] = True
        if self._remember_password and self._password:
            data[CONF_PASSWORD] = self._password
        else:
            data.pop(CONF_PASSWORD, None)
        data.pop(CONF_AUTH_REFRESH_SUSPENDED_UNTIL, None)
        data.pop(CONF_AUTH_BLOCKED_UNTIL, None)
        data.pop(CONF_AUTH_BLOCK_REASON, None)
        data.pop(CONF_HEMS_AUTH_BACKOFF_UNTIL, None)
        data.pop(CONF_HEMS_AUTH_FAILURE_COUNT, None)
        data.pop(CONF_HEMS_AUTH_LAST_FAILURE_UTC, None)
        data.pop(CONF_HEMS_AUTH_LAST_SUCCESS_UTC, None)
        data.pop(CONF_HEMS_AUTH_LAST_ENDPOINT, None)
        data.pop(CONF_HEMS_AUTH_LAST_STATUS, None)
        data.pop(CONF_HEMS_AUTH_LAST_REASON, None)

        await self.async_set_unique_id(self._selected_site_id)

        if self._reconfigure_entry:
            reason = (
                "reauth_successful" if self._reauth_entry else "reconfigure_successful"
            )
            current_site_id_raw = (
                self._reconfigure_entry.unique_id
                or self._reconfigure_entry.data.get(CONF_SITE_ID)
            )
            current_site_id = (
                str(current_site_id_raw) if current_site_id_raw is not None else None
            )
            desired_site_id = self._selected_site_id
            if (
                current_site_id
                and desired_site_id
                and current_site_id != desired_site_id
            ):
                current_site_name = self._reconfigure_entry.data.get(CONF_SITE_NAME)
                desired_site_name = self._sites.get(desired_site_id)
                configured_label = (
                    f"{current_site_name} ({current_site_id})"
                    if current_site_name and current_site_id
                    else current_site_name or current_site_id or "current site"
                )
                requested_label = (
                    f"{desired_site_name} ({desired_site_id})"
                    if desired_site_name and desired_site_id
                    else desired_site_name or desired_site_id or "selected site"
                )
                return self.async_abort(
                    reason="wrong_account",
                    description_placeholders={
                        "configured_label": configured_label,
                        "requested_label": requested_label,
                    },
                )

            self._abort_if_unique_id_mismatch(reason="wrong_account")
            desired_title = _site_entry_title(str(self._selected_site_id))
            merged = dict(self._reconfigure_entry.data)
            for key, value in data.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            merged.pop(CONF_AUTH_REFRESH_SUSPENDED_UNTIL, None)
            merged.pop(CONF_AUTH_BLOCKED_UNTIL, None)
            merged.pop(CONF_AUTH_BLOCK_REASON, None)
            merged.pop(CONF_HEMS_AUTH_BACKOFF_UNTIL, None)
            merged.pop(CONF_HEMS_AUTH_FAILURE_COUNT, None)
            merged.pop(CONF_HEMS_AUTH_LAST_FAILURE_UTC, None)
            merged.pop(CONF_HEMS_AUTH_LAST_SUCCESS_UTC, None)
            merged.pop(CONF_HEMS_AUTH_LAST_ENDPOINT, None)
            merged.pop(CONF_HEMS_AUTH_LAST_STATUS, None)
            merged.pop(CONF_HEMS_AUTH_LAST_REASON, None)
            if not self._remember_password:
                merged.pop(CONF_PASSWORD, None)
            title = (
                desired_title
                if self._reconfigure_entry.title != desired_title
                else None
            )
            return self._async_update_entry_and_abort(
                self._reconfigure_entry,
                title=title,
                data=merged,
                reason=reason,
            )

        self._abort_if_unique_id_configured()
        title = _site_entry_title(str(self._selected_site_id))
        return self.async_create_entry(title=title, data=data)

    async def _ensure_chargers(self) -> None:
        if self._chargers_loaded:
            return
        if self._inventory_iqevse_serials:
            self._chargers = [
                (serial, None) for serial in self._inventory_iqevse_serials
            ]
            self._chargers_loaded = True
            return
        if not self._auth_tokens or not self._selected_site_id:
            self._chargers_loaded = True
            return
        session = async_get_clientsession(self.hass)
        chargers = await async_fetch_chargers(
            session, self._selected_site_id, self._auth_tokens
        )
        self._chargers = [(c.serial, c.name) for c in chargers]
        self._chargers_loaded = True

    async def _ensure_available_type_keys(self) -> None:
        if self._type_keys_loaded:
            return
        self._type_keys_loaded = True
        self._inventory_unknown = False
        if not self._auth_tokens or not self._selected_site_id:
            self._available_type_keys = []
            self._inventory_iqevse_serials = []
            return
        session = async_get_clientsession(self.hass)
        discovery_results = await asyncio.gather(
            async_fetch_devices_inventory(
                session, self._selected_site_id, self._auth_tokens
            ),
            async_fetch_hems_devices(
                session, self._selected_site_id, self._auth_tokens, refresh_data=False
            ),
            async_fetch_battery_site_settings(
                session, self._selected_site_id, self._auth_tokens
            ),
            async_fetch_inverters_inventory(
                session, self._selected_site_id, self._auth_tokens
            ),
            return_exceptions=True,
        )
        payload: object = discovery_results[0]
        hems_payload: object = discovery_results[1]
        battery_site_settings: object = discovery_results[2]
        legacy_inverters: object = discovery_results[3]
        if isinstance(payload, Exception):
            _LOGGER.debug(
                "Failed to fetch device inventory during setup for site %s: %s",
                redact_site_id(self._selected_site_id),
                redact_text(payload, site_ids=(self._selected_site_id,)),
            )
            payload = None
        if isinstance(hems_payload, Exception):
            _LOGGER.debug(
                "Failed to fetch HEMS inventory during setup for site %s: %s",
                redact_site_id(self._selected_site_id),
                redact_text(hems_payload, site_ids=(self._selected_site_id,)),
            )
            hems_payload = None
        if isinstance(battery_site_settings, Exception):
            _LOGGER.debug(
                "Failed to fetch battery site settings during setup for site %s: %s",
                redact_site_id(self._selected_site_id),
                redact_text(battery_site_settings, site_ids=(self._selected_site_id,)),
            )
            battery_site_settings = None
        if isinstance(legacy_inverters, Exception):
            _LOGGER.debug(
                "Failed to fetch legacy inverter inventory during setup for site %s: %s",
                redact_site_id(self._selected_site_id),
                redact_text(legacy_inverters, site_ids=(self._selected_site_id,)),
            )
            legacy_inverters = None
        if payload is None:
            self._inventory_unknown = True
            self._available_type_keys = []
            self._inventory_iqevse_serials = []
        else:
            self._inventory_iqevse_serials = active_type_serials_from_inventory(
                payload, type_key="iqevse"
            )
            self._available_type_keys = [
                key
                for key in active_type_keys_from_inventory(
                    payload,
                    allowed_type_keys=ONBOARDING_SUPPORTED_TYPE_KEYS,
                )
                if key in _TYPE_FIELD_BY_KEY
            ]
        if "microinverter" not in self._available_type_keys:
            if _legacy_microinverters_available(legacy_inverters):
                self._inventory_unknown = False
                self._available_type_keys.append("microinverter")
        if _hems_heatpump_available(hems_payload) and "heatpump" in _TYPE_FIELD_BY_KEY:
            if "heatpump" not in self._available_type_keys:
                self._available_type_keys.append("heatpump")
        if _battery_site_settings_has_acb(battery_site_settings):
            if "ac_battery" not in self._available_type_keys:
                self._available_type_keys.append("ac_battery")
        self._available_type_keys = [
            key
            for key in ONBOARDING_SUPPORTED_TYPE_KEYS
            if key in self._available_type_keys and key in _TYPE_FIELD_BY_KEY
        ]

    async def _ensure_device_selection_data(self) -> None:
        if not self._type_keys_loaded:
            await self._ensure_available_type_keys()
        if not self._chargers_loaded:
            await self._ensure_chargers()

    def _reset_discovery_cache(self) -> None:
        self._chargers = []
        self._chargers_loaded = False
        self._available_type_keys = []
        self._inventory_iqevse_serials = []
        self._type_keys_loaded = False
        self._inventory_unknown = False

    def _normalize_serials(self, value: Any) -> list[str]:
        if isinstance(value, list):
            iterable = value
        elif isinstance(value, str):
            iterable = re.split(r"[,\n]+", value)
        else:
            iterable = []
        serials = []
        for item in iterable:
            itm = str(item).strip()
            if itm and itm not in serials:
                serials.append(itm)
        return serials

    def _discovered_serials(self) -> list[str]:
        return [serial for serial, _name in self._chargers if serial]

    def _selected_iqevse_serials(self, discovered_serials: list[str]) -> list[str]:
        serials: list[str] = []
        for source in (
            discovered_serials,
            self._inventory_iqevse_serials,
            self._stored_configured_serials(),
        ):
            for serial in source:
                if serial and serial not in serials:
                    serials.append(serial)
        return serials

    def _available_type_keys_for_form(self, discovered_serials: list[str]) -> list[str]:
        available = list(self._available_type_keys)
        if self._inventory_unknown:
            available.extend(
                self._fallback_type_keys_for_unknown_inventory(discovered_serials)
            )
        if discovered_serials and "iqevse" not in available:
            available.append("iqevse")
        ordered: list[str] = []
        for type_key in ONBOARDING_SUPPORTED_TYPE_KEYS:
            if type_key in available and type_key in _TYPE_FIELD_BY_KEY:
                ordered.append(type_key)
        return ordered

    def _default_include_inverters(self) -> bool:
        if self._reconfigure_entry:
            return bool(self._reconfigure_entry.data.get(CONF_INCLUDE_INVERTERS, True))
        return bool(self._include_inverters)

    def _default_scan_interval(self) -> int:
        if self._reconfigure_entry:
            return _clamped_int(
                self._reconfigure_entry.data.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                ),
                default=DEFAULT_SCAN_INTERVAL,
                minimum=MIN_SLOW_POLL_INTERVAL,
                maximum=MAX_POLL_INTERVAL,
            )
        return int(DEFAULT_SCAN_INTERVAL)

    def _normalize_type_keys(self, value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            iterable = value
        elif isinstance(value, str):
            iterable = re.split(r"[,\n]+", value)
        else:
            iterable = []
        out: list[str] = []
        for item in iterable:
            normalized = normalize_type_key(item)
            if (
                normalized
                and normalized in _TYPE_FIELD_BY_KEY
                and normalized not in out
            ):
                out.append(normalized)
        return out

    def _default_selected_type_keys(self, available_type_keys: list[str]) -> list[str]:
        if (
            self._reconfigure_entry
            and CONF_SELECTED_TYPE_KEYS in self._reconfigure_entry.data
        ):
            configured = self._normalize_type_keys(
                self._reconfigure_entry.data.get(CONF_SELECTED_TYPE_KEYS, [])
            )
            selected = set(configured)
            heatpump_discovery_handled = bool(
                self._reconfigure_entry.data.get(CONF_HEATPUMP_DISCOVERY_HANDLED, False)
            )
            # Auto-select heatpump only until the user has completed one
            # save where the heatpump option was visible.
            if (
                "heatpump" in available_type_keys
                and "heatpump" not in selected
                and not heatpump_discovery_handled
            ):
                selected.add("heatpump")
            return [key for key in available_type_keys if key in selected]

        selected = set(available_type_keys)
        if self._reconfigure_entry:
            configured_serials = self._normalize_serials(
                self._reconfigure_entry.data.get(CONF_SERIALS, [])
            )
            if not configured_serials or bool(
                self._reconfigure_entry.data.get(CONF_SITE_ONLY, False)
            ):
                selected.discard("iqevse")
            if not bool(self._reconfigure_entry.data.get(CONF_INCLUDE_INVERTERS, True)):
                selected.discard("microinverter")
        else:
            if self._site_only:
                selected.discard("iqevse")
            if not self._include_inverters:
                selected.discard("microinverter")
        return [key for key in available_type_keys if key in selected]

    def _selected_type_keys_from_user_input(
        self,
        user_input: dict[str, Any],
        available_type_keys: list[str],
        *,
        default_selected_type_keys: list[str],
    ) -> list[str]:
        selected: list[str] = []
        for type_key in available_type_keys:
            field_key = _TYPE_FIELD_BY_KEY.get(type_key)
            if not field_key:
                continue
            enabled = bool(
                user_input.get(field_key, type_key in default_selected_type_keys)
            )
            if enabled:
                selected.append(type_key)
        return selected

    def _legacy_selected_type_keys(
        self,
        serials: list[str],
        include_inverters: bool,
        *,
        site_only: bool = False,
    ) -> list[str]:
        discovered_serials = self._discovered_serials()
        available_type_keys = self._available_type_keys_for_form(discovered_serials)
        if available_type_keys:
            selected = set(available_type_keys)
            if site_only or not serials:
                selected.discard("iqevse")
            if not include_inverters:
                selected.discard("microinverter")
            return [key for key in available_type_keys if key in selected]

        fallback_selected: list[str] = ["envoy", "encharge"]
        if serials and not site_only:
            fallback_selected.append("iqevse")
        if include_inverters:
            fallback_selected.append("microinverter")
        return fallback_selected

    def _stored_selected_type_keys(self) -> list[str]:
        if not self._reconfigure_entry:
            return []
        if CONF_SELECTED_TYPE_KEYS in self._reconfigure_entry.data:
            return self._normalize_type_keys(
                self._reconfigure_entry.data.get(CONF_SELECTED_TYPE_KEYS, [])
            )
        return self._legacy_selected_type_keys(
            self._normalize_serials(self._reconfigure_entry.data.get(CONF_SERIALS, [])),
            bool(self._reconfigure_entry.data.get(CONF_INCLUDE_INVERTERS, True)),
            site_only=bool(self._reconfigure_entry.data.get(CONF_SITE_ONLY, False)),
        )

    def _stored_configured_serials(self) -> list[str]:
        if not self._reconfigure_entry:
            return []
        return self._normalize_serials(
            self._reconfigure_entry.data.get(CONF_SERIALS, [])
        )

    def _fallback_type_keys_for_unknown_inventory(
        self, discovered_serials: list[str]
    ) -> list[str]:
        selected = self._stored_selected_type_keys()
        if selected:
            return selected
        fallback = ["envoy", "encharge"]
        if "ac_battery" in self._available_type_keys:
            fallback.append("ac_battery")
        if discovered_serials:
            fallback.append("iqevse")
        if self._default_include_inverters():
            fallback.append("microinverter")
        return fallback

    def _merged_selected_type_keys_for_unknown_inventory(
        self, selected_type_keys: list[str], *, visible_type_keys: list[str]
    ) -> list[str]:
        if not self._inventory_unknown:
            return selected_type_keys
        stored_selected = set(self._stored_selected_type_keys())
        visible = set(visible_type_keys)
        merged = list(selected_type_keys)
        for key in stored_selected:
            if key not in visible and key not in merged and key in _TYPE_FIELD_BY_KEY:
                merged.append(key)
        return merged

    def _get_reconfigure_entry(self) -> EnphaseConfigEntry:
        return cast(EnphaseConfigEntry, super()._get_reconfigure_entry())

    def _get_reauth_entry(self) -> EnphaseConfigEntry:
        return cast(EnphaseConfigEntry, super()._get_reauth_entry())

    def _abort_if_unique_id_mismatch(self, *, reason: str) -> None:
        super()._abort_if_unique_id_mismatch(reason=reason)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._reconfigure_entry = self._get_reconfigure_entry()
        if not self._reconfigure_entry:
            return self.async_abort(reason="unknown")
        has_email = bool(self._reconfigure_entry.data.get(CONF_EMAIL))
        if not has_email:
            return self.async_abort(reason="manual_mode_removed")
        self._email = self._reconfigure_entry.data.get(CONF_EMAIL)
        stored_remember_password = bool(
            self._reconfigure_entry.data.get(CONF_REMEMBER_PASSWORD)
        )
        self._remember_password = stored_remember_password
        self._site_only = bool(self._reconfigure_entry.data.get(CONF_SITE_ONLY, False))
        self._include_inverters = bool(
            self._reconfigure_entry.data.get(CONF_INCLUDE_INVERTERS, True)
        )
        if stored_remember_password:
            self._password = self._reconfigure_entry.data.get(CONF_PASSWORD)
        else:
            self._password = None
        return await self.async_step_user()

    async def async_step_reauth(
        self, entry_data: dict[str, Any] | None = None
    ) -> FlowResult:
        _ = entry_data
        self._reauth_entry = self._get_reauth_entry()
        self._reconfigure_entry = self._reauth_entry
        if not self._reauth_entry:
            return self.async_abort(reason="unknown")
        has_email = bool(self._reauth_entry.data.get(CONF_EMAIL))
        if not has_email:
            return self.async_abort(reason="manual_mode_removed")
        self._email = self._reauth_entry.data.get(CONF_EMAIL)
        stored_remember_password = bool(
            self._reauth_entry.data.get(CONF_REMEMBER_PASSWORD)
        )
        self._remember_password = stored_remember_password
        self._site_only = bool(self._reauth_entry.data.get(CONF_SITE_ONLY, False))
        self._include_inverters = bool(
            self._reauth_entry.data.get(CONF_INCLUDE_INVERTERS, True)
        )
        if stored_remember_password:
            self._password = self._reauth_entry.data.get(CONF_PASSWORD)
        else:
            self._password = None
        return await self.async_step_user()

    @staticmethod
    @callback  # type: ignore[untyped-decorator]
    def async_get_options_flow(
        config_entry: EnphaseConfigEntry,
    ) -> OptionsFlowHandler:
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):  # type: ignore[misc]
    def __init__(self, config_entry: EnphaseConfigEntry) -> None:
        super().__init__()
        self._entry = config_entry
        self._migration_sources: list[EnvoyHistorySource] | None = None
        self._migration_targets: dict[str, EnvoyHistoryTarget] | None = None
        self._migration_extra_candidates: list[EnvoyHistoryCandidate] | None = None
        self._selected_migration_source_id: str | None = None
        self._migration_selection: dict[str, str] = {}
        self._grid_profile_apply_result: dict[str, object] | None = None
        self._grid_mode_target: str | None = None

    @staticmethod
    def _normalize_serials(value: Any) -> list[str]:
        if isinstance(value, list):
            iterable = value
        elif isinstance(value, str):
            iterable = re.split(r"[,\n]+", value)
        else:
            iterable = []
        serials: list[str] = []
        for item in iterable:
            serial = str(item).strip()
            if serial and serial not in serials:
                serials.append(serial)
        return serials

    @staticmethod
    def _normalize_type_keys(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            iterable = value
        elif isinstance(value, str):
            iterable = re.split(r"[,\n]+", value)
        else:
            iterable = []
        selected: list[str] = []
        for item in iterable:
            key = normalize_type_key(item)
            if key and key in _TYPE_FIELD_BY_KEY and key not in selected:
                selected.append(key)
        return selected

    @staticmethod
    def _normalize_any_type_keys(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            iterable = value
        elif isinstance(value, str):
            iterable = re.split(r"[,\n]+", value)
        else:
            iterable = []
        selected: list[str] = []
        for item in iterable:
            key = normalize_type_key(item)
            if key and key not in selected:
                selected.append(key)
        return selected

    def _legacy_selected_type_keys(
        self,
        serials: list[str],
        include_inverters: bool,
        *,
        site_only: bool = False,
    ) -> list[str]:
        selected = ["envoy", "encharge"]
        if serials and not site_only:
            selected.append("iqevse")
        if include_inverters:
            selected.append("microinverter")
        return selected

    def _stored_selected_type_keys(self) -> list[str]:
        if CONF_SELECTED_TYPE_KEYS in self._entry.data:
            return self._normalize_any_type_keys(
                self._entry.data.get(CONF_SELECTED_TYPE_KEYS, [])
            )
        return self._legacy_selected_type_keys(
            self._normalize_serials(self._entry.data.get(CONF_SERIALS, [])),
            bool(self._entry.data.get(CONF_INCLUDE_INVERTERS, True)),
            site_only=bool(self._entry.data.get(CONF_SITE_ONLY, False)),
        )

    def _default_selected_type_keys(self) -> list[str]:
        selected = set(self._stored_selected_type_keys())
        return [key for key in ONBOARDING_SUPPORTED_TYPE_KEYS if key in selected]

    def _default_nominal_voltage(self) -> int:
        configured: int | None = coerce_nominal_voltage(
            self._entry.options.get(OPT_NOMINAL_VOLTAGE)
        )
        if configured is not None:
            return configured

        runtime_data = getattr(self._entry, "runtime_data", None)
        coordinator = getattr(runtime_data, "coordinator", None)
        if coordinator is not None:
            preferred = getattr(coordinator, "preferred_nominal_voltage", None)
            if callable(preferred):
                value: int | None = coerce_nominal_voltage(preferred())
                if value is not None:
                    return value
            nominal: int | None = coerce_nominal_voltage(
                getattr(coordinator, "nominal_voltage", None)
            )
            if nominal is not None:
                return nominal

        return int(resolve_nominal_voltage_for_hass(self.hass))

    def _entry_auth_tokens(self) -> AuthTokens | None:
        site_id = str(self._entry.data.get(CONF_SITE_ID, "") or "").strip()
        access_token = self._entry.data.get(CONF_EAUTH) or self._entry.data.get(
            CONF_ACCESS_TOKEN
        )
        cookie = self._entry.data.get(CONF_COOKIE)
        if not site_id or not access_token or not cookie:
            return None
        return AuthTokens(
            cookie=str(cookie),
            session_id=self._entry.data.get(CONF_SESSION_ID),
            access_token=access_token,
            token_expires_at=self._entry.data.get(CONF_TOKEN_EXPIRES_AT),
        )

    async def _ac_battery_supported_for_options(self) -> bool:
        selected = set(self._stored_selected_type_keys())
        if "ac_battery" in selected:
            return True
        tokens = self._entry_auth_tokens()
        site_id = str(self._entry.data.get(CONF_SITE_ID, "") or "").strip()
        if tokens is None or not site_id:
            return False
        payload = await async_fetch_battery_site_settings(
            async_get_clientsession(self.hass),
            site_id,
            tokens,
        )
        return _battery_site_settings_has_acb(payload)

    async def _settings_type_keys(self) -> list[str]:
        visible: list[str] = []
        ac_battery_supported = await self._ac_battery_supported_for_options()
        for type_key in ONBOARDING_SUPPORTED_TYPE_KEYS:
            if type_key == "ac_battery" and not ac_battery_supported:
                continue
            if type_key in _TYPE_FIELD_BY_KEY:
                visible.append(type_key)
        return visible

    def _build_devices_schema(self, visible_type_keys: list[str]) -> vol.Schema:
        default_selected_type_keys = self._default_selected_type_keys()
        category_fields: dict[vol.Marker, object] = {}
        for type_key in visible_type_keys:
            field_key = _TYPE_FIELD_BY_KEY.get(type_key)
            if field_key is None:
                continue
            category_fields[
                vol.Optional(field_key, default=type_key in default_selected_type_keys)
            ] = bool
        feature_fields: dict[vol.Marker, object] = {
            vol.Optional(
                OPT_SCHEDULE_SYNC_ENABLED,
                default=self._entry.options.get(
                    OPT_SCHEDULE_SYNC_ENABLED,
                    DEFAULT_SCHEDULE_SYNC_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_BATTERY_SCHEDULES_ENABLED,
                default=self._entry.options.get(
                    OPT_BATTERY_SCHEDULES_ENABLED,
                    DEFAULT_BATTERY_SCHEDULES_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_PRICING_EDITS_ENABLED,
                default=self._entry.options.get(
                    OPT_PRICING_EDITS_ENABLED,
                    DEFAULT_PRICING_EDITS_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_WEATHER_ENABLED,
                default=self._entry.options.get(
                    OPT_WEATHER_ENABLED,
                    DEFAULT_WEATHER_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                default=self._entry.options.get(
                    OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                    DEFAULT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_MICROINVERTER_POWER_ENABLED,
                default=self._entry.options.get(
                    OPT_MICROINVERTER_POWER_ENABLED,
                    DEFAULT_MICROINVERTER_POWER_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_NOMINAL_VOLTAGE,
                default=self._default_nominal_voltage(),
            ): int,
        }
        return vol.Schema(
            {
                vol.Required(CONF_DEVICE_CATEGORIES_SECTION): section(
                    vol.Schema(category_fields)
                ),
                vol.Required(CONF_DEVICE_FEATURES_SECTION): section(
                    vol.Schema(feature_fields)
                ),
            }
        )

    @staticmethod
    def _build_authentication_schema() -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional("reauth", default=False): bool,
                vol.Optional("forget_password", default=False): bool,
            }
        )

    def _build_repair_notifications_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional(
                    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
                    default=self._entry.options.get(
                        OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
                        DEFAULT_DEGRADED_SERVICE_REPAIR_ISSUES,
                    ),
                ): bool,
                vol.Optional(
                    OPT_SYSTEM_EVENT_REPAIR_ISSUES,
                    default=self._entry.options.get(
                        OPT_SYSTEM_EVENT_REPAIR_ISSUES,
                        DEFAULT_SYSTEM_EVENT_REPAIR_ISSUES,
                    ),
                ): bool,
            }
        )

    def _build_general_settings_schema(self) -> vol.Schema:
        fast_default, slow_default = normalize_poll_intervals(
            self._entry.options.get(OPT_FAST_POLL_INTERVAL, DEFAULT_FAST_POLL_INTERVAL),
            self._entry.options.get(OPT_SLOW_POLL_INTERVAL, DEFAULT_SLOW_POLL_INTERVAL),
        )
        schema_fields: dict[vol.Marker, object] = {
            vol.Optional(
                OPT_FAST_POLL_INTERVAL,
                default=fast_default,
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_FAST_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
            ),
            vol.Optional(
                OPT_SLOW_POLL_INTERVAL,
                default=slow_default,
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_SLOW_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
            ),
            vol.Optional(
                OPT_FAST_WHILE_STREAMING,
                default=self._entry.options.get(OPT_FAST_WHILE_STREAMING, True),
            ): bool,
            vol.Optional(
                OPT_API_TIMEOUT,
                default=self._entry.options.get(OPT_API_TIMEOUT, DEFAULT_API_TIMEOUT),
            ): selector(
                {
                    "number": {
                        "min": MIN_API_TIMEOUT,
                        "max": MAX_API_TIMEOUT,
                        "step": 1,
                        "mode": "box",
                        "unit_of_measurement": "s",
                    }
                }
            ),
            vol.Optional(
                OPT_SESSION_HISTORY_INTERVAL,
                default=self._entry.options.get(
                    OPT_SESSION_HISTORY_INTERVAL,
                    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
                ),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(
                    min=MIN_SESSION_HISTORY_INTERVAL_MIN,
                    max=MAX_SESSION_HISTORY_INTERVAL_MIN,
                ),
            ),
        }
        base_schema = vol.Schema(schema_fields)
        return self.add_suggested_values_to_schema(base_schema, self._entry.options)

    def _build_settings_schema(self) -> vol.Schema:
        return self._build_general_settings_schema()

    def _build_schema(self) -> vol.Schema:
        """Backward-compatible alias for tests and legacy direct calls."""

        return self._build_general_settings_schema()

    async def _load_migration_sources(self) -> list[EnvoyHistorySource]:
        if self._migration_sources is None:
            self._migration_sources = await discover_envoy_sources(self.hass)
        return self._migration_sources

    def _load_migration_targets(self) -> dict[str, EnvoyHistoryTarget]:
        if self._migration_targets is None:
            self._migration_targets = discover_enphase_targets(self.hass, self._entry)
        return self._migration_targets

    async def _load_migration_extra_candidates(self) -> list[EnvoyHistoryCandidate]:
        if self._migration_extra_candidates is None:
            self._migration_extra_candidates = (
                await discover_external_migration_candidates(self.hass, self._entry)
            )
        return self._migration_extra_candidates

    async def _selected_migration_source(self) -> EnvoyHistorySource | None:
        return source_by_entry_id(
            await self._load_migration_sources(), self._selected_migration_source_id
        )

    async def _async_reload_migration_source_entry(
        self,
        source: EnvoyHistorySource,
        source_entry: (
            config_entries.ConfigEntry | None
        ),  # quality-scale: external-config-entry
    ) -> bool:
        if source_entry is None:
            return True
        try:
            reloaded = await self.hass.config_entries.async_reload(source.entry_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed reloading Envoy source entry after migration: %s",
                redact_text(err),
            )
            return False
        if reloaded:
            object.__setattr__(
                source_entry,
                "state",
                config_entries.ConfigEntryState.LOADED,
            )
        return bool(reloaded)

    def _migration_flow_keys(self) -> tuple[str, ...]:
        targets = self._load_migration_targets()
        return tuple(
            flow_key for flow_key in migration_flow_fields() if flow_key in targets
        )

    def _build_migration_source_schema(
        self, sources: list[EnvoyHistorySource]
    ) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_MIGRATION_SOURCE_ENTRY): selector(
                    {
                        "select": {
                            "options": source_options(sources),
                            "mode": "dropdown",
                        }
                    }
                )
            }
        )

    def _build_migration_intro_schema(self) -> vol.Schema:
        return vol.Schema(
            {vol.Required(CONF_MIGRATION_BACKUP_CONFIRMED, default=False): bool}
        )

    def _build_migration_mapping_schema(
        self,
        source: EnvoyHistorySource,
        extra_candidates: list[EnvoyHistoryCandidate],
        defaults: dict[str, str] | None = None,
    ) -> vol.Schema:
        defaults = defaults or {}
        field_schema: dict[vol.Marker, object] = {}
        selector_config = {
            "select": {
                "options": candidate_options(source, extra_candidates),
                "mode": "dropdown",
            }
        }
        for flow_key in self._migration_flow_keys():
            default_value = defaults.get(flow_key)
            marker = (
                vol.Optional(flow_key)
                if default_value is None
                else vol.Optional(flow_key, default=default_value)
            )
            field_schema[marker] = selector(selector_config)
        return vol.Schema(field_schema)

    def _build_migration_confirm_schema(
        self, *, disable_archived_default: bool
    ) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_MIGRATION_CONFIRM_REASSIGN, default=False): bool,
                vol.Required(
                    CONF_MIGRATION_DISABLE_ARCHIVED,
                    default=disable_archived_default,
                ): bool,
            }
        )

    async def _discover_iqevse_serials(self) -> list[str]:
        site_id = str(self._entry.data.get(CONF_SITE_ID, "")).strip()
        if not site_id:
            return []

        tokens = AuthTokens(
            cookie=str(self._entry.data.get(CONF_COOKIE, "") or ""),
            session_id=self._entry.data.get(CONF_SESSION_ID),
            access_token=self._entry.data.get(CONF_EAUTH)
            or self._entry.data.get(CONF_ACCESS_TOKEN),
            token_expires_at=self._entry.data.get(CONF_TOKEN_EXPIRES_AT),
        )
        session = async_get_clientsession(self.hass)

        discovered: list[str] = []
        try:
            payload = await async_fetch_devices_inventory(session, site_id, tokens)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to fetch device inventory during IQ EVSE serial discovery "
                "for site %s: %s",
                redact_site_id(site_id),
                redact_text(err, site_ids=(site_id,)),
            )
        else:
            discovered = self._normalize_serials(
                active_type_serials_from_inventory(payload, type_key="iqevse")
            )
        if discovered:
            return discovered

        chargers = await async_fetch_chargers(session, site_id, tokens)
        for charger in chargers:
            if charger.serial:
                serial = str(charger.serial).strip()
                if serial and serial not in discovered:
                    discovered.append(serial)
        return discovered

    def _grid_profile_runtime(self) -> GridProfileRuntime | None:
        runtime_data = getattr(self._entry, "runtime_data", None)
        coordinator = getattr(runtime_data, "coordinator", None)
        runtime = getattr(coordinator, "grid_profile_runtime", None)
        return runtime if isinstance(runtime, GridProfileRuntime) else None

    def _grid_mode_coordinator(self) -> Any | None:
        runtime_data = getattr(self._entry, "runtime_data", None)
        return getattr(runtime_data, "coordinator", None)

    async def _async_prime_grid_mode_labels(self) -> None:
        language = getattr(self.hass.config, "language", "en")
        await async_get_translations(self.hass, language, "selector", [DOMAIN])

    def _grid_mode_label(self, mode: object) -> str:
        key = str(mode or "unknown").strip().lower() or "unknown"
        language = getattr(self.hass.config, "language", "en")
        path = f"{_GRID_MODE_LABEL_PREFIX}{key}"
        for candidate_language in (language, "en"):
            translated = async_get_cached_translations(
                self.hass,
                candidate_language,
                "selector",
                DOMAIN,
            ).get(path)
            if isinstance(translated, str) and translated.strip():
                return translated
        return key.replace("_", " ").title()

    def _grid_control_block_reason_label(self, reason: object) -> str:
        key = str(reason or "unknown").strip().lower() or "unknown"
        language = getattr(self.hass.config, "language", "en")
        for reason_key in (key, "unknown"):
            path = f"{_GRID_CONTROL_BLOCK_REASON_LABEL_PREFIX}{reason_key}"
            for candidate_language in (language, "en"):
                translated = async_get_cached_translations(
                    self.hass,
                    candidate_language,
                    "selector",
                    DOMAIN,
                ).get(path)
                if isinstance(translated, str) and translated.strip():
                    return translated
        return "Unknown blocking condition"

    def _grid_mode_placeholders(self) -> dict[str, str]:
        coordinator = self._grid_mode_coordinator()
        current_mode = getattr(coordinator, "grid_mode", None)
        return {
            "current_mode": self._grid_mode_label(current_mode),
            "target_mode": self._grid_mode_label(self._grid_mode_target),
        }

    @staticmethod
    def _grid_mode_error(err: ServiceValidationError) -> str:
        return str(getattr(err, "translation_key", None) or "grid_control_unavailable")

    def _grid_profile_options_available(self) -> bool:
        runtime = self._grid_profile_runtime()
        return bool(
            runtime is not None
            and runtime.installer_access_confirmed
            and runtime.regions
        )

    @staticmethod
    def _grid_profile_unavailable_reason(runtime: GridProfileRuntime) -> str:
        return (
            "grid_profile_installer_required"
            if runtime.support_state == SUPPORT_DENIED
            else "grid_profile_unavailable"
        )

    async def _async_grid_profile_runtime_for_options(
        self,
    ) -> GridProfileRuntime | None:
        runtime = self._grid_profile_runtime()
        if runtime is None:
            return None
        if runtime.installer_access_confirmed and runtime.regions:
            return runtime
        await runtime.async_refresh(force=False, load_profiles=False)
        return runtime

    def _grid_profile_region_options(
        self, runtime: GridProfileRuntime
    ) -> list[dict[str, str]]:
        return [
            {"value": region.region_code, "label": region.label}
            for region in runtime.regions
        ]

    def _grid_profile_options(
        self, profiles: list[GridProfile]
    ) -> list[dict[str, str]]:
        return [
            {"value": profile.profile_id, "label": profile.option_label}
            for profile in profiles
        ]

    @staticmethod
    def _grid_profile_commonly_used_from_input(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return bool(value != ALL_PROFILES_OPTION)

    def _grid_profile_filter_schema(self, runtime: GridProfileRuntime) -> vol.Schema:
        selected_region = runtime.staged_region_code
        if selected_region is None and runtime.regions:
            selected_region = runtime.regions[0].region_code
        return vol.Schema(
            {
                vol.Required(
                    CONF_GRID_PROFILE_REGION,
                    default=selected_region,
                ): selector(
                    {
                        "select": {
                            "options": self._grid_profile_region_options(runtime),
                            "mode": "dropdown",
                        }
                    }
                ),
                vol.Optional(
                    CONF_GRID_PROFILE_COMMONLY_USED,
                    default=runtime.list_mode_option,
                ): selector(
                    {
                        "select": {
                            "options": [COMMONLY_USED_OPTION, ALL_PROFILES_OPTION],
                            "mode": "dropdown",
                            "translation_key": "grid_profile_list_mode",
                        }
                    }
                ),
            }
        )

    def _grid_profile_select_schema(self, profiles: list[GridProfile]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_GRID_PROFILE_ID): selector(
                    {
                        "select": {
                            "options": self._grid_profile_options(profiles),
                            "mode": "dropdown",
                        }
                    }
                )
            }
        )

    def _grid_profile_confirm_schema(self, runtime: GridProfileRuntime) -> vol.Schema:
        if not runtime.apply_available:
            return vol.Schema({})
        return vol.Schema(
            {vol.Required(CONF_GRID_PROFILE_CONFIRM_APPLY, default=False): bool}
        )

    async def _async_prime_grid_profile_labels(self) -> None:
        language = getattr(self.hass.config, "language", "en")
        await async_get_translations(
            self.hass,
            language,
            "selector",
            [DOMAIN],
        )

    def _grid_profile_status_label(self, key: str) -> str:
        language = getattr(self.hass.config, "language", "en")
        path = f"{_GRID_PROFILE_LABEL_PREFIX}{key}"
        for candidate_language in (language, "en"):
            translated = async_get_cached_translations(
                self.hass,
                candidate_language,
                "selector",
                DOMAIN,
            ).get(path)
            if isinstance(translated, str) and translated.strip():
                return translated
        return key.replace("_", " ").capitalize()

    def _grid_profile_flag_label(self, value: bool | None) -> str:
        if value is True:
            return self._grid_profile_status_label("yes")
        if value is False:
            return self._grid_profile_status_label("no")
        return self._grid_profile_status_label("unknown")

    def _grid_profile_confirm_placeholders(
        self, runtime: GridProfileRuntime
    ) -> dict[str, str]:
        profile = runtime.profile_for_id_in_region(
            runtime.staged_profile_id,
            runtime.staged_region_code,
        )
        unknown = self._grid_profile_status_label("unknown")
        return {
            "country": runtime.country_code or unknown,
            "region": runtime.staged_region_label
            or runtime.staged_region_code
            or unknown,
            "current_profile": runtime.current_profile_display() or unknown,
            "selected_profile": profile.option_label if profile else unknown,
            "selected_profile_id": profile.profile_id if profile else unknown,
            "selected_profile_pel": self._grid_profile_flag_label(
                profile.pel_enabled if profile else None
            ),
            "selected_profile_277v": self._grid_profile_flag_label(
                profile.is_277v_compatible if profile else None
            ),
            "apply_status": self._grid_profile_status_label(
                "available" if runtime.apply_available else "unavailable"
            ),
        }

    def _grid_profile_applied_placeholders(
        self, runtime: GridProfileRuntime | None
    ) -> dict[str, str]:
        placeholders: dict[str, str] = {}
        if runtime is not None:
            placeholders.update(self._grid_profile_confirm_placeholders(runtime))

        requested_profile_id = placeholders.get(
            "selected_profile_id", self._grid_profile_status_label("unknown")
        )
        result = self._grid_profile_apply_result
        cloud_apply_status = self._grid_profile_status_label("accepted")
        if isinstance(result, dict):
            requested = result.get("requested_profile_id")
            if requested:
                requested_profile_id = str(requested)
            status = result.get("cloud_apply_status")
            if status:
                cloud_apply_status = self._grid_profile_status_label(str(status))

        placeholders["requested_profile_id"] = requested_profile_id
        placeholders["cloud_apply_status"] = cloud_apply_status
        return placeholders

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            if "reauth" in user_input or "forget_password" in user_input:
                return await self.async_step_authentication_settings(user_input)
            return await self.async_step_settings(user_input)
        menu_options = [
            "settings",
            "devices",
            "repair_notifications",
            "authentication_settings",
            "advanced",
            "migrate_envoy",
        ]
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        del user_input
        menu_options = ["grid_toggle"]
        if self._grid_profile_options_available():
            menu_options.append("grid_profile")
        return self.async_show_menu(
            step_id="advanced",
            menu_options=menu_options,
        )

    async def async_step_grid_toggle(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        coordinator = self._grid_mode_coordinator()
        if coordinator is None:
            return self.async_abort(reason="grid_mode_unavailable")

        errors: dict[str, str] = {}
        await self._async_prime_grid_mode_labels()
        if user_input is None:
            try:
                refreshed = (
                    await coordinator.battery_runtime.async_refresh_grid_control_check(
                        force=True
                    )
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed refreshing Grid Mode eligibility for site %s: %s",
                    redact_site_id(getattr(coordinator, "site_id", "")),
                    redact_text(
                        err,
                        site_ids=(str(getattr(coordinator, "site_id", "")),),
                    ),
                )
                return self.async_abort(reason="grid_mode_unavailable")

            if refreshed is not True:
                return self.async_abort(reason="grid_mode_unavailable")
            if getattr(coordinator, "grid_control_supported", None) is not True:
                return self.async_abort(reason="grid_mode_unavailable")
            if getattr(coordinator, "grid_toggle_allowed", None) is not True:
                reasons = getattr(coordinator, "grid_toggle_blocked_reasons", [])
                return self.async_abort(
                    reason="grid_mode_blocked",
                    description_placeholders={
                        "reasons": ", ".join(
                            self._grid_control_block_reason_label(reason)
                            for reason in (reasons or ["pending"])
                        )
                    },
                )
        else:
            target = str(user_input.get(CONF_GRID_MODE, "")).strip().lower()
            current = str(getattr(coordinator, "grid_mode", "") or "").lower()
            if target not in {"on_grid", "off_grid"}:
                errors[CONF_GRID_MODE] = "grid_mode_invalid"
            elif target == current:
                errors[CONF_GRID_MODE] = "grid_mode_already_active"
            else:
                self._grid_mode_target = target
                try:
                    await coordinator.async_request_grid_toggle_otp()
                except ServiceValidationError as err:
                    errors["base"] = self._grid_mode_error(err)
                else:
                    return await self.async_step_grid_toggle_otp()

        return self.async_show_form(
            step_id="grid_toggle",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GRID_MODE): selector(
                        {
                            "select": {
                                "options": ["on_grid", "off_grid"],
                                "mode": "dropdown",
                                "translation_key": "grid_mode",
                            }
                        }
                    )
                }
            ),
            description_placeholders=self._grid_mode_placeholders(),
            errors=errors,
        )

    async def async_step_grid_toggle_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        coordinator = self._grid_mode_coordinator()
        if coordinator is None or self._grid_mode_target not in {
            "on_grid",
            "off_grid",
        }:
            return self.async_abort(reason="grid_mode_unavailable")

        errors: dict[str, str] = {}
        await self._async_prime_grid_mode_labels()
        if user_input is not None:
            if not user_input.get(CONF_GRID_MODE_CONFIRM):
                errors["base"] = "grid_mode_confirm_required"
            else:
                try:
                    await coordinator.async_set_grid_mode(
                        self._grid_mode_target,
                        user_input.get(CONF_OTP, ""),
                    )
                except ServiceValidationError as err:
                    errors["base"] = self._grid_mode_error(err)
                else:
                    return await self.async_step_grid_toggle_applied()

        return self.async_show_form(
            step_id="grid_toggle_otp",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_OTP): selector({"text": {"type": "password"}}),
                    vol.Required(CONF_GRID_MODE_CONFIRM, default=False): bool,
                }
            ),
            description_placeholders=self._grid_mode_placeholders(),
            errors=errors,
        )

    async def async_step_grid_toggle_applied(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        await self._async_prime_grid_mode_labels()
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(self._entry.options))
        return self.async_show_form(
            step_id="grid_toggle_applied",
            data_schema=vol.Schema({}),
            description_placeholders=self._grid_mode_placeholders(),
        )

    async def async_step_grid_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        runtime = await self._async_grid_profile_runtime_for_options()
        if runtime is None:
            return self.async_abort(reason="grid_profile_unavailable")
        if not runtime.installer_access_confirmed:
            return self.async_abort(
                reason=self._grid_profile_unavailable_reason(runtime)
            )
        if not runtime.regions:
            return self.async_abort(reason="grid_profile_no_regions")

        errors: dict[str, str] = {}
        if user_input is not None:
            region_code = str(user_input.get(CONF_GRID_PROFILE_REGION, "")).strip()
            commonly_used = self._grid_profile_commonly_used_from_input(
                user_input.get(CONF_GRID_PROFILE_COMMONLY_USED, COMMONLY_USED_OPTION)
            )
            if runtime.region_for_code(region_code) is None:
                errors[CONF_GRID_PROFILE_REGION] = "grid_profile_region_invalid"
            else:
                runtime.set_region(region_code)
                runtime.set_list_mode(
                    COMMONLY_USED_OPTION if commonly_used else ALL_PROFILES_OPTION
                )
                runtime.set_search_query(None)
                await runtime.async_load_profiles(
                    region_code=region_code,
                    commonly_used=commonly_used,
                    force=True,
                )
                if not runtime.installer_access_confirmed:
                    return self.async_abort(
                        reason=self._grid_profile_unavailable_reason(runtime)
                    )
                if not runtime.filtered_profiles():
                    errors["base"] = "grid_profile_no_profiles"
                else:
                    return await self.async_step_grid_profile_select()

        return self.async_show_form(
            step_id="grid_profile",
            data_schema=self._grid_profile_filter_schema(runtime),
            errors=errors,
        )

    async def async_step_grid_profile_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        runtime = self._grid_profile_runtime()
        if runtime is None:
            return self.async_abort(reason="grid_profile_unavailable")
        if not runtime.installer_access_confirmed:
            return self.async_abort(
                reason=self._grid_profile_unavailable_reason(runtime)
            )

        profiles = runtime.filtered_profiles()
        if not profiles:
            return await self.async_step_grid_profile()

        errors: dict[str, str] = {}
        if user_input is not None:
            profile_id = str(user_input.get(CONF_GRID_PROFILE_ID, "")).strip()
            if (
                runtime.profile_for_id_in_region(
                    profile_id,
                    runtime.staged_region_code,
                )
                is None
            ):
                errors[CONF_GRID_PROFILE_ID] = "grid_profile_profile_invalid"
            else:
                runtime.set_staged_profile(profile_id)
                return await self.async_step_grid_profile_confirm()

        return self.async_show_form(
            step_id="grid_profile_select",
            data_schema=self._grid_profile_select_schema(profiles),
            errors=errors,
        )

    async def async_step_grid_profile_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        runtime = self._grid_profile_runtime()
        if runtime is None:
            return self.async_abort(reason="grid_profile_unavailable")
        if not runtime.installer_access_confirmed:
            return self.async_abort(
                reason=self._grid_profile_unavailable_reason(runtime)
            )
        if (
            runtime.profile_for_id_in_region(
                runtime.staged_profile_id,
                runtime.staged_region_code,
            )
            is None
        ):
            return await self.async_step_grid_profile_select()

        errors: dict[str, str] = {}
        await self._async_prime_grid_profile_labels()
        if user_input is not None:
            if not runtime.apply_available:
                errors["base"] = "grid_profile_gateway_required"
            elif not user_input.get(CONF_GRID_PROFILE_CONFIRM_APPLY):
                errors["base"] = "confirm_required"
            else:
                try:
                    self._grid_profile_apply_result = await runtime.async_apply_staged()
                except ServiceValidationError as err:
                    errors["base"] = getattr(
                        err, "translation_key", "grid_profile_apply_failed"
                    )
                else:
                    return await self.async_step_grid_profile_applied()

        return self.async_show_form(
            step_id="grid_profile_confirm",
            data_schema=self._grid_profile_confirm_schema(runtime),
            description_placeholders=self._grid_profile_confirm_placeholders(runtime),
            errors=errors,
        )

    async def async_step_grid_profile_applied(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        runtime = self._grid_profile_runtime()
        await self._async_prime_grid_profile_labels()
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data=dict(self._entry.options),
            )
        placeholders: dict[str, str] = {}
        if runtime is not None:
            placeholders = self._grid_profile_applied_placeholders(runtime)
        return self.async_show_form(
            step_id="grid_profile_applied",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        schema = self._build_settings_schema()
        if user_input is not None:
            option_data = dict(user_input)
            option_data.pop("forget_password", None)
            option_data.pop("reauth", None)
            errors: dict[str, str] = {}
            if OPT_API_TIMEOUT in option_data:
                api_timeout = _bounded_int(
                    option_data[OPT_API_TIMEOUT],
                    minimum=MIN_API_TIMEOUT,
                    maximum=MAX_API_TIMEOUT,
                )
                if api_timeout is None:
                    errors[OPT_API_TIMEOUT] = "unknown"
                else:
                    option_data[OPT_API_TIMEOUT] = api_timeout
            if OPT_SESSION_HISTORY_INTERVAL in option_data:
                session_history_interval = _bounded_int(
                    option_data[OPT_SESSION_HISTORY_INTERVAL],
                    minimum=MIN_SESSION_HISTORY_INTERVAL_MIN,
                    maximum=MAX_SESSION_HISTORY_INTERVAL_MIN,
                )
                if session_history_interval is None:
                    errors[OPT_SESSION_HISTORY_INTERVAL] = "unknown"
                else:
                    option_data[OPT_SESSION_HISTORY_INTERVAL] = session_history_interval
            if errors:
                return self.async_show_form(
                    step_id="settings",
                    data_schema=self.add_suggested_values_to_schema(schema, user_input),
                    errors=errors,
                )
            fast_poll, slow_poll = normalize_poll_intervals(
                option_data.get(OPT_FAST_POLL_INTERVAL, DEFAULT_FAST_POLL_INTERVAL),
                option_data.get(OPT_SLOW_POLL_INTERVAL, DEFAULT_SLOW_POLL_INTERVAL),
            )
            option_data[OPT_FAST_POLL_INTERVAL] = fast_poll
            option_data[OPT_SLOW_POLL_INTERVAL] = slow_poll
            for field_key in _TYPE_FIELD_BY_KEY.values():
                option_data.pop(field_key, None)
            option_data.pop(OPT_SCHEDULE_SYNC_ENABLED, None)
            option_data.pop(OPT_BATTERY_SCHEDULES_ENABLED, None)
            option_data.pop(OPT_DEGRADED_SERVICE_REPAIR_ISSUES, None)
            option_data.pop(OPT_SYSTEM_EVENT_REPAIR_ISSUES, None)
            option_data.pop(OPT_PRICING_EDITS_ENABLED, None)
            option_data.pop(OPT_WEATHER_ENABLED, None)
            option_data.pop(OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED, None)
            option_data.pop(OPT_MICROINVERTER_POWER_ENABLED, None)
            option_data.pop(OPT_NOMINAL_VOLTAGE, None)
            option_data.pop(CONF_SCAN_INTERVAL, None)
            option_data.pop(CONF_SITE_ONLY, None)

            options = dict(self._entry.options)
            options.update(option_data)
            return self.async_create_entry(data=options)

        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        visible_type_keys = await self._settings_type_keys()
        schema = self._build_devices_schema(visible_type_keys)
        if user_input is None:
            return self.async_show_form(step_id="devices", data_schema=schema)

        submitted_data = dict(user_input)
        category_data = submitted_data.pop(CONF_DEVICE_CATEGORIES_SECTION, None)
        feature_data = submitted_data.pop(CONF_DEVICE_FEATURES_SECTION, None)
        device_data = dict(category_data) if isinstance(category_data, dict) else {}
        if isinstance(feature_data, dict):
            device_data.update(feature_data)
        device_data.update(submitted_data)
        selected_type_keys: list[str] = []
        default_selected_type_keys = self._default_selected_type_keys()
        for type_key in visible_type_keys:
            field_key = _TYPE_FIELD_BY_KEY.get(type_key)
            if field_key is None:
                continue
            if bool(device_data.pop(field_key, type_key in default_selected_type_keys)):
                selected_type_keys.append(type_key)

        for type_key in self._stored_selected_type_keys():
            if (
                type_key not in ONBOARDING_SUPPORTED_TYPE_KEYS
                and type_key not in selected_type_keys
            ):
                selected_type_keys.append(type_key)

        serials = self._normalize_serials(self._entry.data.get(CONF_SERIALS, []))
        site_only = "iqevse" not in selected_type_keys
        if site_only:
            serials = []
        elif not serials:
            serials = await self._discover_iqevse_serials()
            if not serials:
                return self.async_show_form(
                    step_id="devices",
                    data_schema=self.add_suggested_values_to_schema(schema, user_input),
                    errors={"base": "serials_required"},
                )

        new_data = dict(self._entry.data)
        new_data[CONF_SELECTED_TYPE_KEYS] = selected_type_keys
        new_data[CONF_SITE_ONLY] = site_only
        new_data[CONF_INCLUDE_INVERTERS] = "microinverter" in selected_type_keys
        new_data[CONF_SERIALS] = serials
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)

        options = dict(self._entry.options)
        options[OPT_SCHEDULE_SYNC_ENABLED] = bool(
            device_data.get(
                OPT_SCHEDULE_SYNC_ENABLED,
                options.get(
                    OPT_SCHEDULE_SYNC_ENABLED,
                    DEFAULT_SCHEDULE_SYNC_ENABLED,
                ),
            )
        )
        options[OPT_BATTERY_SCHEDULES_ENABLED] = bool(
            device_data.get(
                OPT_BATTERY_SCHEDULES_ENABLED,
                options.get(
                    OPT_BATTERY_SCHEDULES_ENABLED,
                    DEFAULT_BATTERY_SCHEDULES_ENABLED,
                ),
            )
        )
        options[OPT_PRICING_EDITS_ENABLED] = bool(
            device_data.get(
                OPT_PRICING_EDITS_ENABLED,
                options.get(
                    OPT_PRICING_EDITS_ENABLED,
                    DEFAULT_PRICING_EDITS_ENABLED,
                ),
            )
        )
        options[OPT_WEATHER_ENABLED] = bool(
            device_data.get(
                OPT_WEATHER_ENABLED,
                options.get(
                    OPT_WEATHER_ENABLED,
                    DEFAULT_WEATHER_ENABLED,
                ),
            )
        )
        options[OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED] = bool(
            device_data.get(
                OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                options.get(
                    OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                    DEFAULT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                ),
            )
        )
        options[OPT_MICROINVERTER_POWER_ENABLED] = bool(
            device_data.get(
                OPT_MICROINVERTER_POWER_ENABLED,
                options.get(
                    OPT_MICROINVERTER_POWER_ENABLED,
                    DEFAULT_MICROINVERTER_POWER_ENABLED,
                ),
            )
        )
        options[OPT_NOMINAL_VOLTAGE] = (
            coerce_nominal_voltage(
                device_data.get(
                    OPT_NOMINAL_VOLTAGE,
                    options.get(OPT_NOMINAL_VOLTAGE, self._default_nominal_voltage()),
                )
            )
            or self._default_nominal_voltage()
        )
        return self.async_create_entry(title="", data=options)

    async def async_step_repair_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        schema = self._build_repair_notifications_schema()
        if user_input is None:
            return self.async_show_form(
                step_id="repair_notifications",
                data_schema=schema,
            )

        options = dict(self._entry.options)
        options[OPT_DEGRADED_SERVICE_REPAIR_ISSUES] = bool(
            user_input.get(
                OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
                options.get(
                    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
                    DEFAULT_DEGRADED_SERVICE_REPAIR_ISSUES,
                ),
            )
        )
        options[OPT_SYSTEM_EVENT_REPAIR_ISSUES] = bool(
            user_input.get(
                OPT_SYSTEM_EVENT_REPAIR_ISSUES,
                options.get(
                    OPT_SYSTEM_EVENT_REPAIR_ISSUES,
                    DEFAULT_SYSTEM_EVENT_REPAIR_ISSUES,
                ),
            )
        )
        return self.async_create_entry(title="", data=options)

    async def async_step_authentication_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="authentication_settings",
                data_schema=self._build_authentication_schema(),
            )

        new_data = dict(self._entry.data)
        if bool(user_input.get("forget_password", False)):
            new_data.pop(CONF_PASSWORD, None)
            new_data[CONF_REMEMBER_PASSWORD] = False
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        if bool(user_input.get("reauth", False)):
            self._entry.async_start_reauth(self.hass, data=new_data)
        return self.async_create_entry(title="", data=dict(self._entry.options))

    async def async_step_migrate_envoy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        del user_input
        sources = await self._load_migration_sources()
        targets = self._load_migration_targets()
        if not sources:
            return self.async_abort(reason="migration_no_envoy_sources")
        if not targets:
            return self.async_abort(reason="migration_no_targets")
        if len(sources) == 1:
            self._selected_migration_source_id = sources[0].entry_id
            return await self.async_step_migrate_envoy_intro()
        return await self.async_step_migrate_envoy_source()

    async def async_step_migrate_envoy_source(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        sources = await self._load_migration_sources()
        if user_input is not None:
            self._selected_migration_source_id = user_input.get(
                CONF_MIGRATION_SOURCE_ENTRY
            )
            self._migration_selection = {}
            return await self.async_step_migrate_envoy_intro()

        return self.async_show_form(
            step_id="migrate_envoy_source",
            data_schema=self._build_migration_source_schema(sources),
        )

    async def async_step_migrate_envoy_intro(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        source = await self._selected_migration_source()
        if source is None:
            return await self.async_step_migrate_envoy()
        errors: dict[str, str] = {}
        if user_input is not None:
            if bool(user_input.get(CONF_MIGRATION_BACKUP_CONFIRMED)):
                return await self.async_step_migrate_envoy_mapping()
            errors["base"] = "backup_required"

        return self.async_show_form(
            step_id="migrate_envoy_intro",
            data_schema=self._build_migration_intro_schema(),
            errors=errors,
            description_placeholders={
                "source_title": source.title,
            },
        )

    async def async_step_migrate_envoy_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        source = await self._selected_migration_source()
        if source is None:
            return await self.async_step_migrate_envoy()
        extra_candidates = await self._load_migration_extra_candidates()

        defaults = dict(self._migration_selection)
        if not defaults:
            defaults.update(
                suggest_mappings(
                    source,
                    self._load_migration_targets(),
                    extra_candidates,
                )
            )

        errors: dict[str, str] = {}
        if user_input is not None:
            self._migration_selection = selected_mappings(user_input)
            defaults = {
                flow_key: str(user_input.get(flow_key, skip_option_value()))
                for flow_key in self._migration_flow_keys()
            }
            validation = validate_selected_mappings(
                self.hass,
                self._entry,
                source,
                self._load_migration_targets(),
                self._migration_selection,
                extra_candidates,
                require_source_unloaded=False,
            )
            if validation.error is None:
                return await self.async_step_migrate_envoy_confirm()
            errors["base"] = validation.error

        return self.async_show_form(
            step_id="migrate_envoy_mapping",
            data_schema=self._build_migration_mapping_schema(
                source,
                extra_candidates,
                defaults,
            ),
            errors=errors,
        )

    async def async_step_migrate_envoy_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        source = await self._selected_migration_source()
        if source is None:
            return await self.async_step_migrate_envoy()
        extra_candidates = await self._load_migration_extra_candidates()

        validation = validate_selected_mappings(
            self.hass,
            self._entry,
            source,
            self._load_migration_targets(),
            self._migration_selection,
            extra_candidates,
            require_source_unloaded=False,
        )
        if not self._migration_selection:
            return await self.async_step_migrate_envoy_mapping(
                {**self._migration_selection}
            )
        disable_archived_default = selection_uses_source(
            source,
            self._migration_selection,
            extra_candidates,
        )

        errors: dict[str, str] = {}
        if user_input is not None:
            if not bool(user_input.get(CONF_MIGRATION_CONFIRM_REASSIGN)):
                errors["base"] = "confirm_required"
            else:
                source_entry = self.hass.config_entries.async_get_entry(source.entry_id)
                disable_archived_entities = bool(
                    user_input.get(
                        CONF_MIGRATION_DISABLE_ARCHIVED, disable_archived_default
                    )
                )
                source_selected = selection_uses_source(
                    source,
                    self._migration_selection,
                    extra_candidates,
                )
                source_was_loaded = (
                    source_selected
                    and source_entry is not None
                    and source_entry.state is config_entries.ConfigEntryState.LOADED
                )
                if source_was_loaded:
                    unloaded = await self.hass.config_entries.async_unload(
                        source.entry_id
                    )
                    if not unloaded:
                        errors["base"] = "envoy_entry_loaded"
                    elif source_entry is not None:
                        object.__setattr__(
                            source_entry,
                            "state",
                            config_entries.ConfigEntryState.NOT_LOADED,
                        )

                validation = validate_selected_mappings(
                    self.hass,
                    self._entry,
                    source,
                    self._load_migration_targets(),
                    self._migration_selection,
                    extra_candidates,
                    require_source_unloaded=source_was_loaded,
                )
                if errors:
                    pass
                elif validation.error is not None:
                    if source_was_loaded:
                        await self._async_reload_migration_source_entry(
                            source, source_entry
                        )
                    errors["base"] = validation.error
                else:
                    execution_error = execute_takeover(
                        self.hass,
                        validation.mappings,
                        disable_archived_entities=disable_archived_entities,
                    )
                    if execution_error is not None:
                        if source_was_loaded:
                            await self._async_reload_migration_source_entry(
                                source, source_entry
                            )
                        return self.async_abort(
                            reason="migration_partial_failure",
                            description_placeholders={
                                "completed_mappings": format_completed_preview(
                                    execution_error.completed
                                ),
                                "failed_entity_id": (
                                    execution_error.failed.old_entity_id
                                    if execution_error.failed is not None
                                    else "unknown"
                                ),
                                "failure_reason": execution_error.reason,
                            },
                        )
                    reload_description = "migration_success"
                    try:
                        current_reloaded = await self.hass.config_entries.async_reload(
                            self._entry.entry_id
                        )
                        if not current_reloaded:
                            reload_description = "migration_success_reload_needed"
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug(
                            "Failed reloading config entry after Envoy migration: %s",
                            redact_text(err),
                        )
                        reload_description = "migration_success_reload_needed"
                    if source_was_loaded:
                        if not await self._async_reload_migration_source_entry(
                            source, source_entry
                        ):
                            reload_description = "migration_success_reload_needed"
                    return self.async_create_entry(
                        title="",
                        data=dict(self._entry.options),
                        description=reload_description,
                    )

        return self.async_show_form(
            step_id="migrate_envoy_confirm",
            data_schema=self._build_migration_confirm_schema(
                disable_archived_default=disable_archived_default
            ),
            errors=errors,
            description_placeholders={
                "mapping_preview": (
                    format_mapping_preview(validation.mappings)
                    if validation.error is None
                    else format_selection_preview(
                        self._migration_selection,
                        self._load_migration_targets(),
                    )
                ),
                "warning_preview": format_warning_preview(validation.warnings),
            },
        )
