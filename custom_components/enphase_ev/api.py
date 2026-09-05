"""Client and authentication helpers for Enphase Enlighten cloud endpoints.

This module intentionally keeps the HTTP boundary in one place. Enphase exposes
several browser-backed services with different header, cookie, token, and XSRF
expectations, so the client normalizes those variants before coordinator and
entity code consume the data.
"""

from __future__ import annotations

import copy
import json
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable, Iterable, cast

import aiohttp
from yarl import URL

from .const import (
    AUTH_APP_SETTING as AUTH_APP_SETTING,
    AUTH_RFID_SETTING as AUTH_RFID_SETTING,
    BASE_URL as BASE_URL,
    DEFAULT_CHARGE_LEVEL_SETTING as DEFAULT_CHARGE_LEVEL_SETTING,
    DEFAULT_AUTH_TIMEOUT as DEFAULT_AUTH_TIMEOUT,
    ENTREZ_URL as ENTREZ_URL,
    GS_BASE_URL as GS_BASE_URL,
    GREEN_BATTERY_SETTING as GREEN_BATTERY_SETTING,
    LOGIN_FORM_URL as LOGIN_FORM_URL,
    LOGIN_URL as LOGIN_URL,
    MFA_RESEND_URL as MFA_RESEND_URL,
    MFA_VALIDATE_URL as MFA_VALIDATE_URL,
    SELF_TOKEN_URL as SELF_TOKEN_URL,
    SITE_SEARCH_URL as SITE_SEARCH_URL,
)
from . import api_parsers
from .inverter_inventory import async_fetch_inverter_pages
from .api_client.mqtt import (
    MqttStreamSurface,
    _LIVE_STATUS_GRID_RELAY_ENUM as _LIVE_STATUS_GRID_RELAY_ENUM,
)
from .api_client.errors import (
    Unauthorized as Unauthorized,
    EnphaseLoginWallUnauthorized as EnphaseLoginWallUnauthorized,
    EnlightenAuthError as EnlightenAuthError,
    EnlightenAuthInvalidCredentials as EnlightenAuthInvalidCredentials,
    EnlightenAuthTooManySessions as EnlightenAuthTooManySessions,
    EnlightenAuthMFARequired as EnlightenAuthMFARequired,
    EnlightenAuthInvalidOTP as EnlightenAuthInvalidOTP,
    EnlightenAuthOTPBlocked as EnlightenAuthOTPBlocked,
    EnlightenAuthUnavailable as EnlightenAuthUnavailable,
    EnlightenTokenUnavailable as EnlightenTokenUnavailable,
    SchedulerUnavailable as SchedulerUnavailable,
    SessionHistoryUnavailable as SessionHistoryUnavailable,
    SiteEnergyUnavailable as SiteEnergyUnavailable,
    EVSETimeseriesUnavailable as EVSETimeseriesUnavailable,
    AuthSettingsUnavailable as AuthSettingsUnavailable,
    ChargerConfigUnavailable as ChargerConfigUnavailable,
    OptionalEndpointUnavailable as OptionalEndpointUnavailable,
    ActivationAccessDenied as ActivationAccessDenied,
    PayloadFailureSignature as PayloadFailureSignature,
    InvalidPayloadError as InvalidPayloadError,
    _is_optional_non_json_payload as _is_optional_non_json_payload,
    _is_optional_html_payload as _is_optional_html_payload,
    _safe_response_error_message as _safe_response_error_message,
    _enphase_error_status_from_text as _enphase_error_status_from_text,
    _scheduler_error_context_from_text as _scheduler_error_context_from_text,
    _scheduler_error_context as _scheduler_error_context,
    _scheduler_error_code as _scheduler_error_code,
    _is_scheduler_charging_mode_endpoint as _is_scheduler_charging_mode_endpoint,
    _is_enphase_login_wall as _is_enphase_login_wall,
    _is_hems_invalid_site_error as _is_hems_invalid_site_error,
)
from .api_client import site_surface as api_site_surface
from .api_client import vpp_surface as api_vpp_surface
from .api_models import (
    AuthTokens as AuthTokens,
    ChargerInfo as ChargerInfo,
    SiteInfo as SiteInfo,
    TextResponse as TextResponse,
)
from .log_redaction import (
    redact_site_id,
    redact_text,
    truncate_identifier,
)

# Enlighten web pages and XHR endpoints share service capacity with the mobile
# app. A module-level limiter keeps parallel refresh helpers from creating a
# burst of browser-like reads during one Home Assistant update cycle.


from .api_client import request_surface as api_request_surface
from .api_client import header_surface as api_header_surface
from .api_client import battery_surface as api_battery_surface
from .api_client import evse_surface as api_evse_surface
from .api_client import dashboard_surface as api_dashboard_surface
from .api_client import activation_surface as api_activation_surface
from .api_client.authentication import (
    _build_tokens_and_sites as _build_tokens_and_sites,
    _extract_login_session_from_cookies as _extract_login_session_from_cookies,
    _login_form_headers as _login_form_headers,
    _login_headers as _login_headers,
    _mfa_headers as _mfa_headers,
    _submit_login_form as _submit_login_form,
    async_authenticate as async_authenticate,
    async_resend_login_otp as async_resend_login_otp,
    async_validate_login_otp as async_validate_login_otp,
)
from .api_client.common import (
    _LOGGER as _LOGGER,
    _EMAIL_RE as _EMAIL_RE,
    _DEBUG_KV_RE as _DEBUG_KV_RE,
    _XSRF_COOKIE_NAMES as _XSRF_COOKIE_NAMES,
    _ENLIGHTEN_BROWSER_USER_AGENT as _ENLIGHTEN_BROWSER_USER_AGENT,
    _BATTERY_CONFIG_BROWSER_USER_AGENT as _BATTERY_CONFIG_BROWSER_USER_AGENT,
    _BATTERY_CONFIG_VARIANT_PRIMARY as _BATTERY_CONFIG_VARIANT_PRIMARY,
    _BATTERY_CONFIG_VARIANT_LEAN as _BATTERY_CONFIG_VARIANT_LEAN,
    _BATTERY_CONFIG_VARIANT_SESSION_COOKIE as _BATTERY_CONFIG_VARIANT_SESSION_COOKIE,
    _BATTERY_CONFIG_VARIANT_COOKIE_EAUTH as _BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
    _BATTERY_CONFIG_VARIANT_MIXED as _BATTERY_CONFIG_VARIANT_MIXED,
    _ACTIVATION_UI_URL_RE as _ACTIVATION_UI_URL_RE,
    _ACTIVATION_UI_TEMPLATE_RE as _ACTIVATION_UI_TEMPLATE_RE,
    _ACTIVATION_TOKEN_ASSIGNMENT_RE as _ACTIVATION_TOKEN_ASSIGNMENT_RE,
    _ACTIVATION_GRID_PROFILE_RE as _ACTIVATION_GRID_PROFILE_RE,
    _HTML_TAG_RE as _HTML_TAG_RE,
    _ENLIGHTEN_READ_CONCURRENCY_LIMIT as _ENLIGHTEN_READ_CONCURRENCY_LIMIT,
    _ENLIGHTEN_OPTIONAL_READ_CONCURRENCY_LIMIT as _ENLIGHTEN_OPTIONAL_READ_CONCURRENCY_LIMIT,
    _SYSTEM_EVENTS_PAGE_SIZE as _SYSTEM_EVENTS_PAGE_SIZE,
    _SYSTEM_EVENTS_MAX_PAGES as _SYSTEM_EVENTS_MAX_PAGES,
    _SYSTEM_ALARMS_PAGE_SIZE as _SYSTEM_ALARMS_PAGE_SIZE,
    _SYSTEM_ALARMS_MAX_PAGES as _SYSTEM_ALARMS_MAX_PAGES,
    OCPP_TRIGGER_MESSAGES as OCPP_TRIGGER_MESSAGES,
    OCPP_TRIGGER_MESSAGES_REQUIRING_CONFIRMATION as OCPP_TRIGGER_MESSAGES_REQUIRING_CONFIRMATION,
    _OCPP_TRIGGER_MESSAGE_MAX_LENGTH as _OCPP_TRIGGER_MESSAGE_MAX_LENGTH,
    _OCPP_TRIGGER_MESSAGE_RE as _OCPP_TRIGGER_MESSAGE_RE,
    _enlighten_read_semaphore as _enlighten_read_semaphore,
    _enlighten_optional_read_semaphore as _enlighten_optional_read_semaphore,
    _enlighten_optional_read as _enlighten_optional_read,
    _enlighten_read_limiter_bypass as _enlighten_read_limiter_bypass,
    JsonDict as JsonDict,
    _BatteryConfigWriteAttempt as _BatteryConfigWriteAttempt,
    _truncate_preview as _truncate_preview,
    validate_ocpp_trigger_message as validate_ocpp_trigger_message,
    _redact_debug_json_body as _redact_debug_json_body,
    _payload_preview_and_hash as _payload_preview_and_hash,
    _SYSTEM_DASHBOARD_DETAIL_QUERY_MAP as _SYSTEM_DASHBOARD_DETAIL_QUERY_MAP,
    _system_dashboard_query_type as _system_dashboard_query_type,
    _request_label as _request_label,
    _serialize_cookie_jar as _serialize_cookie_jar,
    _cookie_header_from_map as _cookie_header_from_map,
    _decode_jwt_exp as _decode_jwt_exp,
    _activation_context_from_settings_html as _activation_context_from_settings_html,
    _activation_grid_profiles_from_settings_html as _activation_grid_profiles_from_settings_html,
    _decode_jwt_payload as _decode_jwt_payload,
    _jwt_user_id as _jwt_user_id,
    _jwt_session_id as _jwt_session_id,
    _extract_xsrf_token as _extract_xsrf_token,
    _coerce_cookie_map as _coerce_cookie_map,
    _cookie_map_from_header as _cookie_map_from_header,
    _cookie_names_from_header as _cookie_names_from_header,
    _authorization_bearer_token as _authorization_bearer_token,
    _request_failure_debug_family as _request_failure_debug_family,
    _should_limit_enlighten_read_request as _should_limit_enlighten_read_request,
    _get_enlighten_read_semaphore as _get_enlighten_read_semaphore,
    _get_enlighten_optional_read_semaphore as _get_enlighten_optional_read_semaphore,
    enlighten_optional_read_scope as enlighten_optional_read_scope,
    _enlighten_reauth_read_scope as _enlighten_reauth_read_scope,
    _enlighten_read_request_guard as _enlighten_read_request_guard,
    _timed_enlighten_read_request_guard as _timed_enlighten_read_request_guard,
    _timed_response_context as _timed_response_context,
    _timed_response_json as _timed_response_json,
    _timed_response_text as _timed_response_text,
    _seed_cookie_jar as _seed_cookie_jar,
    _extract_login_session as _extract_login_session,
    _is_too_many_active_sessions_response as _is_too_many_active_sessions_response,
    is_scheduler_unavailable_error as is_scheduler_unavailable_error,
    is_session_history_unavailable_error as is_session_history_unavailable_error,
    is_site_energy_unavailable_error as is_site_energy_unavailable_error,
    is_evse_timeseries_unavailable_error as is_evse_timeseries_unavailable_error,
    is_auth_settings_unavailable_error as is_auth_settings_unavailable_error,
    _request_json as _request_json,
    _request_mfa_json as _request_mfa_json,
    _normalize_sites as _normalize_sites,
    _normalize_chargers as _normalize_chargers,
)


async def async_fetch_chargers(
    session: aiohttp.ClientSession,
    site_id: str,
    tokens: AuthTokens,
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> list[ChargerInfo]:
    """Fetch chargers for a site using the provided authentication tokens."""

    if not site_id:
        return []

    client = EnphaseEVClient(
        session,
        site_id,
        tokens.access_token,
        tokens.cookie,
        timeout=timeout,
    )
    try:
        payload = await client.summary_v2()
    except Exception as err:  # noqa: BLE001 - propagate as empty list for flow UX
        _LOGGER.debug(
            "Failed to fetch charger summary for site %s: %s",
            redact_site_id(site_id),
            redact_text(err, site_ids=(site_id,)),
        )
        return []
    return _normalize_chargers(payload)


async def async_fetch_devices_inventory(
    session: aiohttp.ClientSession,
    site_id: str,
    tokens: AuthTokens,
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> dict[str, object] | None:
    """Fetch a site devices inventory payload for config-flow category selection."""

    if not site_id:
        return {}

    client = EnphaseEVClient(
        session,
        site_id,
        tokens.access_token,
        tokens.cookie,
        timeout=timeout,
    )
    try:
        payload = await client.devices_inventory()
    except Exception as err:  # noqa: BLE001 - best-effort for flow UX
        _LOGGER.debug(
            "Failed to fetch devices inventory for site %s: %s",
            redact_site_id(site_id),
            redact_text(err, site_ids=(site_id,)),
        )
        return None
    if isinstance(payload, dict):
        return payload
    return None


async def async_fetch_battery_site_settings(
    session: aiohttp.ClientSession,
    site_id: str,
    tokens: AuthTokens,
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> dict[str, object] | None:
    """Fetch BatteryConfig site settings for config-flow category selection."""

    if not site_id:
        return {}

    client = EnphaseEVClient(
        session,
        site_id,
        tokens.access_token,
        tokens.cookie,
        timeout=timeout,
    )
    try:
        payload = await client.battery_site_settings()
    except Exception as err:  # noqa: BLE001 - best-effort for flow UX
        _LOGGER.debug(
            "Failed to fetch battery site settings for site %s: %s",
            redact_site_id(site_id),
            redact_text(err, site_ids=(site_id,)),
        )
        return None
    if isinstance(payload, dict):
        return payload
    return None


async def async_fetch_inverters_inventory(
    session: aiohttp.ClientSession,
    site_id: str,
    tokens: AuthTokens,
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> dict[str, object] | None:
    """Fetch legacy inverter inventory for config-flow microinverter discovery."""

    if not site_id:
        return {}

    client = EnphaseEVClient(
        session,
        site_id,
        tokens.access_token,
        tokens.cookie,
        timeout=timeout,
    )

    async def _fetch_page(offset: int) -> dict[str, object] | None:
        try:
            payload = await client.inverters_inventory(
                limit=1000, offset=offset, search=""
            )
        except TypeError:
            if offset != 0:
                return None
            try:
                payload = await client.inverters_inventory()
            except Exception as err:  # noqa: BLE001 - best-effort for flow UX
                _LOGGER.debug(
                    "Failed to fetch inverter inventory for site %s: %s",
                    redact_site_id(site_id),
                    redact_text(err, site_ids=(site_id,)),
                )
                return None
        except Exception as err:  # noqa: BLE001 - best-effort for flow UX
            _LOGGER.debug(
                "Failed to fetch inverter inventory for site %s: %s",
                redact_site_id(site_id),
                redact_text(err, site_ids=(site_id,)),
            )
            return None
        if isinstance(payload, dict):
            return payload
        return None

    try:
        result = await async_fetch_inverter_pages(_fetch_page)
        return (
            result.payload
            if result.complete or result.payload.get("inverters")
            else None
        )
    except Exception as err:  # noqa: BLE001 - best-effort for flow UX
        _LOGGER.debug(
            "Failed to assemble inverter inventory for site %s: %s",
            redact_site_id(site_id),
            redact_text(err, site_ids=(site_id,)),
        )
        return None


async def async_fetch_hems_devices(
    session: aiohttp.ClientSession,
    site_id: str,
    tokens: AuthTokens,
    *,
    refresh_data: bool = False,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> dict[str, object] | None:
    """Fetch dedicated HEMS device inventory for config-flow discovery."""

    if not site_id:
        return {}

    client = EnphaseEVClient(
        session,
        site_id,
        tokens.access_token,
        tokens.cookie,
        timeout=timeout,
    )
    try:
        payload = await client.hems_devices(refresh_data=refresh_data)
    except Exception as err:  # noqa: BLE001 - best-effort for flow UX
        _LOGGER.debug(
            "Failed to fetch HEMS devices for site %s: %s",
            redact_site_id(site_id),
            redact_text(err, site_ids=(site_id,)),
        )
        return None
    if isinstance(payload, dict):
        return payload
    return None


class EnphaseEVClient(MqttStreamSurface):
    def __init__(
        self,
        session: aiohttp.ClientSession,
        site_id: str,
        eauth: str | None,
        cookie: str | None,
        timeout: int = 15,
        reauth_callback: Callable[[], Awaitable[bool]] | None = None,
        cookie_header_session: aiohttp.ClientSession | None = None,
    ):
        self._timeout = int(timeout)
        self._s = session
        self._cookie_header_session = cookie_header_session
        self._site = site_id
        # Cache working API variant indexes per action to avoid retries once discovered
        self._start_variant_idx: int | None = None
        self._start_variant_idx_with_level: int | None = None
        self._start_variant_idx_no_level: int | None = None
        self._stop_variant_idx: int | None = None
        self._bp_xsrf_token: str | None = None
        self._battery_config_variant_cache: dict[tuple[str, str, str], str] = {}
        self._battery_config_write_attempt_cache: dict[
            tuple[str, str, str, str], str
        ] = {}
        self._battery_config_supports_mqtt: bool | None = None

        self._battery_config_write_bases: dict[str, dict[str, Any]] = {}
        self._cookie = cookie or ""
        self._eauth = eauth or None
        self._activation_token: str | None = None
        self._activation_referer: str | None = None
        self._activation_settings_grid_profiles: list[tuple[str, str]] = []
        self._hems_site_supported: bool | None = None
        self._system_dashboard_summary_payload: dict[str, object] | None = None
        self._reauth_cb: Callable[[], Awaitable[bool]] | None = reauth_callback
        self._last_unauthorized_request: str | None = None
        self._request_count = 0
        self._payload_failure_log_state: dict[str, PayloadFailureSignature] = {}
        self._h = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/pv/systems/{site_id}/summary",
            "User-Agent": _ENLIGHTEN_BROWSER_USER_AGENT,
        }
        self.update_credentials(eauth=eauth, cookie=cookie)

    def set_timeout(self, timeout: int) -> None:
        """Update the request timeout for subsequent API operations."""

        self._timeout = int(timeout)

    def set_reauth_callback(
        self, callback: Callable[[], Awaitable[bool]] | None
    ) -> None:
        """Register coroutine used to refresh credentials on 401."""

        self._reauth_cb = callback

    async def async_close(self) -> None:
        """Detach the private session while preserving Home Assistant's connector."""

        session = self._cookie_header_session
        self._cookie_header_session = None
        if session is not None and not session.closed:
            session.detach()

    @staticmethod
    def _is_hems_api_endpoint(endpoint: str | None) -> bool:
        """Return True for HEMS JSON API endpoints that are optional/read-only."""

        return bool(endpoint and endpoint.startswith("/api/v1/hems/"))

    @property
    def last_unauthorized_request(self) -> str | None:
        """Return the most recent request that received a 401 response."""

        return self._last_unauthorized_request

    def reset_request_count(self) -> None:
        """Reset the lightweight cloud request counter."""

        self._request_count = 0

    @property
    def request_count(self) -> int:
        """Return the number of HTTP attempts since the last counter reset."""

        return int(getattr(self, "_request_count", 0) or 0)

    def update_credentials(
        self,
        *,
        eauth: str | None = None,
        cookie: str | None = None,
    ) -> None:
        "Update headers when auth credentials change."

        return api_header_surface.update_credentials(self, eauth=eauth, cookie=cookie)

    def _bearer(self) -> str | None:
        "Extract Authorization bearer token from cookies if present."

        return api_header_surface._bearer(self)

    def scheduler_bearer(self) -> str | None:
        "Public bearer accessor for scheduler feature checks."

        return api_header_surface.scheduler_bearer(self)

    def has_scheduler_bearer(self) -> bool:
        "Return True when scheduler bearer auth can be derived."

        return api_header_surface.has_scheduler_bearer(self)

    @property
    def hems_site_supported(self) -> bool | None:
        """Return whether HEMS has been positively identified for this site."""

        return self._hems_site_supported

    def base_header_names(self) -> list[str]:
        """Return base header names without exposing values."""

        return sorted(self._h.keys())

    def _mark_payload_healthy(self, endpoint: str | None) -> None:
        "Log endpoint recovery once after a prior invalid payload."

        return api_request_surface._mark_payload_healthy(self, endpoint)

    def _log_invalid_payload(self, err: InvalidPayloadError) -> None:
        "Log invalid payload details once per endpoint failure transition."

        return api_request_surface._log_invalid_payload(self, err)

    def _invalid_payload_error(
        self,
        *,
        endpoint: str | None,
        summary: str | None = None,
        status: int | None = None,
        content_type: str | None = None,
        failure_kind: str,
        decode_error: str | None = None,
        payload: object = None,
        log_warning: bool = True,
    ) -> InvalidPayloadError:
        "Build and log a structured invalid payload error."

        return api_request_surface._invalid_payload_error(
            self,
            endpoint=endpoint,
            summary=summary,
            status=status,
            content_type=content_type,
            failure_kind=failure_kind,
            decode_error=decode_error,
            payload=payload,
            log_warning=log_warning,
        )

    def _login_wall_unauthorized(
        self,
        *,
        endpoint: str | None,
        request_label: str,
        status: int | None,
        content_type: str | None,
        payload: object,
    ) -> EnphaseLoginWallUnauthorized:
        "Build a structured unauthorized error for Enlighten login-wall responses."

        return api_request_surface._login_wall_unauthorized(
            self,
            endpoint=endpoint,
            request_label=request_label,
            status=status,
            content_type=content_type,
            payload=payload,
        )

    def _history_bearer(self) -> str | None:
        "Return the preferred bearer token for session history calls."

        return api_header_surface._history_bearer(self)

    def _session_history_username(self) -> str | None:
        "Return the user id expected by the session history service."

        return api_header_surface._session_history_username(self)

    def _session_history_headers(
        self, request_id: str | None, username: str | None
    ) -> dict[str, str]:
        "Return headers for session history endpoints."

        return api_header_surface._session_history_headers(self, request_id, username)

    def _evse_timeseries_headers(
        self,
        request_id: str | None,
        username: str | None,
    ) -> dict[str, str]:
        "Return headers for EVSE timeseries endpoints."

        return api_header_surface._evse_timeseries_headers(self, request_id, username)

    def _site_web_graph_referer(self, view: str, *, graph_range: str = "years") -> str:
        "Return a web-app graph referer for a site-scoped Enlighten view."

        return api_header_surface._site_web_graph_referer(
            self, view, graph_range=graph_range
        )

    def _site_web_referer(self, view: str) -> str:
        "Return the default years-graph referer for site XHR families."

        return api_header_surface._site_web_referer(self, view)

    def _root_xhr_headers(self) -> dict[str, str]:
        "Return base headers for root-scoped Enlighten XHR requests."

        return api_header_surface._root_xhr_headers(self)

    def _history_headers(self) -> dict[str, str]:
        "Return headers for app-api and pv/settings history-family requests."

        return api_header_surface._history_headers(self)

    def _homeowner_events_headers(self) -> dict[str, str]:
        "Return browser-style headers for the homeowner event-history feed."

        return api_header_surface._homeowner_events_headers(self)

    def _today_headers(self) -> dict[str, str]:
        "Return headers for EV today-page XHR requests."

        return api_header_surface._today_headers(self)

    def _today_json_headers(self) -> dict[str, str]:
        "Return headers for EV today-page JSON/XHR requests."

        return api_header_surface._today_json_headers(self)

    def _history_form_headers(self) -> dict[str, str]:
        "Return headers for history-family form POST requests."

        return api_header_surface._history_form_headers(self)

    def _layout_headers(self) -> dict[str, str]:
        "Return headers for systems/layout-family requests."

        return api_header_surface._layout_headers(self)

    def _systems_html_headers(self, referer: str | None = None) -> dict[str, str]:
        "Return browser-style headers for site-scoped HTML /systems routes."

        return api_header_surface._systems_html_headers(self, referer)

    def _systems_json_headers(self) -> dict[str, str]:
        "Return headers for site-scoped /systems JSON endpoints."

        return api_header_surface._systems_json_headers(self)

    def _control_headers(self) -> dict[str, str]:
        "Return Authorization header overrides for control-plane requests."

        return api_header_surface._control_headers(self)

    def _control_request_headers(
        self, base: Callable[[], dict[str, str]]
    ) -> dict[str, str]:
        "Build a control request using the current credentials."

        return api_header_surface._control_request_headers(self, base)

    def _vpp_headers(self) -> dict[str, str | None]:
        "Return isolated browser headers for the Grid Services host."

        return api_header_surface._vpp_headers(self)

    def control_headers(self) -> dict[str, str]:
        "Public control header helper for read-only diagnostics checks."

        return api_header_surface.control_headers(self)

    def _system_dashboard_headers(self) -> dict[str, str]:
        "Return headers for system dashboard read endpoints."

        return api_header_surface._system_dashboard_headers(self)

    def _hems_auth_context(self) -> tuple[str | None, str | None]:
        "Return the preferred HEMS bearer token and resolved user id."

        return api_header_surface._hems_auth_context(self)

    @staticmethod
    def _system_dashboard_is_optional_error(err: Exception) -> bool:
        """Return True when a dashboard route should fall back or soft-fail."""

        if isinstance(err, EnphaseLoginWallUnauthorized):
            return False
        if isinstance(err, Unauthorized):
            return True
        if isinstance(err, InvalidPayloadError):
            return _is_optional_non_json_payload(err)
        if isinstance(err, aiohttp.ClientResponseError):
            return err.status in (401, 403, 404)
        return False

    async def _system_dashboard_get(
        self,
        modern_url: str,
        legacy_url: str,
    ) -> JsonDict | None:
        "Fetch a system dashboard payload from the modern route with fallback."

        return await api_dashboard_surface._system_dashboard_get(
            self, modern_url, legacy_url
        )

    def _hems_headers(self) -> dict[str, str]:
        "Return headers for HEMS read endpoints."

        return api_header_surface._hems_headers(self)

    def _battery_config_user_id(self) -> str | None:
        "Return the user id for BatteryConfig requests when available."

        return api_battery_surface._battery_config_user_id(self)

    def _battery_config_single_auth_token(self) -> str | None:
        "Return the single-token auth candidate used by external clients."

        return api_battery_surface._battery_config_single_auth_token(self)

    def _battery_config_user_id_for_token(self, token: str | None = None) -> str | None:
        "Return the preferred BatteryConfig user id for a specific token."

        return api_battery_surface._battery_config_user_id_for_token(self, token)

    def _battery_config_auth_source_label(self, token: str | None) -> str:
        "Return a coarse label describing the selected BatteryConfig token source."

        return api_battery_surface._battery_config_auth_source_label(self, token)

    def _battery_config_header_debug_flags(
        self,
        headers: dict[str, str],
        *,
        auth_source_override: str | None = None,
    ) -> dict[str, object]:
        "Return safe debug flags describing BatteryConfig auth-header shape."

        return api_battery_surface._battery_config_header_debug_flags(
            self, headers, auth_source_override=auth_source_override
        )

    @staticmethod
    def _merge_request_headers(
        base_headers: dict[str, str],
        extra_headers: dict[str, str | None] | None,
    ) -> dict[str, str]:
        "Merge request headers, treating ``None`` values as explicit removals."

        return api_header_surface._merge_request_headers(base_headers, extra_headers)

    def _battery_config_auth_context(self) -> tuple[str | None, str | None]:
        "Return preferred BatteryConfig auth token and resolved user id."

        return api_battery_surface._battery_config_auth_context(self)

    def _battery_config_headers(
        self,
        *,
        include_xsrf: bool = False,
        variant: str = _BATTERY_CONFIG_VARIANT_PRIMARY,
    ) -> dict[str, str | None]:
        "Return headers for BatteryConfig read/write calls."

        return api_battery_surface._battery_config_headers(
            self, include_xsrf=include_xsrf, variant=variant
        )

    def _tariff_headers(self, *, write: bool = False) -> dict[str, str | None]:
        "Return headers for tariff microservice calls."

        return api_header_surface._tariff_headers(self, write=write)

    def _activation_reference_headers(self) -> dict[str, str | None]:
        "Return headers for Activation reference-data calls."

        return api_activation_surface._activation_reference_headers(self)

    def _activation_headers(self, *, write: bool = False) -> dict[str, str | None]:
        "Return cloud Activation API headers."

        return api_activation_surface._activation_headers(self, write=write)

    def _activation_auth_token(self) -> str | None:
        "Return the settings-page Activation token, with stored-auth fallback."

        return api_activation_surface._activation_auth_token(self)

    def _clear_activation_auth_context(self) -> None:
        """Discard the in-memory Activation data tied to the current session."""

        self._activation_token = None
        self._activation_referer = None
        self._activation_settings_grid_profiles = []

    def activation_settings_grid_profiles(self) -> list[tuple[str, str]]:
        """Return Grid Profile labels visible on the classic Settings page."""

        return list(self._activation_settings_grid_profiles)

    def _activation_cookie(self, token: str | None) -> str | None:
        "Return session cookies with the Activation Manager token synchronized."

        return api_activation_surface._activation_cookie(self, token)

    async def async_prepare_activation_auth(self, *, force: bool = False) -> bool:
        "Bootstrap the same Activation JWT embedded by the Enlighten settings UI."

        return await api_activation_surface.async_prepare_activation_auth(
            self, force=force
        )

    async def _activation_payload(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str | None] | Callable[[], dict[str, str | None]],
        **kwargs: Any,
    ) -> object:
        "Return Activation JSON, mapping denied access to optional unavailable."

        return await api_activation_surface._activation_payload(
            self, method, url, headers=headers, **kwargs
        )

    async def _activation_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str | None] | Callable[[], dict[str, str | None]],
        **kwargs: Any,
    ) -> JsonDict:
        "Return Activation object JSON."

        return await api_activation_surface._activation_json(
            self, method, url, headers=headers, **kwargs
        )

    def _battery_config_cookie_eauth_headers(
        self,
        *,
        include_xsrf: bool = False,
    ) -> dict[str, str | None]:
        "Return the cookie-backed external-compatible BatteryConfig headers."

        return api_battery_surface._battery_config_cookie_eauth_headers(
            self, include_xsrf=include_xsrf
        )

    def _battery_config_cookie(
        self,
        *,
        include_xsrf: bool = False,
        preserve_existing_xsrf: bool = False,
    ) -> str | None:
        "Return a normalized BatteryConfig cookie header value."

        return api_battery_surface._battery_config_cookie(
            self,
            include_xsrf=include_xsrf,
            preserve_existing_xsrf=preserve_existing_xsrf,
        )

    def _battery_config_cookie_header_xsrf_token(self) -> str | None:
        "Return the BP-XSRF token from the stored cookie header."

        return api_battery_surface._battery_config_cookie_header_xsrf_token(self)

    def _battery_config_mixed_auth_headers(
        self,
        *,
        include_xsrf: bool = False,
    ) -> dict[str, str | None]:
        "Return the mixed-auth compatibility BatteryConfig headers."

        return api_battery_surface._battery_config_mixed_auth_headers(
            self, include_xsrf=include_xsrf
        )

    def _battery_schedule_validation_payload(
        self, schedule_type: str = "cfg"
    ) -> dict[str, object]:
        "Return the XSRF bootstrap / validation payload for a schedule family."

        return api_battery_surface._battery_schedule_validation_payload(
            self, schedule_type
        )

    def _battery_config_params(
        self,
        *,
        include_source: bool | str = False,
        locale: str | None = None,
    ) -> dict[str, str]:
        "Return query parameters for BatteryConfig calls."

        return api_battery_surface._battery_config_params(
            self, include_source=include_source, locale=locale
        )

    def _battery_config_endpoint_family(self, url: str) -> str:
        "Return the cache family for a BatteryConfig endpoint URL."

        return api_battery_surface._battery_config_endpoint_family(self, url)

    def _battery_config_variant_cache_key(
        self, endpoint_family: str
    ) -> tuple[str, str, str]:
        "Return the cache key for BatteryConfig request variants."

        return api_battery_surface._battery_config_variant_cache_key(
            self, endpoint_family
        )

    def _battery_config_cached_variant(self, endpoint_family: str) -> str | None:
        "Return the cached request variant for a BatteryConfig family."

        return api_battery_surface._battery_config_cached_variant(self, endpoint_family)

    def _cache_battery_config_variant(self, endpoint_family: str, variant: str) -> None:
        """Remember the working request variant for a BatteryConfig family."""

        key = self._battery_config_variant_cache_key(endpoint_family)
        self._battery_config_variant_cache[key] = variant

    def _battery_config_variant_order(self, endpoint_family: str) -> list[str]:
        "Return the ordered variants to try for a BatteryConfig family."

        return api_battery_surface._battery_config_variant_order(self, endpoint_family)

    def _battery_config_write_attempt_cache_key(
        self,
        endpoint_family: str,
        *,
        supports_mqtt: bool | None,
    ) -> tuple[str, str, str, str]:
        "Return the cache key for BatteryConfig write attempts."

        return api_battery_surface._battery_config_write_attempt_cache_key(
            self, endpoint_family, supports_mqtt=supports_mqtt
        )

    def _battery_config_cached_write_attempt(
        self,
        endpoint_family: str,
        *,
        supports_mqtt: bool | None,
    ) -> str | None:
        "Return the cached BatteryConfig write attempt id for an endpoint family."

        return api_battery_surface._battery_config_cached_write_attempt(
            self, endpoint_family, supports_mqtt=supports_mqtt
        )

    def _cache_battery_config_write_attempt(
        self,
        endpoint_family: str,
        attempt_id: str,
        *,
        supports_mqtt: bool | None,
    ) -> None:
        """Remember the working BatteryConfig write attempt id for an endpoint family."""

        key = self._battery_config_write_attempt_cache_key(
            endpoint_family,
            supports_mqtt=supports_mqtt,
        )
        self._battery_config_write_attempt_cache[key] = attempt_id

    def _battery_config_write_attempts(
        self,
        endpoint_family: str,
        *,
        write_intent: str,
        supports_mqtt: bool | None,
        params: dict[str, str] | None,
        json_body: dict[str, Any] | list[Any] | None,
    ) -> list[_BatteryConfigWriteAttempt]:
        "Return ordered write attempts for a BatteryConfig endpoint family."

        return api_battery_surface._battery_config_write_attempts(
            self,
            endpoint_family,
            write_intent=write_intent,
            supports_mqtt=supports_mqtt,
            params=params,
            json_body=json_body,
        )

    def _battery_config_prefers_cookie_compat(self) -> bool:
        "Return True when cookie-backed BatteryConfig writes should be preferred."

        return api_battery_surface._battery_config_prefers_cookie_compat(self)

    def _battery_config_attempt_headers(
        self,
        attempt: _BatteryConfigWriteAttempt,
        *,
        include_xsrf: bool,
    ) -> dict[str, str | None]:
        "Return headers for a BatteryConfig write attempt."

        return api_battery_surface._battery_config_attempt_headers(
            self, attempt, include_xsrf=include_xsrf
        )

    def _battery_config_attempt_params(
        self,
        params: dict[str, str] | None,
        attempt: _BatteryConfigWriteAttempt,
    ) -> dict[str, str] | None:
        "Return query params for a BatteryConfig write attempt."

        return api_battery_surface._battery_config_attempt_params(self, params, attempt)

    def _battery_config_attempt_json_body(
        self,
        json_body: dict[str, Any] | list[Any] | None,
        endpoint_family: str,
        attempt: _BatteryConfigWriteAttempt,
    ) -> dict[str, Any] | list[Any] | None:
        "Return the request payload for a BatteryConfig write attempt."

        return api_battery_surface._battery_config_attempt_json_body(
            self, json_body, endpoint_family, attempt
        )

    def _battery_config_attempt_change_summary(
        self,
        attempt: _BatteryConfigWriteAttempt,
        *,
        params: dict[str, str] | None,
        json_body: dict[str, Any] | list[Any] | None,
    ) -> dict[str, object]:
        "Return safe debug details describing how an attempt differs from canonical."

        return api_battery_surface._battery_config_attempt_change_summary(
            self, attempt, params=params, json_body=json_body
        )

    @staticmethod
    def _battery_config_attempt_signature(
        *,
        attempt: _BatteryConfigWriteAttempt,
        params: dict[str, str] | None,
        json_body: dict[str, Any] | list[Any] | None,
    ) -> str:
        "Return a stable signature for deduplicating write attempts."

        return api_battery_surface._battery_config_attempt_signature(
            attempt=attempt, params=params, json_body=json_body
        )

    def _remember_battery_config_capabilities(self, payload: object) -> None:
        """Persist BatteryConfig capability hints discovered from payloads."""

        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        supports_mqtt = data.get("supportsMqtt")
        if isinstance(supports_mqtt, bool):
            self._battery_config_supports_mqtt = supports_mqtt

    @staticmethod
    def _battery_config_payload_data(payload: object) -> dict[str, Any] | None:
        "Return the nested BatteryConfig data payload when available."

        return api_battery_surface._battery_config_payload_data(payload)

    def _remember_battery_config_write_base(
        self, endpoint_family: str, payload: object
    ) -> None:
        """Persist a writable base payload for later state-preserving retries."""

        data = self._battery_config_payload_data(payload)
        if not isinstance(data, dict):
            return

        if endpoint_family == "battery_settings":
            allowed_keys = {
                "profile",
                "operationModeSubType",
                "batteryBackupPercentage",
                "requestedConfig",
                "requestedConfigMqtt",
                "stormGuardState",
                "showStormGuardAlert",
                "acceptedItcDisclaimer",
                "hideChargeFromGrid",
                "envoySupportsVls",
                "chargeBeginTime",
                "chargeEndTime",
                "batteryGridMode",
                "veryLowSoc",
                "chargeFromGrid",
                "chargeFromGridScheduleEnabled",
                "systemTask",
                "dtgControl",
                "cfgControl",
                "rbdControl",
                "powerMatchControl",
                "evseStormEnabled",
                "devices",
            }
        elif endpoint_family == "profile":
            allowed_keys = {
                "profile",
                "operationModeSubType",
                "batteryBackupPercentage",
                "requestedConfig",
                "requestedConfigMqtt",
                "stormGuardState",
                "acceptedStormGuardDisclaimer",
                "showStormGuardAlert",
                "systemTask",
                "veryLowSoc",
                "dtgControl",
                "cfgControl",
                "rbdControl",
                "evseStormEnabled",
                "devices",
            }
        else:
            return

        write_base = {
            key: copy.deepcopy(value)
            for key, value in data.items()
            if key in allowed_keys
        }
        if write_base:
            self._battery_config_write_bases[endpoint_family] = write_base

    def _battery_config_merged_write_payload(
        self,
        endpoint_family: str,
        json_body: dict[str, Any] | list[Any] | None,
    ) -> dict[str, Any] | list[Any] | None:
        "Merge a partial write payload onto the last successful read payload."

        return api_battery_surface._battery_config_merged_write_payload(
            self, endpoint_family, json_body
        )

    def _xsrf_token(self) -> str | None:
        "Return the XSRF token value."

        return api_battery_surface._xsrf_token(self)

    async def _battery_config_request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | list[Any] | None = None,
        params: dict[str, str] | None = None,
        schedule_type: str = "cfg",
        endpoint_family: str | None = None,
        bootstrap_xsrf: bool = False,
        cache_on_success: bool = False,
    ) -> JsonDict:
        "Issue a BatteryConfig request using the observed first-party variants."

        return await api_battery_surface._battery_config_request(
            self,
            method,
            url,
            json_body=json_body,
            params=params,
            schedule_type=schedule_type,
            endpoint_family=endpoint_family,
            bootstrap_xsrf=bootstrap_xsrf,
            cache_on_success=cache_on_success,
        )

    async def _battery_config_write_request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | list[Any] | None = None,
        params: dict[str, str] | None = None,
        schedule_type: str = "cfg",
        endpoint_family: str | None = None,
        write_intent: str = "generic",
        supports_mqtt: bool | None = None,
        strip_devices: bool = False,
        partial_payload_only: bool = False,
    ) -> JsonDict:
        "Issue a BatteryConfig write using endpoint-specific compatibility attempts."

        return await api_battery_surface._battery_config_write_request(
            self,
            method,
            url,
            json_body=json_body,
            params=params,
            schedule_type=schedule_type,
            endpoint_family=endpoint_family,
            write_intent=write_intent,
            supports_mqtt=supports_mqtt,
            strip_devices=strip_devices,
            partial_payload_only=partial_payload_only,
        )

    @staticmethod
    def _extract_xsrf_from_response_header(response: object) -> str | None:
        "Return the XSRF token from a response's ``x-csrf-token`` header."

        return api_battery_surface._extract_xsrf_from_response_header(response)

    @staticmethod
    def _extract_xsrf_from_response_cookies(response: object) -> str | None:
        "Return the XSRF token from Set-Cookie headers or response cookies."

        return api_battery_surface._extract_xsrf_from_response_cookies(response)

    async def _acquire_xsrf_token(
        self,
        schedule_type: str = "cfg",
        *,
        variant: str = _BATTERY_CONFIG_VARIANT_PRIMARY,
    ) -> str | None:
        "Acquire an XSRF token for BatteryConfig write operations."

        return await api_battery_surface._acquire_xsrf_token(
            self, schedule_type, variant=variant
        )

    def _redact_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Return a copy of headers with sensitive values masked."""

        redacted: dict[str, str] = {}
        for key, value in headers.items():
            if key.lower() in {
                "cookie",
                "authorization",
                "e-auth-token",
                "enlm-token",
                "x-csrf-token",
                "x-xsrf-token",
                "username",
            }:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_text(value, site_ids=(self._site,))
        return redacted

    @staticmethod
    def _truncate_debug_identifier(value: object) -> str | None:
        """Return the shared log-safe identifier representation."""

        return truncate_identifier(value)

    def _redact_debug_text(
        self,
        value: object,
        *,
        device_uid: object | None = None,
    ) -> str:
        """Return compact debug text with site-specific IDs removed."""

        try:
            text = " ".join(str(value or "").split()).strip()
        except Exception:  # noqa: BLE001 - defensive casting
            return ""
        if not text:
            return ""

        replacements: list[tuple[str, str]] = []
        try:
            site_text = str(self._site).strip()
        except Exception:  # noqa: BLE001
            site_text = ""
        if site_text:
            replacements.append((site_text, "[site]"))

        if device_uid is not None:
            try:
                raw_uid = str(device_uid).strip()
            except Exception:  # noqa: BLE001
                raw_uid = ""
            safe_uid = self._truncate_debug_identifier(raw_uid)
            if raw_uid and safe_uid:
                replacements.append((raw_uid, safe_uid))

        for raw, safe in replacements:
            text = text.replace(raw, safe)

        text = _EMAIL_RE.sub("[redacted]", text)
        text = _DEBUG_KV_RE.sub(self._redact_debug_kv_match, text)
        if len(text) > 256:
            text = f"{text[:256]}..."
        return text

    def _redact_debug_kv_match(self, match: re.Match[str]) -> str:
        """Redact inline key/value debug fragments such as ``serial=...``."""

        key = match.group("key")
        sep = match.group("sep")
        value = match.group("value")
        kind = self._debug_query_key_kind(key)
        if kind == "redact":
            safe_value = "[redacted]"
        elif kind == "truncate":
            safe_value = self._truncate_debug_identifier(value) or "[redacted]"
        else:
            safe_value = value
        return f"{key}{sep}{safe_value}"

    @staticmethod
    def _debug_query_key_kind(key: object) -> str:
        """Return the debug-redaction strategy for a query parameter name."""

        try:
            key_text = str(key).strip().lower()
        except Exception:  # noqa: BLE001 - defensive casting
            return "text"
        compact = "".join(ch for ch in key_text if ch.isalnum())
        if not compact:
            return "text"
        if any(
            token in compact
            for token in (
                "token",
                "auth",
                "cookie",
                "email",
                "user",
                "pass",
                "secret",
            )
        ):
            return "redact"
        if compact in {
            "deviceuid",
            "requesteddeviceuid",
            "deviceuids",
            "requesteddeviceuids",
        }:
            return "truncate"
        if "uid" in compact or "serial" in compact or compact.endswith(("id", "ids")):
            return "redact"
        return "text"

    def _debug_query_value(self, key: object, value: object) -> str:
        """Return a safe debug rendering for a URL query value."""

        kind = self._debug_query_key_kind(key)
        if kind == "redact":
            return "[redacted]"
        if kind == "truncate":
            return self._truncate_debug_identifier(value) or "[redacted]"
        text = self._redact_debug_text(value)
        return text or "[redacted]"

    def _debug_sanitize_payload(
        self,
        value: object,
        *,
        key: object | None = None,
        device_uid: object | None = None,
    ) -> object:
        """Return a redacted debug-safe representation of a payload."""

        kind = self._debug_query_key_kind(key) if key is not None else "text"
        if kind == "redact":
            return "[redacted]"
        if kind == "truncate":
            return self._truncate_debug_identifier(value) or "[redacted]"

        if isinstance(value, dict):
            out: dict[str, object] = {}
            for child_key, child_value in value.items():
                try:
                    key_text = str(child_key)
                except Exception:  # noqa: BLE001 - defensive casting
                    key_text = "[invalid]"
                out[key_text] = self._debug_sanitize_payload(
                    child_value,
                    key=key_text,
                    device_uid=device_uid,
                )
            return out
        if isinstance(value, list):
            return [
                self._debug_sanitize_payload(
                    item,
                    key=key,
                    device_uid=device_uid,
                )
                for item in value
            ]
        if isinstance(value, tuple):
            return [
                self._debug_sanitize_payload(
                    item,
                    key=key,
                    device_uid=device_uid,
                )
                for item in value
            ]

        text = self._redact_debug_text(value, device_uid=device_uid)
        if not text:
            return "[redacted]" if value is not None else None
        return text

    def _debug_error_message(
        self,
        value: object,
        *,
        device_uid: object | None = None,
    ) -> str:
        """Return a safe debug string for server-provided error content."""

        if isinstance(value, (dict, list, tuple)):
            sanitized = self._debug_sanitize_payload(value, device_uid=device_uid)
            try:
                return json.dumps(sanitized, sort_keys=True, ensure_ascii=True)
            except Exception:  # noqa: BLE001 - defensive serialization
                return self._redact_debug_text(sanitized, device_uid=device_uid)

        try:
            text = str(value or "").strip()
        except Exception:  # noqa: BLE001 - defensive casting
            text = ""
        if not text:
            return ""

        try:
            parsed = json.loads(text)
        except Exception:
            return self._redact_debug_text(text, device_uid=device_uid)

        sanitized = self._debug_sanitize_payload(parsed, device_uid=device_uid)
        try:
            return json.dumps(sanitized, sort_keys=True, ensure_ascii=True)
        except Exception:  # noqa: BLE001 - defensive serialization
            return self._redact_debug_text(sanitized, device_uid=device_uid)

    def _debug_request_context(
        self,
        method: object,
        url: object,
        *,
        requested_device_uid: object | None = None,
        site_date: object | None = None,
    ) -> dict[str, object]:
        """Return a sanitized request context suitable for debug logs."""

        try:
            method_text = str(method).strip().upper()
        except Exception:  # noqa: BLE001 - defensive casting
            method_text = "REQUEST"

        normalized_site_date = self._parse_evse_timeseries_date_key(site_date)
        context: dict[str, object] = {}

        try:
            url_obj = url if isinstance(url, URL) else URL(str(url))
        except Exception:  # noqa: BLE001 - fallback to raw text
            raw = self._redact_debug_text(url, device_uid=requested_device_uid)
            context["request"] = f"{method_text} {raw}" if raw else method_text
        else:
            path = url_obj.path or ""
            try:
                site_text = str(self._site).strip()
            except Exception:  # noqa: BLE001
                site_text = ""
            if site_text and path:
                path_parts = path.split("/")
                path = "/".join(
                    "[site]" if part == site_text else part for part in path_parts
                )

            query_bits: list[str] = []
            query_keys: list[str] = []
            for key, value in url_obj.query.items():
                key_text = str(key)
                query_keys.append(key_text)
                query_bits.append(
                    f"{key_text}={self._debug_query_value(key_text, value)}"
                )

            request_text = f"{method_text} {path}" if path else method_text
            if query_bits:
                request_text = f"{request_text}?{'&'.join(query_bits)}"
            context["request"] = request_text
            if query_keys:
                context["query_keys"] = query_keys
                if "device-uid" in query_keys or "device_uid" in query_keys:
                    context["has_device_uid"] = True
                for key_text in query_keys:
                    if key_text in {"start_date", "date"}:
                        context["date_key"] = key_text
                        break

        if normalized_site_date is not None:
            context["normalized_site_date"] = normalized_site_date
        requested_uid = self._truncate_debug_identifier(requested_device_uid)
        if requested_uid is not None:
            context["requested_device_uid"] = requested_uid
        return context

    async def _json(
        self,
        method: str,
        url: str,
        *,
        mark_payload_success: bool = True,
        log_invalid_payload: bool = True,
        **kwargs: Any,
    ) -> Any:
        "Perform an HTTP request returning JSON with sane header handling."

        return await api_request_surface._json(
            self,
            method,
            url,
            mark_payload_success=mark_payload_success,
            log_invalid_payload=log_invalid_payload,
            **kwargs,
        )

    @asynccontextmanager
    async def _request_session(
        self, *, cookie_header_only: bool = False
    ) -> AsyncIterator[aiohttp.ClientSession]:
        """Yield the HTTP session to use for a request.

        Requests with an explicit cookie policy need their headers sent without any
        session-jar merging. The injected stateless session avoids hidden cookie
        mutations from the shared client while preserving connection reuse.
        """

        if not cookie_header_only:
            yield self._s
            return

        session = self._cookie_header_session
        if session is None and isinstance(
            getattr(self._s, "cookie_jar", None), aiohttp.DummyCookieJar
        ):
            session = self._s
        if session is None:
            raise RuntimeError(
                "Cookie-header-only requests require an injected stateless session"
            )
        yield session

    async def _text_response(
        self,
        method: str,
        url: str,
        *,
        expected_statuses: tuple[int, ...] | None = None,
        mark_payload_success: bool = True,
        **kwargs: Any,
    ) -> TextResponse:
        "Perform an HTTP request returning text plus response metadata."

        return await api_request_surface._text_response(
            self,
            method,
            url,
            expected_statuses=expected_statuses,
            mark_payload_success=mark_payload_success,
            **kwargs,
        )

    async def _text(
        self,
        method: str,
        url: str,
        *,
        expected_statuses: tuple[int, ...] | None = None,
        mark_payload_success: bool = True,
        **kwargs: Any,
    ) -> str:
        "Perform an HTTP request returning text only."

        return await api_request_surface._text(
            self,
            method,
            url,
            expected_statuses=expected_statuses,
            mark_payload_success=mark_payload_success,
            **kwargs,
        )

    async def status(self) -> JsonDict:
        "Delegate status to its cloud surface."

        return await api_evse_surface.status(self)

    @staticmethod
    def _payload_has_level(payload: JsonDict | None) -> bool:
        "Return True when a payload explicitly includes a charging level."

        return api_evse_surface._payload_has_level(payload)

    def _start_charging_candidates(
        self, sn: str, level: int, connector_id: int
    ) -> list[tuple[str, str, JsonDict | None]]:
        "Delegate _start_charging_candidates to its cloud surface."

        return api_evse_surface._start_charging_candidates(
            self, sn, level, connector_id
        )

    async def start_charging(
        self,
        sn: str,
        amps: int,
        connector_id: int = 1,
        *,
        include_level: bool | None = None,
        strict_preference: bool = False,
    ) -> JsonDict:
        "Start charging or set the charging level."

        return await api_evse_surface.start_charging(
            self,
            sn,
            amps,
            connector_id,
            include_level=include_level,
            strict_preference=strict_preference,
        )

    def _stop_charging_candidates(
        self, sn: str
    ) -> list[tuple[str, str, JsonDict | None]]:
        "Delegate _stop_charging_candidates to its cloud surface."

        return api_evse_surface._stop_charging_candidates(self, sn)

    @staticmethod
    def _is_routing_not_found(message: str | None) -> bool:
        "Return True when a 404 is a routing miss rather than action state."

        return api_evse_surface._is_routing_not_found(message)

    @staticmethod
    def _is_invalid_charge_level_error(message: str | None) -> bool:
        "Return True when a response reports an invalid charge level."

        return api_evse_surface._is_invalid_charge_level_error(message)

    async def stop_charging(self, sn: str) -> JsonDict:
        "Stop charging; try multiple endpoint variants."

        return await api_evse_surface.stop_charging(self, sn)

    async def trigger_message(self, sn: str, requested_message: str) -> JsonDict:
        "Delegate trigger_message to its cloud surface."

        return await api_evse_surface.trigger_message(self, sn, requested_message)

    async def start_live_stream(self) -> JsonDict:
        "Delegate start_live_stream to its cloud surface."

        return await api_evse_surface.start_live_stream(self)

    async def stop_live_stream(self) -> JsonDict:
        "Delegate stop_live_stream to its cloud surface."

        return await api_evse_surface.stop_live_stream(self)

    async def charge_mode(self, sn: str) -> str | None:
        "Fetch the current charge mode via scheduler API."

        return await api_evse_surface.charge_mode(self, sn)

    async def set_charge_mode(
        self, sn: str, mode: str, *, previous_mode: str | None = None
    ) -> JsonDict:
        "Set the charging mode via scheduler API."

        return await api_evse_surface.set_charge_mode(
            self, sn, mode, previous_mode=previous_mode
        )

    async def _charge_mode_write_landed(self, sn: str, mode: str) -> bool:
        "Return True if a failed preference write is visible on read-back."

        return await api_evse_surface._charge_mode_write_landed(self, sn, mode)

    async def green_charging_settings(self, sn: str) -> list[dict[str, Any]]:
        "Return green charging settings for the charger."

        return await api_evse_surface.green_charging_settings(self, sn)

    async def set_green_battery_setting(self, sn: str, *, enabled: bool) -> JsonDict:
        "Toggle green charging battery support."

        return await api_evse_surface.set_green_battery_setting(
            self, sn, enabled=enabled
        )

    async def storm_guard_alert(self) -> JsonDict:
        "Return Storm Guard alert status for the site."

        return await api_battery_surface.storm_guard_alert(self)

    async def opt_out_storm_alert(self, *, alert_id: str, name: str) -> JsonDict:
        "Opt out of a specific Storm Guard alert."

        return await api_battery_surface.opt_out_storm_alert(
            self, alert_id=alert_id, name=name
        )

    async def storm_guard_profile(self, *, locale: str | None = None) -> JsonDict:
        "Return Storm Guard state and EVSE settings for the site."

        return await api_battery_surface.storm_guard_profile(self, locale=locale)

    async def battery_site_settings(self) -> JsonDict:
        "Return BatteryConfig site settings and feature flags."

        return await api_battery_surface.battery_site_settings(self)

    async def site_tariff_billing_details(self) -> JsonDict:
        "Return site tariff billing-cycle details."

        return await api_dashboard_surface.site_tariff_billing_details(self)

    async def site_tariff_billing_update(
        self,
        payload: dict[str, Any],
        *,
        request_date: date | datetime | str | None = None,
    ) -> JsonDict:
        "Update site tariff billing-cycle details."

        return await api_dashboard_surface.site_tariff_billing_update(
            self, payload, request_date=request_date
        )

    async def site_tariff(self) -> JsonDict:
        "Return site import/export tariff configuration."

        return await api_dashboard_surface.site_tariff(self)

    async def site_tariff_rates(
        self,
        *,
        rate_type: str,
        request_date: date | datetime | str | None = None,
    ) -> JsonDict:
        "Return dated tariff rates for a site tariff branch."

        return await api_dashboard_surface.site_tariff_rates(
            self, rate_type=rate_type, request_date=request_date
        )

    async def site_tariff_bundle(self) -> tuple[JsonDict, JsonDict]:
        "Return billing details and tariff configuration for the site."

        return await api_dashboard_surface.site_tariff_bundle(self)

    async def site_tariff_update(self, payload: dict[str, Any]) -> JsonDict:
        "Update site import/export tariff configuration."

        return await api_dashboard_surface.site_tariff_update(self, payload)

    async def notify_tariff_change(self) -> JsonDict:
        "Notify the EVSE scheduler service that site tariff data changed."

        return await api_dashboard_surface.notify_tariff_change(self)

    async def async_get_activation_reference_data(self) -> JsonDict:
        "Return Activation country/region reference data."

        return await api_activation_surface.async_get_activation_reference_data(self)

    async def async_get_activation_record(self) -> JsonDict:
        "Return the cloud Activation record for this site."

        return await api_activation_surface.async_get_activation_record(self)

    async def async_get_activation_device_list(self) -> JsonDict:
        "Return Activation device inventory and current grid-profile status."

        return await api_activation_surface.async_get_activation_device_list(self)

    async def async_get_grid_profiles_filtered(
        self,
        *,
        country: str,
        state: str,
        commonly_used: bool = True,
    ) -> JsonDict:
        "Return grid profiles for a country/region from Activation."

        return await api_activation_surface.async_get_grid_profiles_filtered(
            self, country=country, state=state, commonly_used=commonly_used
        )

    async def async_apply_grid_profile(
        self,
        *,
        gateway_serial: str,
        part_num: str | None,
        ensemble_envoy: bool,
        profile_id: str,
    ) -> JsonDict:
        "Apply a cloud Activation grid profile to a Gateway."

        return await api_activation_surface.async_apply_grid_profile(
            self,
            gateway_serial=gateway_serial,
            part_num=part_num,
            ensemble_envoy=ensemble_envoy,
            profile_id=profile_id,
        )

    async def battery_profile_details(self, *, locale: str | None = None) -> JsonDict:
        "Return BatteryConfig profile details for system + EVSE settings."

        return await api_battery_surface.battery_profile_details(self, locale=locale)

    async def battery_settings_details(self) -> JsonDict:
        "Return BatteryConfig battery details for charge-grid and shutdown controls."

        return await api_battery_surface.battery_settings_details(self)

    async def accept_battery_settings_disclaimer(
        self, disclaimer_type: str = "itc"
    ) -> JsonDict:
        "Acknowledge the BatteryConfig charge-from-grid disclaimer."

        return await api_battery_surface.accept_battery_settings_disclaimer(
            self, disclaimer_type
        )

    async def set_battery_settings(
        self,
        payload: dict[str, Any],
        *,
        schedule_type: str = "cfg",
    ) -> JsonDict:
        "Update BatteryConfig battery detail settings using a partial payload."

        return await api_battery_surface.set_battery_settings(
            self, payload, schedule_type=schedule_type
        )

    async def set_battery_settings_compat(
        self,
        payload: dict[str, Any],
        *,
        schedule_type: str = "cfg",
        include_source: bool = True,
        merged_payload: bool = False,
        strip_devices: bool = False,
        partial_payload_only: bool = False,
    ) -> JsonDict:
        "Update battery settings using an explicit compatibility payload shape."

        return await api_battery_surface.set_battery_settings_compat(
            self,
            payload,
            schedule_type=schedule_type,
            include_source=include_source,
            merged_payload=merged_payload,
            strip_devices=strip_devices,
            partial_payload_only=partial_payload_only,
        )

    async def set_battery_profile(
        self,
        *,
        profile: str,
        battery_backup_percentage: int,
        operation_mode_sub_type: str | None = None,
        devices: list[dict[str, Any]] | None = None,
    ) -> JsonDict:
        "Update the site battery profile and reserve percentage."

        return await api_battery_surface.set_battery_profile(
            self,
            profile=profile,
            battery_backup_percentage=battery_backup_percentage,
            operation_mode_sub_type=operation_mode_sub_type,
            devices=devices,
        )

    async def cancel_battery_profile_update(self) -> JsonDict:
        "Cancel a pending site battery profile change."

        return await api_battery_surface.cancel_battery_profile_update(self)

    async def set_storm_guard(self, *, enabled: bool, evse_enabled: bool) -> JsonDict:
        "Toggle Storm Guard and the EVSE charge-to-100% option."

        return await api_battery_surface.set_storm_guard(
            self, enabled=enabled, evse_enabled=evse_enabled
        )

    # ------------------------------------------------------------------
    # Battery schedule CRUD (newer /battery/sites/{id}/schedules API)
    # ------------------------------------------------------------------

    async def battery_schedules(self) -> JsonDict:
        "Return all battery schedules for the site."

        return await api_battery_surface.battery_schedules(self)

    async def create_battery_schedule(
        self,
        *,
        schedule_type: str,
        start_time: str,
        end_time: str,
        limit: int | None,
        days: list[int],
        timezone: str = "UTC",
        is_enabled: bool | None = None,
    ) -> JsonDict:
        "Create a new battery schedule."

        return await api_battery_surface.create_battery_schedule(
            self,
            schedule_type=schedule_type,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            days=days,
            timezone=timezone,
            is_enabled=is_enabled,
        )

    async def update_battery_schedule(
        self,
        schedule_id: str | int,
        *,
        schedule_type: str,
        start_time: str,
        end_time: str,
        limit: int | None,
        days: list[int],
        timezone: str = "UTC",
        is_enabled: bool | None = None,
        is_deleted: bool | None = None,
    ) -> JsonDict:
        "Update an existing battery schedule in-place."

        return await api_battery_surface.update_battery_schedule(
            self,
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

    async def delete_battery_schedule(
        self,
        schedule_id: str | int,
        *,
        schedule_type: str = "cfg",
    ) -> JsonDict:
        "Delete a battery schedule by ID."

        return await api_battery_surface.delete_battery_schedule(
            self, schedule_id, schedule_type=schedule_type
        )

    async def validate_battery_schedule(self, schedule_type: str = "cfg") -> JsonDict:
        "Validate a battery schedule configuration."

        return await api_battery_surface.validate_battery_schedule(self, schedule_type)

    async def charger_auth_settings(self, sn: str) -> list[dict[str, Any]]:
        "Return authentication settings for the charger."

        return await api_evse_surface.charger_auth_settings(self, sn)

    async def charger_config(
        self,
        sn: str,
        keys: Iterable[str],
    ) -> list[dict[str, Any]]:
        "Return raw charger config entries for the requested keys."

        return await api_evse_surface.charger_config(self, sn, keys)

    async def set_app_authentication(self, sn: str, *, enabled: bool) -> JsonDict:
        "Enable or disable session authentication via app."

        return await api_evse_surface.set_app_authentication(self, sn, enabled=enabled)

    async def set_default_charge_level(self, sn: str, amps: int) -> JsonDict:
        "Set the charger's stored default charge level."

        return await api_evse_surface.set_default_charge_level(self, sn, amps)

    async def get_schedules(self, sn: str) -> JsonDict:
        "Return scheduler config and slots for the charger."

        return await api_evse_surface.get_schedules(self, sn)

    async def patch_schedules(
        self, sn: str, *, server_timestamp: str, slots: list[JsonDict]
    ) -> JsonDict:
        "Patch the scheduler slots for the charger."

        return await api_evse_surface.patch_schedules(
            self, sn, server_timestamp=server_timestamp, slots=slots
        )

    async def patch_schedule_states(
        self, sn: str, *, slot_states: dict[str, bool]
    ) -> JsonDict:
        "Patch schedule slot enabled states for the charger."

        return await api_evse_surface.patch_schedule_states(
            self, sn, slot_states=slot_states
        )

    async def patch_schedule(self, sn: str, slot_id: str, slot: JsonDict) -> JsonDict:
        "Patch a single schedule slot for the charger."

        return await api_evse_surface.patch_schedule(self, sn, slot_id, slot)

    async def create_schedule(self, sn: str, slot: JsonDict) -> JsonDict:
        "Create a single schedule slot for the charger."

        return await api_evse_surface.create_schedule(self, sn, slot)

    async def delete_schedule(self, sn: str, slot_id: str) -> JsonDict:
        "Delete a single schedule slot for the charger."

        return await api_evse_surface.delete_schedule(self, sn, slot_id)

    async def lifetime_energy(self) -> JsonDict | None:
        "Return lifetime energy buckets for the configured site."

        return await api_dashboard_surface.lifetime_energy(self)

    async def weather(self, *, locale: str) -> JsonDict:
        "Return the current weather reported for the configured site."

        return await api_dashboard_surface.weather(self, locale=locale)

    @classmethod
    def _normalize_latest_power_payload(
        cls, payload: object
    ) -> dict[str, object] | None:
        """Normalize app-api latest power payloads into a common shape."""

        return api_site_surface.normalize_latest_power(payload)

    async def latest_power(self) -> dict[str, object] | None:
        "Return the latest site power sample for the configured site."

        return await api_dashboard_surface.latest_power(self)

    async def show_livestream(
        self, *, allow_reauth: bool = True
    ) -> dict[str, object] | None:
        "Return live-status/vitals capability flags when available."

        return await api_dashboard_surface.show_livestream(
            self, allow_reauth=allow_reauth
        )

    async def site_livestream_authorizer(
        self,
        serial_num: str,
        *,
        live_debug: bool = False,
        allow_reauth: bool = True,
    ) -> dict[str, object] | None:
        "Return signed AWS IoT connection details for the site live stream."

        return await api_dashboard_surface.site_livestream_authorizer(
            self, serial_num, live_debug=live_debug, allow_reauth=allow_reauth
        )

    async def site_livestream_payload(
        self,
        serial_num: str,
        *,
        live_debug: bool = False,
        timeout_s: float = 15.0,
        allow_reauth: bool = True,
    ) -> dict[str, object] | None:
        "Read and decode one MQTT payload from the signed site live stream."

        return await api_dashboard_surface.site_livestream_payload(
            self,
            serial_num,
            live_debug=live_debug,
            timeout_s=timeout_s,
            allow_reauth=allow_reauth,
        )

    @staticmethod
    def _normalize_evse_timeseries_serial(value: object) -> str | None:
        serial = api_parsers.normalize_evse_timeseries_serial(value)
        return str(serial) if serial is not None else None

    @staticmethod
    def _parse_evse_timeseries_date_key(value: object) -> str | None:
        date_key = api_parsers.parse_evse_timeseries_date_key(value)
        return str(date_key) if date_key is not None else None

    @classmethod
    def _coerce_evse_timeseries_energy(
        cls,
        value: object,
        *,
        key_hint: str | None = None,
        unit_hint: object | None = None,
    ) -> float | None:
        return api_parsers.coerce_evse_timeseries_energy(
            value,
            key_hint=key_hint,
            unit_hint=unit_hint,
        )

    @classmethod
    def _normalize_evse_timeseries_metadata(cls, payload: object) -> dict[str, object]:
        return api_parsers.normalize_evse_timeseries_metadata(payload)

    @classmethod
    def _daily_values_from_mapping(
        cls,
        payload: dict[str, object],
    ) -> tuple[dict[str, float], float | None]:
        return api_parsers.daily_values_from_mapping(
            payload,
            parse_date_key=cls._parse_evse_timeseries_date_key,
            coerce_energy=cls._coerce_evse_timeseries_energy,
        )

    @classmethod
    def _daily_values_from_sequence(
        cls,
        values: list[object],
        *,
        start_date_value: object | None = None,
        unit_hint: object | None = None,
    ) -> tuple[dict[str, float], float | None]:
        return api_parsers.daily_values_from_sequence(
            values,
            start_date_value=start_date_value,
            unit_hint=unit_hint,
            parse_date_key=cls._parse_evse_timeseries_date_key,
            coerce_energy=cls._coerce_evse_timeseries_energy,
        )

    @classmethod
    def _normalize_evse_daily_entry(
        cls,
        serial: str,
        payload: object,
        *,
        base_metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        return api_parsers.normalize_evse_daily_entry(
            serial,
            payload,
            base_metadata=base_metadata,
            parse_date_key=cls._parse_evse_timeseries_date_key,
            coerce_energy=cls._coerce_evse_timeseries_energy,
        )

    @classmethod
    def _normalize_evse_lifetime_entry(
        cls,
        serial: str,
        payload: object,
        *,
        base_metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        return api_parsers.normalize_evse_lifetime_entry(
            serial,
            payload,
            base_metadata=base_metadata,
            coerce_energy=cls._coerce_evse_timeseries_energy,
        )

    @classmethod
    def _normalize_evse_timeseries_payload(
        cls,
        payload: object,
        *,
        daily: bool,
    ) -> dict[str, dict[str, object]] | None:
        return api_parsers.normalize_evse_timeseries_payload(
            payload,
            daily=daily,
            parse_date_key=cls._parse_evse_timeseries_date_key,
            coerce_energy=cls._coerce_evse_timeseries_energy,
        )

    async def evse_timeseries_daily_energy(
        self,
        *,
        start_date: str | date | datetime | None = None,
        request_id: str | None = None,
        username: str | None = None,
    ) -> dict[str, dict[str, object]] | None:
        "Return EVSE daily timeseries keyed by charger serial."

        return await api_dashboard_surface.evse_timeseries_daily_energy(
            self, start_date=start_date, request_id=request_id, username=username
        )

    async def evse_timeseries_lifetime_energy(
        self,
        *,
        request_id: str | None = None,
        username: str | None = None,
    ) -> dict[str, dict[str, object]] | None:
        "Return EVSE lifetime timeseries keyed by charger serial."

        return await api_dashboard_surface.evse_timeseries_lifetime_energy(
            self, request_id=request_id, username=username
        )

    @classmethod
    def _coerce_non_boolean_number(cls, value: object) -> float | None:
        """Normalize numeric values while rejecting JSON booleans."""

        return api_parsers.coerce_non_boolean_number(value)

    @classmethod
    def _normalize_lifetime_energy_payload(cls, payload: object) -> JsonDict | None:
        """Normalize site/HEMS lifetime-energy payloads into a common shape."""

        return cast(
            JsonDict | None, api_parsers.normalize_lifetime_energy_payload(payload)
        )

    async def hems_consumption_lifetime(self) -> JsonDict | None:
        "Return HEMS lifetime consumption buckets when available."

        return await api_dashboard_surface.hems_consumption_lifetime(self)

    @staticmethod
    def _clean_optional_text(value: object) -> str | None:
        """Return a trimmed string value when present."""
        text = api_parsers.clean_optional_text(value)
        return str(text) if text is not None else None

    @classmethod
    def _heatpump_sg_ready_mode_details(cls, value: object) -> dict[str, object]:
        """Map raw HEMS SG Ready mode labels to app-facing semantics."""

        return cast(
            dict[str, object], api_parsers.heatpump_sg_ready_mode_details(value)
        )

    @classmethod
    def _normalize_hems_heatpump_state_payload(cls, payload: object) -> JsonDict | None:
        """Normalize HEMS heat-pump runtime state payloads."""

        return cast(
            JsonDict | None,
            api_parsers.normalize_hems_heatpump_state_payload(payload),
        )

    @classmethod
    def _normalize_hems_daily_consumption_entry(
        cls, payload: object
    ) -> dict[str, object] | None:
        """Normalize a HEMS daily-consumption device entry."""

        return api_parsers.normalize_hems_daily_consumption_entry(payload)

    @classmethod
    def _normalize_hems_energy_consumption_payload(
        cls, payload: object
    ) -> JsonDict | None:
        """Normalize HEMS daily energy-consumption payloads."""

        return cast(
            JsonDict | None,
            api_parsers.normalize_hems_energy_consumption_payload(payload),
        )

    @classmethod
    def _normalize_pv_system_today_payload(cls, payload: object) -> JsonDict | None:
        """Normalize site-today payloads used by heat-pump daily totals."""

        return cast(
            JsonDict | None, api_parsers.normalize_pv_system_today_payload(payload)
        )

    async def hems_heatpump_state(
        self, device_uid: str, *, timezone: str | None = None
    ) -> JsonDict | None:
        "Return HEMS heat-pump runtime state when available."

        return await api_dashboard_surface.hems_heatpump_state(
            self, device_uid, timezone=timezone
        )

    async def hems_energy_consumption(
        self,
        *,
        start_at: str,
        end_at: str,
        timezone: str,
        step: str = "P1D",
    ) -> JsonDict | None:
        "Return HEMS daily device energy-consumption buckets when available."

        return await api_dashboard_surface.hems_energy_consumption(
            self, start_at=start_at, end_at=end_at, timezone=timezone, step=step
        )

    async def pv_system_today(self, *, allow_reauth: bool = True) -> JsonDict | None:
        "Return the site today payload when available."

        return await api_dashboard_surface.pv_system_today(
            self, allow_reauth=allow_reauth
        )

    async def heat_pump_events_json(
        self, device_uid: str
    ) -> JsonDict | list[Any] | None:
        "Return per-device HEMS heat-pump events payload when available."

        return await api_dashboard_surface.heat_pump_events_json(self, device_uid)

    async def iq_er_events_json(self, device_uid: str) -> JsonDict | list[Any] | None:
        "Return per-device HEMS IQ Energy Router events payload when available."

        return await api_dashboard_surface.iq_er_events_json(self, device_uid)

    async def summary_v2(self) -> list[JsonDict] | None:
        "Fetch charger summary v2 list."

        return await api_dashboard_surface.summary_v2(self)

    async def evse_fw_details(self) -> list[dict[str, Any]] | None:
        "Fetch EVSE firmware details for the current site."

        return await api_dashboard_surface.evse_fw_details(self)

    async def evse_feature_flags(
        self, *, country: str | None = None
    ) -> JsonDict | None:
        "Return EVSE feature flags and UI gating details for the site."

        return await api_dashboard_surface.evse_feature_flags(self, country=country)

    async def devices_inventory(self) -> JsonDict:
        "Return site device inventory grouped by hardware type."

        return await api_dashboard_surface.devices_inventory(self)

    async def phase_map_multiple_envoy(self) -> JsonDict | None:
        "Return per-gateway phase and topology metadata for the site."

        return await api_dashboard_surface.phase_map_multiple_envoy(self)

    async def devices_tree(self) -> JsonDict | None:
        "Return the system dashboard device hierarchy when available."

        return await api_dashboard_surface.devices_tree(self)

    async def vpp_enrollment_id(self) -> object:
        """Return the VPP enrollment lookup wrapper for this site."""

        return await api_vpp_surface.enrollment_id(self, gs_base_url=GS_BASE_URL)

    async def vpp_enrollment_details(self, enrollment_id: str) -> object:
        """Return VPP enrollment details for one enrollment id."""

        return await api_vpp_surface.enrollment_details(
            self,
            enrollment_id,
            gs_base_url=GS_BASE_URL,
        )

    async def vpp_events(self, program_id: str) -> object:
        """Return the default VPP event result for one program."""

        return await api_vpp_surface.events(
            self,
            program_id,
            gs_base_url=GS_BASE_URL,
        )

    async def system_dashboard_summary(
        self, *, allow_reauth: bool = True
    ) -> JsonDict | None:
        "Return the system dashboard capability summary when available."

        return await api_dashboard_surface.system_dashboard_summary(
            self, allow_reauth=allow_reauth
        )

    async def system_dashboard_events(self) -> JsonDict | None:
        "Return current System Dashboard event rows and lookup catalogs."

        return await api_dashboard_surface.system_dashboard_events(self)

    async def homeowner_events_page(
        self,
        *,
        next_cursor: str = "start",
        page_size: int = 200,
        locale: str = "en",
    ) -> JsonDict | None:
        "Return one cursor-paginated homeowner event-history page."

        return await api_dashboard_surface.homeowner_events_page(
            self, next_cursor=next_cursor, page_size=page_size, locale=locale
        )

    async def system_dashboard_standing_alarms(self) -> JsonDict | None:
        "Return current System Dashboard standing alarms."

        return await api_dashboard_surface.system_dashboard_standing_alarms(self)

    async def devices_details(self, type_key: str) -> JsonDict | None:
        "Return system dashboard per-type device details when available."

        return await api_dashboard_surface.devices_details(self, type_key)

    async def system_dashboard_master_data(self) -> JsonDict | None:
        "Return the system-dashboard device and parameter catalogs."

        return await api_dashboard_surface.system_dashboard_master_data(self)

    async def system_dashboard_envoy_inverters(
        self, gateway_serial: str
    ) -> JsonDict | None:
        "Return flattened microinverter inventory for one gateway."

        return await api_dashboard_surface.system_dashboard_envoy_inverters(
            self, gateway_serial
        )

    async def system_dashboard_data_columns(
        self, gateway_serial: str
    ) -> JsonDict | None:
        "Return device-level parameter column metadata for one gateway."

        return await api_dashboard_surface.system_dashboard_data_columns(
            self, gateway_serial
        )

    async def system_dashboard_parameter_view(
        self,
        serial_numbers: list[str] | tuple[str, ...],
        parameter_id: str,
        *,
        per_page: int = 500,
        page: int = 1,
        range_name: str = "today",
        start_date: str = "",
        end_date: str = "",
        sort_by_date: str = "desc",
    ) -> JsonDict | None:
        "Return one parameter for many devices in a single dashboard request."

        return await api_dashboard_surface.system_dashboard_parameter_view(
            self,
            serial_numbers,
            parameter_id,
            per_page=per_page,
            page=page,
            range_name=range_name,
            start_date=start_date,
            end_date=end_date,
            sort_by_date=sort_by_date,
        )

    async def hems_devices(self, *, refresh_data: bool = False) -> JsonDict | None:
        "Return dedicated HEMS device inventory when available."

        return await api_dashboard_surface.hems_devices(self, refresh_data=refresh_data)

    async def grid_control_check(self) -> JsonDict:
        "Return site-level grid control eligibility guard flags."

        return await api_dashboard_surface.grid_control_check(self)

    async def off_grid_due_to_grid_outage(self) -> JsonDict:
        "Return live grid-outage/off-grid context for the site."

        return await api_dashboard_surface.off_grid_due_to_grid_outage(self)

    async def request_grid_toggle_otp(self) -> JsonDict:
        "Request OTP delivery for a site grid-mode toggle."

        return await api_dashboard_surface.request_grid_toggle_otp(self)

    async def validate_grid_toggle_otp(self, otp: str) -> bool:
        "Validate a grid-mode OTP for the configured site."

        return await api_dashboard_surface.validate_grid_toggle_otp(self, otp)

    async def set_grid_state(self, envoy_serial_number: str, state: int) -> JsonDict:
        "Submit a grid relay state-change request."

        return await api_dashboard_surface.set_grid_state(
            self, envoy_serial_number, state
        )

    async def log_grid_change(
        self,
        envoy_serial_number: str,
        old_state: str,
        new_state: str,
    ) -> JsonDict:
        "Write grid relay transition audit metadata."

        return await api_dashboard_surface.log_grid_change(
            self, envoy_serial_number, old_state, new_state
        )

    async def battery_backup_history(self) -> JsonDict:
        "Return battery backup outage history for the site."

        return await api_dashboard_surface.battery_backup_history(self)

    async def battery_status(self) -> JsonDict:
        "Return battery status payload used by the Enlighten battery card."

        return await api_dashboard_surface.battery_status(self)

    async def ac_battery_devices_page(self, *, status: str = "active") -> str:
        "Return the AC Battery devices page HTML for the site."

        return await api_dashboard_surface.ac_battery_devices_page(self, status=status)

    async def ac_battery_detail_page(self, battery_id: str) -> str:
        "Return the AC Battery detail page HTML."

        return await api_dashboard_surface.ac_battery_detail_page(self, battery_id)

    async def ac_battery_events_page(self, battery_id: str) -> str:
        "Return the AC Battery events page HTML."

        return await api_dashboard_surface.ac_battery_events_page(self, battery_id)

    async def ac_battery_show_stat_data(self, battery_id: str) -> str:
        "Return the AC Battery telemetry HTML fragment."

        return await api_dashboard_surface.ac_battery_show_stat_data(self, battery_id)

    async def set_ac_battery_sleep(
        self, battery_id: str, sleep_min_soc: int
    ) -> TextResponse:
        "Request AC Battery sleep mode using the Enlighten web route."

        return await api_dashboard_surface.set_ac_battery_sleep(
            self, battery_id, sleep_min_soc
        )

    async def set_ac_battery_wake(self, battery_id: str) -> TextResponse:
        "Request AC Battery wake/cancel using the Enlighten web route."

        return await api_dashboard_surface.set_ac_battery_wake(self, battery_id)

    async def dry_contacts_settings(self) -> JsonDict:
        "Return dry-contact settings payload used by site settings views."

        return await api_dashboard_surface.dry_contacts_settings(self)

    async def inverters_inventory(
        self,
        *,
        limit: int = 1000,
        offset: int = 0,
        search: str = "",
    ) -> JsonDict:
        "Return site inverter inventory used by legacy microinverter views."

        return await api_dashboard_surface.inverters_inventory(
            self, limit=limit, offset=offset, search=search
        )

    async def inverter_status(self) -> dict[str, dict[str, Any]]:
        "Return inverter status map keyed by inverter id."

        return await api_dashboard_surface.inverter_status(self)

    async def inverter_production(
        self,
        *,
        start_date: str,
        end_date: str,
    ) -> JsonDict:
        "Return inverter production totals for a date range."

        return await api_dashboard_surface.inverter_production(
            self, start_date=start_date, end_date=end_date
        )

    async def session_history_filter_criteria(
        self,
        *,
        request_id: str | None = None,
        username: str | None = None,
    ) -> JsonDict:
        "Fetch session history filter criteria for a site."

        return await api_dashboard_surface.session_history_filter_criteria(
            self, request_id=request_id, username=username
        )

    async def session_history(
        self,
        sn: str,
        *,
        start_date: str,
        end_date: str | None = None,
        offset: int = 0,
        limit: int = 20,
        timezone: str | None = None,
        request_id: str | None = None,
        username: str | None = None,
    ) -> JsonDict:
        "Fetch charging sessions for a charger between the provided dates."

        return await api_dashboard_surface.session_history(
            self,
            sn,
            start_date=start_date,
            end_date=end_date,
            offset=offset,
            limit=limit,
            timezone=timezone,
            request_id=request_id,
            username=username,
        )
