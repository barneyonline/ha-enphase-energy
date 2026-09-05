"""Handle Enphase Enlighten authentication, discovery, and options flows."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import selector

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
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_SLOW_POLL_INTERVAL,
)
from .device_types import (
    ONBOARDING_SUPPORTED_TYPE_KEYS,
    active_type_serials_from_inventory,
    active_type_keys_from_inventory,
)
from .config_selection import normalize_serials, normalize_selected_type_keys
from .log_redaction import redact_site_id, redact_text
from .runtime_data import EnphaseConfigEntry

from .config_flow_support import (
    CONFIG_ENTRY_MINOR_VERSION,
    _LOGGER as _LOGGER,
    MFA_RESEND_DELAY_SECONDS as MFA_RESEND_DELAY_SECONDS,
    CONF_OTP as CONF_OTP,
    CONF_RESEND_CODE as CONF_RESEND_CODE,
    CONF_TYPE_ENVOY as CONF_TYPE_ENVOY,
    CONF_TYPE_ENCHARGE as CONF_TYPE_ENCHARGE,
    CONF_TYPE_AC_BATTERY as CONF_TYPE_AC_BATTERY,
    CONF_TYPE_IQEVSE as CONF_TYPE_IQEVSE,
    CONF_TYPE_HEATPUMP as CONF_TYPE_HEATPUMP,
    CONF_TYPE_MICROINVERTER as CONF_TYPE_MICROINVERTER,
    CONF_DEVICE_CATEGORIES_SECTION as CONF_DEVICE_CATEGORIES_SECTION,
    _load_get_clientsession as _load_get_clientsession,
    async_get_clientsession as async_get_clientsession,
    CONF_DEVICE_FEATURES_SECTION as CONF_DEVICE_FEATURES_SECTION,
    CONF_MIGRATION_SOURCE_ENTRY as CONF_MIGRATION_SOURCE_ENTRY,
    CONF_MIGRATION_BACKUP_CONFIRMED as CONF_MIGRATION_BACKUP_CONFIRMED,
    CONF_MIGRATION_CONFIRM_REASSIGN as CONF_MIGRATION_CONFIRM_REASSIGN,
    CONF_MIGRATION_DISABLE_ARCHIVED as CONF_MIGRATION_DISABLE_ARCHIVED,
    CONF_GRID_PROFILE_REGION as CONF_GRID_PROFILE_REGION,
    CONF_GRID_PROFILE_COMMONLY_USED as CONF_GRID_PROFILE_COMMONLY_USED,
    CONF_GRID_PROFILE_ID as CONF_GRID_PROFILE_ID,
    CONF_GRID_PROFILE_CONFIRM_APPLY as CONF_GRID_PROFILE_CONFIRM_APPLY,
    CONF_GRID_MODE as CONF_GRID_MODE,
    CONF_GRID_MODE_CONFIRM as CONF_GRID_MODE_CONFIRM,
    _GRID_PROFILE_LABEL_PREFIX as _GRID_PROFILE_LABEL_PREFIX,
    _GRID_MODE_LABEL_PREFIX as _GRID_MODE_LABEL_PREFIX,
    _GRID_CONTROL_BLOCK_REASON_LABEL_PREFIX as _GRID_CONTROL_BLOCK_REASON_LABEL_PREFIX,
    _TYPE_FIELD_BY_KEY as _TYPE_FIELD_BY_KEY,
    _battery_site_settings_has_acb as _battery_site_settings_has_acb,
    _site_entry_title as _site_entry_title,
    _coerce_int_value as _coerce_int_value,
    _bounded_int as _bounded_int,
    _clamped_int as _clamped_int,
    _hems_heatpump_available as _hems_heatpump_available,
    _legacy_microinverters_available as _legacy_microinverters_available,
)

from .config_flow_support import _hems_devices_groups as _hems_devices_groups
from .options_flow import OptionsFlowHandler as OptionsFlowHandler

if TYPE_CHECKING:  # pragma: no cover
    pass


class EnphaseEVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg,misc]
    VERSION = 1
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

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

            session = await async_get_clientsession(self.hass)
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

            session = await async_get_clientsession(self.hass)

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
        session = await async_get_clientsession(self.hass)
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
        session = await async_get_clientsession(self.hass)
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
        session = await async_get_clientsession(self.hass)
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
        return normalize_serials(value)

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
        return normalize_selected_type_keys(value, allowed=_TYPE_FIELD_BY_KEY)

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
