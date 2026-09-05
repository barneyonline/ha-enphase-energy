"""Battery surface for the stable Enphase client facade."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from dataclasses import replace
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote

import aiohttp

from ..const import (
    BASE_URL,
    ENTREZ_URL,
)
from .errors import (
    EnphaseLoginWallUnauthorized,
)

if TYPE_CHECKING:
    from ..api import EnphaseEVClient

from .common import (
    _BATTERY_CONFIG_BROWSER_USER_AGENT,
    _BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
    _BATTERY_CONFIG_VARIANT_LEAN,
    _BATTERY_CONFIG_VARIANT_MIXED,
    _BATTERY_CONFIG_VARIANT_PRIMARY,
    _BATTERY_CONFIG_VARIANT_SESSION_COOKIE,
    _LOGGER,
    _XSRF_COOKIE_NAMES,
    JsonDict,
    _authorization_bearer_token,
    _BatteryConfigWriteAttempt,
    _coerce_cookie_map,
    _cookie_header_from_map,
    _cookie_map_from_header,
    _extract_xsrf_token,
    _jwt_user_id,
    _request_label,
    _seed_cookie_jar,
    _serialize_cookie_jar,
)


def _battery_config_user_id(self: EnphaseEVClient) -> str | None:
    """Return the user id for BatteryConfig requests when available."""

    _token, user_id = self._battery_config_auth_context()
    return user_id


def _battery_config_single_auth_token(self: EnphaseEVClient) -> str | None:
    """Return the single-token auth candidate used by external clients."""

    return self._bearer() or self._eauth


def _battery_config_user_id_for_token(
    self: EnphaseEVClient, token: str | None = None
) -> str | None:
    """Return the preferred BatteryConfig user id for a specific token."""

    if token:
        user_id = _jwt_user_id(token)
        if user_id:
            return user_id
    return self._battery_config_user_id()


def _battery_config_auth_source_label(self: EnphaseEVClient, token: str | None) -> str:
    """Return a coarse label describing the selected BatteryConfig token source."""

    if not token:
        return "none"
    bearer = self._bearer()
    eauth = self._eauth
    if bearer and eauth and token == bearer and token == eauth:
        return "shared"
    if bearer and eauth and token in {bearer, eauth} and bearer != eauth:
        return "mixed"
    if bearer and token == bearer:
        return "manager_cookie"
    if eauth and token == eauth:
        return "access_token"
    return "unknown"


def _battery_config_header_debug_flags(
    self: EnphaseEVClient,
    headers: dict[str, str],
    *,
    auth_source_override: str | None = None,
) -> dict[str, object]:
    """Return safe debug flags describing BatteryConfig auth-header shape."""

    bearer = _authorization_bearer_token(headers)
    eauth = headers.get("e-auth-token")
    auth_mode = "none"
    if bearer and eauth:
        auth_mode = "dual_match" if bearer == eauth else "dual_mismatch"
    elif bearer:
        auth_mode = "authorization_only"
    elif eauth:
        auth_mode = "eauth_only"

    auth_source = auth_source_override or self._battery_config_auth_source_label(
        bearer or eauth
    )
    if auth_mode == "dual_mismatch":
        auth_source = "mixed"

    return {
        "has_authorization": "Authorization" in headers,
        "has_e_auth_token": "e-auth-token" in headers,
        "has_requestid": "requestid" in headers,
        "has_username": "Username" in headers,
        "has_x_csrf_token": "X-CSRF-Token" in headers,
        "has_x_xsrf_token": "X-XSRF-Token" in headers,
        "auth_mode": auth_mode,
        "auth_source": auth_source,
    }


def _battery_config_auth_context(
    self: EnphaseEVClient,
) -> tuple[str | None, str | None]:
    """Return preferred BatteryConfig auth token and resolved user id.

    Preference order follows captured browser behavior:
    1) manager bearer cookie token when it contains a usable user id
    2) access-token fallback when it contains a usable user id
    3) first available token when user id cannot be resolved
    """

    candidates: list[str] = []
    bearer = self._bearer()
    if bearer:
        candidates.append(bearer)
    if self._eauth and self._eauth not in candidates:
        candidates.append(self._eauth)

    fallback_token: str | None = None
    for token in candidates:
        if fallback_token is None:
            fallback_token = token
        user_id = _jwt_user_id(token)
        if user_id:
            return token, user_id
    return fallback_token, None


def _battery_config_headers(
    self: EnphaseEVClient,
    *,
    include_xsrf: bool = False,
    variant: str = _BATTERY_CONFIG_VARIANT_PRIMARY,
) -> dict[str, str | None]:
    """Return headers for BatteryConfig read/write calls."""

    # BatteryConfig is backed by the first-party battery profile web app,
    # not the regular Enlighten XHR surface, so these headers intentionally
    # mimic that origin and user agent instead of reusing the base headers.
    headers: dict[str, str | None] = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://battery-profile-ui.enphaseenergy.com",
        "Referer": "https://battery-profile-ui.enphaseenergy.com/",
        "User-Agent": _BATTERY_CONFIG_BROWSER_USER_AGENT,
        "Authorization": None,
        "X-Requested-With": None,
        "Cookie": None,
        "e-auth-token": None,
        "X-CSRF-Token": None,
        "requestid": (
            str(uuid.uuid4())
            if variant
            in {
                _BATTERY_CONFIG_VARIANT_PRIMARY,
                _BATTERY_CONFIG_VARIANT_SESSION_COOKIE,
            }
            else None
        ),
    }
    token, user_id = self._battery_config_auth_context()
    if variant == _BATTERY_CONFIG_VARIANT_PRIMARY:
        headers["e-auth-token"] = token
    elif variant == _BATTERY_CONFIG_VARIANT_SESSION_COOKIE:
        headers["Cookie"] = self._battery_config_cookie(
            include_xsrf=include_xsrf,
            preserve_existing_xsrf=True,
        )
        headers["e-auth-token"] = None
    else:
        headers["e-auth-token"] = None
    if user_id:
        headers["Username"] = user_id
    else:
        headers.pop("Username", None)
    if include_xsrf:
        xsrf = self._xsrf_token()
        if xsrf:
            headers["X-XSRF-Token"] = xsrf
    return headers


def _battery_config_cookie_eauth_headers(
    self: EnphaseEVClient,
    *,
    include_xsrf: bool = False,
) -> dict[str, str | None]:
    """Return the cookie-backed external-compatible BatteryConfig headers."""

    token = self._battery_config_single_auth_token()
    user_id = self._battery_config_user_id_for_token(token)
    headers: dict[str, str | None] = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://battery-profile-ui.enphaseenergy.com",
        "Referer": "https://battery-profile-ui.enphaseenergy.com/",
        "User-Agent": _BATTERY_CONFIG_BROWSER_USER_AGENT,
        "Authorization": None,
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": self._cookie or None,
        "e-auth-token": token,
        "X-CSRF-Token": None,
        "requestid": None,
    }
    if user_id:
        headers["Username"] = user_id
    else:
        headers.pop("Username", None)
    if include_xsrf:
        xsrf = self._battery_config_cookie_header_xsrf_token()
        if xsrf:
            headers["X-XSRF-Token"] = xsrf
        else:
            headers["X-XSRF-Token"] = None
    return headers


def _battery_config_cookie(
    self: EnphaseEVClient,
    *,
    include_xsrf: bool = False,
    preserve_existing_xsrf: bool = False,
) -> str | None:
    """Return a normalized BatteryConfig cookie header value."""

    cookies: dict[str, str] = {}

    try:
        cookie_str = str(self._cookie) if self._cookie else ""
    except Exception:  # noqa: BLE001 - defensive parsing
        cookie_str = ""

    cookies.update(_cookie_map_from_header(cookie_str))

    jar = getattr(self._s, "cookie_jar", None)
    if jar is not None:
        _cookie_header, jar_cookies = _serialize_cookie_jar(
            jar,
            (
                BASE_URL,
                ENTREZ_URL,
                "https://battery-profile-ui.enphaseenergy.com",
            ),
        )
        cookies.update(jar_cookies)

    if not preserve_existing_xsrf:
        cookies = {
            name: value
            for name, value in cookies.items()
            if name.strip().lower() not in _XSRF_COOKIE_NAMES
        }

    if include_xsrf:
        xsrf = self._xsrf_token()
        if xsrf:
            cookies["BP-XSRF-Token"] = xsrf
    if not cookies:
        return None
    return _cookie_header_from_map(cookies)


def _battery_config_cookie_header_xsrf_token(self: EnphaseEVClient) -> str | None:
    """Return the BP-XSRF token from the stored cookie header."""

    try:
        parts = [p.strip() for p in (self._cookie or "").split(";")]
    except Exception:  # noqa: BLE001 - defensive parsing
        return None
    for part in parts:
        key, sep, value = part.partition("=")
        if not sep or key.strip().lower() not in _XSRF_COOKIE_NAMES:
            continue
        token = value.strip()
        if token.startswith('"') and token.endswith('"') and len(token) >= 2:
            token = token[1:-1]
        if not token:
            continue
        try:
            return unquote(token)
        except Exception:  # noqa: BLE001 - defensive decoding
            return token
    return None


def _battery_config_mixed_auth_headers(
    self: EnphaseEVClient,
    *,
    include_xsrf: bool = False,
) -> dict[str, str | None]:
    """Return the mixed-auth compatibility BatteryConfig headers."""

    headers: dict[str, str | None] = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://battery-profile-ui.enphaseenergy.com",
        "Referer": "https://battery-profile-ui.enphaseenergy.com/",
        "User-Agent": _BATTERY_CONFIG_BROWSER_USER_AGENT,
        "Authorization": None,
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": None,
        "e-auth-token": None,
        "X-CSRF-Token": None,
        "requestid": None,
    }
    token, user_id = self._battery_config_auth_context()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["Authorization"] = None
    headers["e-auth-token"] = token
    if user_id:
        headers["Username"] = user_id
    else:
        headers.pop("Username", None)
    cookie = self._battery_config_cookie(include_xsrf=include_xsrf)
    headers["Cookie"] = cookie
    if include_xsrf:
        xsrf = self._xsrf_token()
        if xsrf:
            headers["X-XSRF-Token"] = xsrf
            headers["X-CSRF-Token"] = xsrf
    else:
        headers["X-XSRF-Token"] = None
        headers["X-CSRF-Token"] = None
    return headers


def _battery_schedule_validation_payload(
    self: EnphaseEVClient, schedule_type: str = "cfg"
) -> dict[str, object]:
    """Return the XSRF bootstrap / validation payload for a schedule family."""

    normalized = str(schedule_type).lower()
    payload: dict[str, object] = {"scheduleType": normalized}
    if normalized == "cfg":
        payload["forceScheduleOpted"] = True
    return payload


def _battery_config_params(
    self: EnphaseEVClient,
    *,
    include_source: bool | str = False,
    locale: str | None = None,
) -> dict[str, str]:
    """Return query parameters for BatteryConfig calls."""

    params: dict[str, str] = {}
    user_id = self._battery_config_user_id()
    if user_id:
        params["userId"] = user_id
    if include_source:
        params["source"] = (
            str(include_source) if isinstance(include_source, str) else "enho"
        )
    if locale:
        params["locale"] = locale
    return params


def _battery_config_endpoint_family(self: EnphaseEVClient, url: str) -> str:
    """Return the cache family for a BatteryConfig endpoint URL."""

    if "/batterySettings/" in url:
        return "battery_settings"
    if "/battery/sites/" in url and "/schedules" in url:
        return "schedules"
    return "profile"


def _battery_config_variant_cache_key(
    self: EnphaseEVClient, endpoint_family: str
) -> tuple[str, str, str]:
    """Return the cache key for BatteryConfig request variants."""

    user_id = self._battery_config_user_id() or "<unknown>"
    return (str(self._site), user_id, str(endpoint_family))


def _battery_config_cached_variant(
    self: EnphaseEVClient, endpoint_family: str
) -> str | None:
    """Return the cached request variant for a BatteryConfig family."""

    key = self._battery_config_variant_cache_key(endpoint_family)
    return self._battery_config_variant_cache.get(key)


def _battery_config_variant_order(
    self: EnphaseEVClient, endpoint_family: str
) -> list[str]:
    """Return the ordered variants to try for a BatteryConfig family."""

    cached = self._battery_config_cached_variant(endpoint_family)
    has_session_cookie = bool(self._battery_config_cookie())
    variants = [
        cached,
        (_BATTERY_CONFIG_VARIANT_SESSION_COOKIE if has_session_cookie else None),
        _BATTERY_CONFIG_VARIANT_PRIMARY,
        _BATTERY_CONFIG_VARIANT_LEAN,
    ]
    return [
        variant
        for variant in dict.fromkeys(variants)
        if variant
        in {
            _BATTERY_CONFIG_VARIANT_SESSION_COOKIE,
            _BATTERY_CONFIG_VARIANT_PRIMARY,
            _BATTERY_CONFIG_VARIANT_LEAN,
        }
        and not (
            variant == _BATTERY_CONFIG_VARIANT_SESSION_COOKIE and not has_session_cookie
        )
    ]


def _battery_config_write_attempt_cache_key(
    self: EnphaseEVClient,
    endpoint_family: str,
    *,
    supports_mqtt: bool | None,
) -> tuple[str, str, str, str]:
    """Return the cache key for BatteryConfig write attempts."""

    user_id = self._battery_config_user_id() or "<unknown>"
    mqtt_key = (
        "mqtt"
        if supports_mqtt is True
        else "nomqtt" if supports_mqtt is False else "<unknown>"
    )
    return (str(self._site), user_id, str(endpoint_family), mqtt_key)


def _battery_config_cached_write_attempt(
    self: EnphaseEVClient,
    endpoint_family: str,
    *,
    supports_mqtt: bool | None,
) -> str | None:
    """Return the cached BatteryConfig write attempt id for an endpoint family."""

    key = self._battery_config_write_attempt_cache_key(
        endpoint_family,
        supports_mqtt=supports_mqtt,
    )
    return self._battery_config_write_attempt_cache.get(key)


def _battery_config_write_attempts(
    self: EnphaseEVClient,
    endpoint_family: str,
    *,
    write_intent: str,
    supports_mqtt: bool | None,
    params: dict[str, str] | None,
    json_body: dict[str, Any] | list[Any] | None,
) -> list[_BatteryConfigWriteAttempt]:
    """Return ordered write attempts for a BatteryConfig endpoint family."""

    # Captured BatteryConfig traffic differs by region, firmware, and
    # whether the site supports MQTT-backed writes. Keep these variants
    # explicit so a successful shape can be cached without hiding why the
    # fallback exists.
    attempts: list[_BatteryConfigWriteAttempt]
    body = json_body if isinstance(json_body, dict) else None
    has_source = isinstance(params, dict) and "source" in params
    has_devices = isinstance(body, dict) and "devices" in body
    has_disclaimer = (
        isinstance(body, dict)
        and "acceptedItcDisclaimer" in body
        and body.get("acceptedItcDisclaimer") is not True
    )
    has_compat_auth_material = bool(self._battery_config_single_auth_token())
    prefer_cookie_compat = self._battery_config_prefers_cookie_compat()
    has_stateful_base = endpoint_family in self._battery_config_write_bases

    if write_intent == "profile_update":
        attempts = [
            _BatteryConfigWriteAttempt(
                attempt_id="profile_primary",
                auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
            ),
        ]
        if has_devices:
            attempts.append(
                _BatteryConfigWriteAttempt(
                    attempt_id="profile_primary_no_devices",
                    auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
                    strip_devices=True,
                )
            )
        if has_source:
            attempts.append(
                _BatteryConfigWriteAttempt(
                    attempt_id="profile_primary_no_source",
                    auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
                    omit_source=True,
                    strip_devices=has_devices,
                )
            )
        if has_stateful_base:
            attempts.extend(
                [
                    _BatteryConfigWriteAttempt(
                        attempt_id="profile_stateful_primary",
                        auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
                        merged_payload=True,
                        preserve_base_devices=has_devices,
                    ),
                    _BatteryConfigWriteAttempt(
                        attempt_id="profile_stateful_primary_no_source",
                        auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
                        omit_source=has_source,
                        merged_payload=True,
                        preserve_base_devices=has_devices,
                    ),
                ]
            )
        if has_compat_auth_material:
            attempts.append(
                _BatteryConfigWriteAttempt(
                    attempt_id="profile_cookie_eauth_compat",
                    auth_mode=_BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                    omit_source=has_source,
                    strip_devices=has_devices,
                    prefer_existing_xsrf=True,
                )
            )
            if has_stateful_base:
                attempts.append(
                    _BatteryConfigWriteAttempt(
                        attempt_id="profile_stateful_cookie_eauth_compat",
                        auth_mode=_BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                        omit_source=has_source,
                        merged_payload=True,
                        preserve_base_devices=has_devices,
                        prefer_existing_xsrf=True,
                    )
                )
        if has_compat_auth_material:
            attempts.append(
                _BatteryConfigWriteAttempt(
                    attempt_id="profile_mixed_compat",
                    auth_mode=_BATTERY_CONFIG_VARIANT_MIXED,
                    omit_source=has_source,
                    strip_devices=has_devices,
                )
            )
            if has_stateful_base:
                attempts.append(
                    _BatteryConfigWriteAttempt(
                        attempt_id="profile_stateful_mixed_compat",
                        auth_mode=_BATTERY_CONFIG_VARIANT_MIXED,
                        omit_source=has_source,
                        merged_payload=True,
                        preserve_base_devices=has_devices,
                    )
                )
    elif write_intent == "battery_settings_update":
        attempts = [
            _BatteryConfigWriteAttempt(
                attempt_id="battery_settings_primary_source",
                auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
            ),
            _BatteryConfigWriteAttempt(
                attempt_id="battery_settings_lean_source",
                auth_mode=_BATTERY_CONFIG_VARIANT_LEAN,
            ),
        ]
        if supports_mqtt is True and has_source:
            attempts.extend(
                [
                    _BatteryConfigWriteAttempt(
                        attempt_id="battery_settings_primary_no_source",
                        auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
                        omit_source=True,
                    ),
                    _BatteryConfigWriteAttempt(
                        attempt_id="battery_settings_lean_no_source",
                        auth_mode=_BATTERY_CONFIG_VARIANT_LEAN,
                        omit_source=True,
                    ),
                ]
            )
        if has_stateful_base:
            attempts.extend(
                [
                    _BatteryConfigWriteAttempt(
                        attempt_id="battery_settings_stateful_primary_source",
                        auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
                        merged_payload=True,
                    ),
                    _BatteryConfigWriteAttempt(
                        attempt_id="battery_settings_stateful_lean_source",
                        auth_mode=_BATTERY_CONFIG_VARIANT_LEAN,
                        merged_payload=True,
                    ),
                ]
            )
            if supports_mqtt is True and has_source:
                attempts.extend(
                    [
                        _BatteryConfigWriteAttempt(
                            attempt_id=("battery_settings_stateful_primary_no_source"),
                            auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
                            omit_source=True,
                            merged_payload=True,
                        ),
                        _BatteryConfigWriteAttempt(
                            attempt_id="battery_settings_stateful_lean_no_source",
                            auth_mode=_BATTERY_CONFIG_VARIANT_LEAN,
                            omit_source=True,
                            merged_payload=True,
                        ),
                    ]
                )
        if has_compat_auth_material:
            attempts.append(
                _BatteryConfigWriteAttempt(
                    attempt_id="battery_settings_cookie_eauth_source",
                    auth_mode=_BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                    prefer_existing_xsrf=True,
                )
            )
            if supports_mqtt is True and has_source:
                attempts.append(
                    _BatteryConfigWriteAttempt(
                        attempt_id="battery_settings_cookie_eauth_no_source",
                        auth_mode=_BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                        omit_source=True,
                        prefer_existing_xsrf=True,
                    )
                )
            if has_stateful_base:
                attempts.append(
                    _BatteryConfigWriteAttempt(
                        attempt_id="battery_settings_stateful_cookie_eauth_source",
                        auth_mode=_BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                        merged_payload=True,
                        prefer_existing_xsrf=True,
                    )
                )
                if supports_mqtt is True and has_source:
                    attempts.append(
                        _BatteryConfigWriteAttempt(
                            attempt_id=(
                                "battery_settings_stateful_cookie_eauth_no_source"
                            ),
                            auth_mode=_BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                            omit_source=True,
                            merged_payload=True,
                            prefer_existing_xsrf=True,
                        )
                    )
        if has_compat_auth_material:
            attempts.append(
                _BatteryConfigWriteAttempt(
                    attempt_id="battery_settings_mixed_source",
                    auth_mode=_BATTERY_CONFIG_VARIANT_MIXED,
                )
            )
            if supports_mqtt is True and has_source:
                attempts.append(
                    _BatteryConfigWriteAttempt(
                        attempt_id="battery_settings_mixed_no_source",
                        auth_mode=_BATTERY_CONFIG_VARIANT_MIXED,
                        omit_source=True,
                    )
                )
            if has_stateful_base:
                attempts.append(
                    _BatteryConfigWriteAttempt(
                        attempt_id="battery_settings_stateful_mixed_source",
                        auth_mode=_BATTERY_CONFIG_VARIANT_MIXED,
                        merged_payload=True,
                    )
                )
                if supports_mqtt is True and has_source:
                    attempts.append(
                        _BatteryConfigWriteAttempt(
                            attempt_id="battery_settings_stateful_mixed_no_source",
                            auth_mode=_BATTERY_CONFIG_VARIANT_MIXED,
                            omit_source=True,
                            merged_payload=True,
                        )
                    )
            if has_disclaimer:
                attempts.append(
                    _BatteryConfigWriteAttempt(
                        attempt_id="battery_settings_disclaimer_true",
                        auth_mode=_BATTERY_CONFIG_VARIANT_MIXED,
                        omit_source=supports_mqtt is True and has_source,
                        disclaimer_bool_true=True,
                    )
                )
                if has_stateful_base:
                    attempts.append(
                        _BatteryConfigWriteAttempt(
                            attempt_id=("battery_settings_stateful_disclaimer_true"),
                            auth_mode=_BATTERY_CONFIG_VARIANT_MIXED,
                            omit_source=supports_mqtt is True and has_source,
                            disclaimer_bool_true=True,
                            merged_payload=True,
                        )
                    )
    elif write_intent == "battery_settings_disclaimer_accept":
        attempts = [
            _BatteryConfigWriteAttempt(
                attempt_id="battery_settings_disclaimer_primary",
                auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
            ),
            _BatteryConfigWriteAttempt(
                attempt_id="battery_settings_disclaimer_lean",
                auth_mode=_BATTERY_CONFIG_VARIANT_LEAN,
            ),
        ]
        if has_compat_auth_material:
            attempts.append(
                _BatteryConfigWriteAttempt(
                    attempt_id="battery_settings_disclaimer_cookie_eauth",
                    auth_mode=_BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                    prefer_existing_xsrf=True,
                )
            )
        if has_compat_auth_material:
            attempts.append(
                _BatteryConfigWriteAttempt(
                    attempt_id="battery_settings_disclaimer_mixed",
                    auth_mode=_BATTERY_CONFIG_VARIANT_MIXED,
                )
            )
    else:
        attempts = [
            _BatteryConfigWriteAttempt(
                attempt_id=f"{endpoint_family}_primary",
                auth_mode=_BATTERY_CONFIG_VARIANT_PRIMARY,
            ),
            _BatteryConfigWriteAttempt(
                attempt_id=f"{endpoint_family}_lean",
                auth_mode=_BATTERY_CONFIG_VARIANT_LEAN,
            ),
        ]
        if has_compat_auth_material:
            attempts.append(
                _BatteryConfigWriteAttempt(
                    attempt_id=f"{endpoint_family}_cookie_eauth",
                    auth_mode=_BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                    prefer_existing_xsrf=True,
                )
            )
            attempts.append(
                _BatteryConfigWriteAttempt(
                    attempt_id=f"{endpoint_family}_mixed",
                    auth_mode=_BATTERY_CONFIG_VARIANT_MIXED,
                )
            )

    cached_attempt = self._battery_config_cached_write_attempt(
        endpoint_family,
        supports_mqtt=supports_mqtt,
    )
    if cached_attempt:
        attempts = sorted(
            attempts,
            key=lambda attempt: 0 if attempt.attempt_id == cached_attempt else 1,
        )
    elif prefer_cookie_compat:
        attempts = sorted(
            attempts,
            key=lambda attempt: (
                0 if attempt.auth_mode == _BATTERY_CONFIG_VARIANT_COOKIE_EAUTH else 1
            ),
        )
    return attempts


def _battery_config_prefers_cookie_compat(self: EnphaseEVClient) -> bool:
    """Return True when cookie-backed BatteryConfig writes should be preferred.

    Some Enphase battery sites only accept the browser-like request that reuses
    the stored session cookie and its original BP-XSRF token. When that raw
    cookie/XSRF pair is present locally, start with the cookie-backed attempt
    instead of probing the known-bad official-web variants first.
    """

    return bool(
        self._cookie
        and self._battery_config_single_auth_token()
        and self._battery_config_cookie_header_xsrf_token()
    )


def _battery_config_attempt_headers(
    self: EnphaseEVClient,
    attempt: _BatteryConfigWriteAttempt,
    *,
    include_xsrf: bool,
) -> dict[str, str | None]:
    """Return headers for a BatteryConfig write attempt."""

    if attempt.auth_mode == _BATTERY_CONFIG_VARIANT_MIXED:
        return self._battery_config_mixed_auth_headers(include_xsrf=include_xsrf)
    if attempt.auth_mode == _BATTERY_CONFIG_VARIANT_COOKIE_EAUTH:
        return self._battery_config_cookie_eauth_headers(include_xsrf=include_xsrf)
    return self._battery_config_headers(
        include_xsrf=include_xsrf,
        variant=attempt.auth_mode,
    )


def _battery_config_attempt_params(
    self: EnphaseEVClient,
    params: dict[str, str] | None,
    attempt: _BatteryConfigWriteAttempt,
) -> dict[str, str] | None:
    """Return query params for a BatteryConfig write attempt."""

    if not isinstance(params, dict):
        return params
    adjusted = dict(params)
    if attempt.omit_source:
        adjusted.pop("source", None)
    return adjusted


def _battery_config_attempt_json_body(
    self: EnphaseEVClient,
    json_body: dict[str, Any] | list[Any] | None,
    endpoint_family: str,
    attempt: _BatteryConfigWriteAttempt,
) -> dict[str, Any] | list[Any] | None:
    """Return the request payload for a BatteryConfig write attempt."""

    body_for_attempt = json_body
    if (
        attempt.merged_payload
        and attempt.preserve_base_devices
        and isinstance(json_body, dict)
        and "devices" in json_body
    ):
        body_for_attempt = dict(json_body)
        body_for_attempt.pop("devices", None)

    if attempt.merged_payload:
        adjusted = self._battery_config_merged_write_payload(
            endpoint_family,
            body_for_attempt,
        )
    elif isinstance(body_for_attempt, dict):
        adjusted = dict(body_for_attempt)
    elif isinstance(body_for_attempt, list):
        adjusted = list(body_for_attempt)
    else:
        adjusted = body_for_attempt
    if not isinstance(adjusted, dict):
        return adjusted
    if attempt.strip_devices:
        adjusted.pop("devices", None)
    if attempt.disclaimer_bool_true and "acceptedItcDisclaimer" in adjusted:
        adjusted["acceptedItcDisclaimer"] = True
    return adjusted


def _battery_config_attempt_change_summary(
    self: EnphaseEVClient,
    attempt: _BatteryConfigWriteAttempt,
    *,
    params: dict[str, str] | None,
    json_body: dict[str, Any] | list[Any] | None,
) -> dict[str, object]:
    """Return safe debug details describing how an attempt differs from canonical."""

    return {
        "auth_mode": attempt.auth_mode,
        "source": (
            "omitted"
            if attempt.omit_source and isinstance(params, dict) and "source" in params
            else "kept"
        ),
        "source_value": params.get("source") if isinstance(params, dict) else None,
        "devices": (
            "stripped"
            if attempt.strip_devices
            and isinstance(json_body, dict)
            and "devices" in json_body
            else "kept"
        ),
        "payload": "merged" if attempt.merged_payload else "partial",
        "devices_shape": (
            "preserved_from_base"
            if attempt.preserve_base_devices
            and isinstance(json_body, dict)
            and "devices" in json_body
            else "from_request"
        ),
        "disclaimer": (
            "boolean_true"
            if attempt.disclaimer_bool_true
            and isinstance(json_body, dict)
            and "acceptedItcDisclaimer" in json_body
            else "preserved"
        ),
    }


def _battery_config_attempt_signature(
    *,
    attempt: _BatteryConfigWriteAttempt,
    params: dict[str, str] | None,
    json_body: dict[str, Any] | list[Any] | None,
) -> str:
    """Return a stable signature for deduplicating write attempts."""

    return json.dumps(
        {
            "attempt_id": attempt.attempt_id,
            "auth_mode": attempt.auth_mode,
            "omit_source": attempt.omit_source,
            "strip_devices": attempt.strip_devices,
            "disclaimer_bool_true": attempt.disclaimer_bool_true,
            "merged_payload": attempt.merged_payload,
            "preserve_base_devices": attempt.preserve_base_devices,
            "params": params,
            "json_body": json_body,
        },
        sort_keys=True,
        default=str,
    )


def _battery_config_payload_data(payload: object) -> dict[str, Any] | None:
    """Return the nested BatteryConfig data payload when available."""

    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _battery_config_merged_write_payload(
    self: EnphaseEVClient,
    endpoint_family: str,
    json_body: dict[str, Any] | list[Any] | None,
) -> dict[str, Any] | list[Any] | None:
    """Merge a partial write payload onto the last successful read payload."""

    if not isinstance(json_body, dict):
        return json_body

    base_payload = self._battery_config_write_bases.get(endpoint_family)
    if not isinstance(base_payload, dict):
        return dict(json_body)

    merged = dict(base_payload)
    for key, value in json_body.items():
        base_value = merged.get(key)
        if isinstance(value, dict) and isinstance(base_value, dict):
            nested = dict(base_value)
            nested.update(copy.deepcopy(value))
            merged[key] = nested
            continue
        merged[key] = copy.deepcopy(value)
    return merged


def _xsrf_token(self: EnphaseEVClient) -> str | None:
    """Return the XSRF token value.

    Checks the dynamically acquired BP-XSRF-Token first, then falls back
    to extracting from the cookie string.
    """

    if self._bp_xsrf_token:
        return self._bp_xsrf_token

    try:
        parts = [p.strip() for p in (self._cookie or "").split(";")]
    except Exception:  # noqa: BLE001 - defensive parsing
        return None
    for part in parts:
        key, sep, value = part.partition("=")
        if not sep or key.strip().lower() not in _XSRF_COOKIE_NAMES:
            continue
        token = value.strip()
        if token.startswith('"') and token.endswith('"') and len(token) >= 2:
            token = token[1:-1]
        if not token:
            continue
        try:
            return unquote(token)
        except Exception:  # noqa: BLE001 - defensive decoding
            return token
    return None


async def _battery_config_request(
    self: EnphaseEVClient,
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
    """Issue a BatteryConfig request using the observed first-party variants."""

    family = endpoint_family or self._battery_config_endpoint_family(url)
    variants = self._battery_config_variant_order(family)

    try:
        for index, variant in enumerate(variants):
            try:

                async def headers() -> dict[str, str | None]:
                    if bootstrap_xsrf:
                        await self._acquire_xsrf_token(schedule_type, variant=variant)
                    result = self._battery_config_headers(
                        include_xsrf=bootstrap_xsrf,
                        variant=variant,
                    )
                    if json_body is not None:
                        result.setdefault("Content-Type", "application/json")
                    return result

                result = await self._json(
                    method,
                    url,
                    json=json_body,
                    headers=headers,
                    params=params,
                    debug_auth_source=variant,
                )
            except EnphaseLoginWallUnauthorized:
                if index == len(variants) - 1:
                    raise
                _LOGGER.debug(
                    "Retrying BatteryConfig request for %s with %s variant "
                    "after login wall (cached_variant=%s)",
                    _request_label(method, url),
                    variants[index + 1],
                    self._battery_config_cached_variant(family),
                )
                continue
            except aiohttp.ClientResponseError as err:
                if err.status == HTTPStatus.UNAUTHORIZED:
                    raise
                if err.status != HTTPStatus.FORBIDDEN or index == len(variants) - 1:
                    raise
                _LOGGER.debug(
                    "Retrying BatteryConfig %s for %s with %s variant "
                    "(cached_variant=%s)",
                    "write" if bootstrap_xsrf else "request",
                    _request_label(method, url),
                    variants[index + 1],
                    self._battery_config_cached_variant(family),
                )
                continue
            if cache_on_success:
                self._cache_battery_config_variant(family, variant)
            return cast(JsonDict, result)
    finally:
        if bootstrap_xsrf:
            self._bp_xsrf_token = None

    raise aiohttp.ClientError("BatteryConfig request exhausted variants")


async def _battery_config_write_request(
    self: EnphaseEVClient,
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
    """Issue a BatteryConfig write using endpoint-specific compatibility attempts."""

    family = endpoint_family or self._battery_config_endpoint_family(url)
    attempts = self._battery_config_write_attempts(
        family,
        write_intent=write_intent,
        supports_mqtt=supports_mqtt,
        params=params,
        json_body=json_body,
    )
    if partial_payload_only:
        attempts = [attempt for attempt in attempts if not attempt.merged_payload]
    if strip_devices:
        attempts = [replace(attempt, strip_devices=True) for attempt in attempts]
    last_error: aiohttp.ClientResponseError | None = None
    seen_signatures: set[str] = set()

    try:
        for index, attempt in enumerate(attempts):
            attempt_params = self._battery_config_attempt_params(params, attempt)
            attempt_json_body = self._battery_config_attempt_json_body(
                json_body,
                family,
                attempt,
            )
            signature = self._battery_config_attempt_signature(
                attempt=attempt,
                params=attempt_params,
                json_body=attempt_json_body,
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)

            try:

                async def headers() -> dict[str, str | None]:
                    # Some cookie-compatible writes must reuse the XSRF token
                    # captured with the original browser session; refreshing it
                    # first can turn a working request into a 403.
                    if not (
                        attempt.prefer_existing_xsrf
                        and self._battery_config_cookie_header_xsrf_token() is not None
                    ):
                        await self._acquire_xsrf_token(
                            schedule_type,
                            variant=(
                                _BATTERY_CONFIG_VARIANT_PRIMARY
                                if attempt.auth_mode
                                in {
                                    _BATTERY_CONFIG_VARIANT_MIXED,
                                    _BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                                }
                                else attempt.auth_mode
                            ),
                        )
                    result = self._battery_config_attempt_headers(
                        attempt,
                        include_xsrf=True,
                    )
                    if attempt_json_body is not None:
                        result.setdefault("Content-Type", "application/json")
                    return result

                result = await self._json(
                    method,
                    url,
                    json=attempt_json_body,
                    headers=headers,
                    params=attempt_params,
                    use_cookie_header_only=(
                        attempt.auth_mode == _BATTERY_CONFIG_VARIANT_COOKIE_EAUTH
                    ),
                    debug_auth_source=attempt.auth_mode,
                    debug_battery_attempt_id=attempt.attempt_id,
                    debug_battery_attempt_changes=(
                        self._battery_config_attempt_change_summary(
                            attempt,
                            params=params,
                            json_body=json_body,
                        )
                    ),
                )
            except aiohttp.ClientResponseError as err:
                if err.status == HTTPStatus.UNAUTHORIZED:
                    raise
                last_error = err
                retry_profile_without_devices = (
                    write_intent == "profile_update"
                    and err.status == HTTPStatus.BAD_REQUEST
                    and isinstance(attempt_json_body, dict)
                    and "devices" in attempt_json_body
                    and index < len(attempts) - 1
                )
                if retry_profile_without_devices:
                    next_attempt = attempts[index + 1]
                    next_json_body = self._battery_config_attempt_json_body(
                        json_body,
                        family,
                        next_attempt,
                    )
                    next_has_devices = (
                        isinstance(next_json_body, dict) and "devices" in next_json_body
                    )
                    _LOGGER.debug(
                        "Retrying BatteryConfig profile write for %s after "
                        "HTTP 400 with devices (next_attempt=%s, "
                        "next_devices=%s)",
                        _request_label(method, url),
                        next_attempt.attempt_id,
                        "kept" if next_has_devices else "stripped",
                    )
                    continue
                if err.status != HTTPStatus.FORBIDDEN:
                    raise
                if index == len(attempts) - 1:
                    raise
                _LOGGER.debug(
                    "Retrying BatteryConfig write for %s with attempt %s "
                    "(cached_attempt=%s, changes=%s)",
                    _request_label(method, url),
                    attempts[index + 1].attempt_id,
                    self._battery_config_cached_write_attempt(
                        family,
                        supports_mqtt=supports_mqtt,
                    ),
                    self._battery_config_attempt_change_summary(
                        attempts[index + 1],
                        params=params,
                        json_body=json_body,
                    ),
                )
                continue

            self._cache_battery_config_write_attempt(
                family,
                attempt.attempt_id,
                supports_mqtt=supports_mqtt,
            )
            return cast(JsonDict, result)
    finally:
        self._bp_xsrf_token = None

    if last_error is not None:
        raise last_error
    raise aiohttp.ClientError("BatteryConfig request exhausted variants")


def _extract_xsrf_from_response_header(response: object) -> str | None:
    """Return the XSRF token from a response's ``x-csrf-token`` header.

    The Enphase BatteryConfig service emits ``x-csrf-token`` on every
    response; the ``battery-profile-ui.enphaseenergy.com`` web UI relies
    on this as its primary bootstrap mechanism (see PR description for
    HAR evidence).
    """

    headers_get = getattr(getattr(response, "headers", None), "get", None)
    if not callable(headers_get):
        return None
    token = headers_get("x-csrf-token") or headers_get("X-CSRF-Token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _extract_xsrf_from_response_cookies(response: object) -> str | None:
    """Return the XSRF token from Set-Cookie headers or response cookies."""

    header_values: list[str] = []
    headers = getattr(response, "headers", None)
    getall = getattr(headers, "getall", None)
    headers_get = getattr(headers, "get", None)
    if callable(getall):
        header_values = list(getall("Set-Cookie", []))
    elif callable(headers_get):
        header_value = headers_get("Set-Cookie")
        if isinstance(header_value, str) and header_value:
            header_values = [header_value]
    for value in header_values:
        match = re.search(r"(?i)(?:^|;\s*)(?:bp-)?xsrf-token=([^;]+)", value)
        if match:
            try:
                decoded = unquote(match.group(1))
            except Exception:  # noqa: BLE001 - defensive decoding
                decoded = match.group(1)
            if decoded:
                return decoded

    response_cookie_token = _extract_xsrf_token(
        _coerce_cookie_map(getattr(response, "cookies", None))
    )
    if response_cookie_token:
        return response_cookie_token

    return None


async def _acquire_xsrf_token(
    self: EnphaseEVClient,
    schedule_type: str = "cfg",
    *,
    variant: str = _BATTERY_CONFIG_VARIANT_PRIMARY,
) -> str | None:
    """Acquire an XSRF token for BatteryConfig write operations.

    Tries two bootstrap shapes, in order:

    1. **GET** ``siteSettings/{site}?userId={userId}`` and read the
       ``x-csrf-token`` response header. This matches the Enphase web UI
       (``battery-profile-ui.enphaseenergy.com``) and works on EMEA sites
       that do not set a ``BP-XSRF-Token`` cookie.
    2. **POST** ``schedules/isValid`` and read ``Set-Cookie`` /
       ``response.cookies`` — the legacy bootstrap, kept as a fallback
       for sites that still expose the token that way.
    """

    headers = self._battery_config_headers(
        include_xsrf=True,
        variant=variant,
    )
    request_headers = self._merge_request_headers({}, headers)

    def _remember_xsrf(token: str, source: str) -> str:
        self._bp_xsrf_token = token
        _LOGGER.debug("Acquired BP-XSRF-Token from %s", source)
        return token

    try:
        _seed_cookie_jar(self._s, _cookie_map_from_header(self._cookie))

        # Preferred path: GET siteSettings. The response includes
        # ``x-csrf-token`` on success without requiring an XSRF token
        # itself, so this avoids the chicken-and-egg problem when the
        # legacy POST bootstrap is rejected with 403.
        user_id = self._battery_config_user_id_for_token() or ""
        site_settings_url = (
            f"{BASE_URL}/service/batteryConfig/api/v1/siteSettings/" f"{self._site}"
        )
        site_settings_params = {"userId": user_id} if user_id else None
        async with asyncio.timeout(self._timeout):
            async with self._s.request(
                "GET",
                site_settings_url,
                headers=request_headers,
                params=site_settings_params,
            ) as r:
                if r.status < HTTPStatus.BAD_REQUEST:
                    token = self._extract_xsrf_from_response_header(r)
                    if token:
                        return _remember_xsrf(token, "siteSettings response header")
                else:
                    _LOGGER.debug(
                        "BatteryConfig GET bootstrap returned %s for %s; "
                        "falling back to POST isValid",
                        r.status,
                        _request_label("GET", site_settings_url),
                    )

        # Legacy fallback: POST /schedules/isValid and parse Set-Cookie.
        isvalid_url = (
            f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
            f"{self._site}/schedules/isValid"
        )
        isvalid_headers = dict(request_headers)
        isvalid_headers["Content-Type"] = "application/json"
        payload = self._battery_schedule_validation_payload(schedule_type)

        def _remember_session_cookie_xsrf(source: str) -> str | None:
            _cookie_header, cookie_map = _serialize_cookie_jar(
                self._s.cookie_jar,
                (
                    isvalid_url,
                    BASE_URL,
                    ENTREZ_URL,
                    "https://battery-profile-ui.enphaseenergy.com",
                ),
            )
            session_cookie_token = _extract_xsrf_token(cookie_map)
            if session_cookie_token:
                return _remember_xsrf(session_cookie_token, source)
            return None

        async def _bootstrap_site_settings_xsrf() -> str | None:
            site_settings_url = (
                f"{BASE_URL}/service/batteryConfig/api/v1/siteSettings/{self._site}"
            )
            site_settings_headers = self._merge_request_headers(
                {},
                self._battery_config_headers(
                    include_xsrf=False,
                    variant=variant,
                ),
            )
            async with asyncio.timeout(self._timeout):
                async with self._s.request(
                    "GET",
                    site_settings_url,
                    headers=site_settings_headers,
                    params=self._battery_config_params(),
                ) as r:
                    if r.status < HTTPStatus.BAD_REQUEST:
                        if token := _extract_xsrf_token(
                            _coerce_cookie_map(getattr(r, "cookies", None))
                        ):
                            return _remember_xsrf(
                                token, "siteSettings response cookies"
                            )
                        return _remember_session_cookie_xsrf(
                            "siteSettings session cookie jar"
                        )
                    _LOGGER.debug(
                        "BatteryConfig siteSettings XSRF bootstrap returned %s for %s",
                        r.status,
                        _request_label("GET", site_settings_url),
                    )
                    return None

        async with asyncio.timeout(self._timeout):
            async with self._s.request(
                "POST", isvalid_url, json=payload, headers=isvalid_headers
            ) as r:
                if r.status >= HTTPStatus.BAD_REQUEST:
                    if token := _extract_xsrf_token(
                        _coerce_cookie_map(getattr(r, "cookies", None))
                    ):
                        return _remember_xsrf(token, "isValid error response cookies")
                    if token := _remember_session_cookie_xsrf(
                        "isValid error session cookie jar"
                    ):
                        return token
                    if token := await _bootstrap_site_settings_xsrf():
                        return token
                    _LOGGER.debug(
                        "BatteryConfig bootstrap returned %s for %s; "
                        "keeping existing XSRF token",
                        r.status,
                        _request_label("POST", isvalid_url),
                    )
                    return None

                token = self._extract_xsrf_from_response_header(r)
                if token:
                    return _remember_xsrf(token, "isValid response header")
                cookie_token = self._extract_xsrf_from_response_cookies(r)
                if cookie_token:
                    return _remember_xsrf(cookie_token, "isValid Set-Cookie")

                if token := _remember_session_cookie_xsrf("session cookie jar"):
                    return token

                _LOGGER.warning("isValid endpoint did not return BP-XSRF-Token cookie")
                return None
    except Exception:  # noqa: BLE001 - XSRF acquisition is best-effort
        _LOGGER.warning("Failed to acquire XSRF token", exc_info=True)
        return None


async def storm_guard_alert(self: EnphaseEVClient) -> JsonDict:
    """Return Storm Guard alert status for the site.

    GET /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
    """
    url = f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/{self._site}/stormAlert"
    return await self._battery_config_request(
        "GET",
        url,
        endpoint_family="storm_alert",
    )


async def opt_out_storm_alert(
    self: EnphaseEVClient, *, alert_id: str, name: str
) -> JsonDict:
    """Opt out of a specific Storm Guard alert.

    PUT /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
    Body: {
      "stormAlerts": [
        {"id": "<alert_id>", "name": "<alert_name>", "status": "opted-out"}
      ]
    }
    """
    url = f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/{self._site}/stormAlert"
    payload: JsonDict = {
        "stormAlerts": [
            {
                "id": str(alert_id),
                "name": str(name),
                "status": "opted-out",
            }
        ]
    }
    return await self._battery_config_write_request(
        "PUT",
        url,
        json_body=payload,
        endpoint_family="storm_alert",
        write_intent="storm_alert_opt_out",
    )


async def storm_guard_profile(
    self: EnphaseEVClient, *, locale: str | None = None
) -> JsonDict:
    """Return Storm Guard state and EVSE settings for the site.

    GET /service/batteryConfig/api/v1/profile/<site_id>?source=enho&userId=<user_id>&locale=<locale>
    """
    return await self.battery_profile_details(locale=locale)


async def battery_site_settings(self: EnphaseEVClient) -> JsonDict:
    """Return BatteryConfig site settings and feature flags."""

    url = f"{BASE_URL}/service/batteryConfig/api/v1/siteSettings/{self._site}"
    params = self._battery_config_params()
    return await self._battery_config_request("GET", url, params=params)


async def battery_profile_details(
    self: EnphaseEVClient, *, locale: str | None = None
) -> JsonDict:
    """Return BatteryConfig profile details for system + EVSE settings."""

    url = f"{BASE_URL}/service/batteryConfig/api/v1/profile/{self._site}"
    params = self._battery_config_params(include_source=True, locale=locale)
    result = await self._battery_config_request(
        "GET",
        url,
        params=params,
        endpoint_family="profile",
    )
    self._remember_battery_config_capabilities(result)
    self._remember_battery_config_write_base("profile", result)
    return result


async def battery_settings_details(self: EnphaseEVClient) -> JsonDict:
    """Return BatteryConfig battery details for charge-grid and shutdown controls."""

    url = f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{self._site}"
    params = self._battery_config_params(include_source="enlm")
    result = await self._battery_config_request(
        "GET",
        url,
        params=params,
        endpoint_family="battery_settings",
    )
    self._remember_battery_config_capabilities(result)
    self._remember_battery_config_write_base("battery_settings", result)
    return result


async def accept_battery_settings_disclaimer(
    self: EnphaseEVClient, disclaimer_type: str = "itc"
) -> JsonDict:
    """Acknowledge the BatteryConfig charge-from-grid disclaimer."""

    url = (
        f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/"
        f"acceptDisclaimer/{self._site}"
    )
    body = {"disclaimer-type": str(disclaimer_type)}
    return await self._battery_config_write_request(
        "POST",
        url,
        json_body=body,
        params=None,
        endpoint_family="battery_settings_disclaimer",
        write_intent="battery_settings_disclaimer_accept",
    )


async def set_battery_settings(
    self: EnphaseEVClient,
    payload: dict[str, Any],
    *,
    schedule_type: str = "cfg",
) -> JsonDict:
    """Update BatteryConfig battery detail settings using a partial payload."""
    url = f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{self._site}"
    params = self._battery_config_params(include_source=True)
    body = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    return await self._battery_config_write_request(
        "PUT",
        url,
        json_body=body,
        params=params,
        schedule_type=schedule_type,
        endpoint_family="battery_settings",
        write_intent="battery_settings_update",
        supports_mqtt=self._battery_config_supports_mqtt,
    )


async def set_battery_settings_compat(
    self: EnphaseEVClient,
    payload: dict[str, Any],
    *,
    schedule_type: str = "cfg",
    include_source: bool = True,
    merged_payload: bool = False,
    strip_devices: bool = False,
    partial_payload_only: bool = False,
) -> JsonDict:
    """Update battery settings using an explicit compatibility payload shape."""

    url = f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{self._site}"
    params = self._battery_config_params(include_source=include_source)
    body: dict[str, Any] | list[Any] | None = (
        copy.deepcopy(payload) if isinstance(payload, dict) else {}
    )
    if merged_payload:
        body = self._battery_config_merged_write_payload("battery_settings", body)
    if isinstance(body, dict) and strip_devices:
        body.pop("devices", None)
    return await self._battery_config_write_request(
        "PUT",
        url,
        json_body=body,
        params=params,
        schedule_type=schedule_type,
        endpoint_family="battery_settings",
        write_intent="battery_settings_update",
        supports_mqtt=self._battery_config_supports_mqtt,
        strip_devices=strip_devices,
        partial_payload_only=partial_payload_only,
    )


async def set_battery_profile(
    self: EnphaseEVClient,
    *,
    profile: str,
    battery_backup_percentage: int,
    operation_mode_sub_type: str | None = None,
    devices: list[dict[str, Any]] | None = None,
) -> JsonDict:
    """Update the site battery profile and reserve percentage."""
    url = f"{BASE_URL}/service/batteryConfig/api/v1/profile/{self._site}"
    params = self._battery_config_params(include_source=True)
    payload: dict[str, Any] = {
        "profile": str(profile),
        "batteryBackupPercentage": int(battery_backup_percentage),
    }
    if operation_mode_sub_type:
        payload["operationModeSubType"] = str(operation_mode_sub_type)
    if devices:
        payload["devices"] = [item for item in devices if isinstance(item, dict)]
    return await self._battery_config_write_request(
        "PUT",
        url,
        json_body=payload,
        params=params,
        endpoint_family="profile",
        write_intent="profile_update",
        supports_mqtt=self._battery_config_supports_mqtt,
    )


async def cancel_battery_profile_update(self: EnphaseEVClient) -> JsonDict:
    """Cancel a pending site battery profile change."""
    url = f"{BASE_URL}/service/batteryConfig/api/v1/cancel/profile/{self._site}"
    params = self._battery_config_params(include_source=True)
    return await self._battery_config_write_request(
        "PUT",
        url,
        json_body={},
        params=params,
        endpoint_family="profile",
    )


async def set_storm_guard(
    self: EnphaseEVClient, *, enabled: bool, evse_enabled: bool
) -> JsonDict:
    """Toggle Storm Guard and the EVSE charge-to-100% option.

    PUT /service/batteryConfig/api/v1/stormGuard/toggle/<site_id>?userId=<user_id>
    """
    url = f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/toggle/{self._site}"
    params = self._battery_config_params(include_source=True)
    payload: JsonDict = {
        "stormGuardState": "enabled" if enabled else "disabled",
        "evseStormEnabled": bool(evse_enabled),
    }
    return await self._battery_config_write_request(
        "PUT",
        url,
        json_body=payload,
        params=params,
        endpoint_family="profile",
    )


async def battery_schedules(self: EnphaseEVClient) -> JsonDict:
    """Return all battery schedules for the site.

    GET /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules

    Response contains ``cfg``, ``dtg``, and ``rbd`` schedule families,
    each with a ``details`` list of individual schedules.
    """

    url = (
        f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
        f"{self._site}/schedules"
    )
    return await self._battery_config_request(
        "GET",
        url,
        endpoint_family="schedules",
    )


async def create_battery_schedule(
    self: EnphaseEVClient,
    *,
    schedule_type: str,
    start_time: str,
    end_time: str,
    limit: int | None,
    days: list[int],
    timezone: str = "UTC",
    is_enabled: bool | None = None,
) -> JsonDict:
    """Create a new battery schedule.

    POST /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules

    Parameters:
        schedule_type: ``CFG`` (charge from grid), ``DTG`` (discharge to grid),
                       or ``RBD`` (restrict battery discharge).
        start_time: ``HH:MM`` format.
        end_time: ``HH:MM`` format.
        limit: Target SoC percentage (0-100).
        days: List of weekday numbers (1=Mon … 7=Sun).
        timezone: IANA timezone string.
    """

    url = (
        f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
        f"{self._site}/schedules"
    )
    payload: JsonDict = {
        "timezone": timezone,
        "startTime": start_time[:5],
        "endTime": end_time[:5],
        "scheduleType": str(schedule_type).upper(),
        "days": [int(d) for d in days],
    }
    if limit is not None:
        payload["limit"] = int(limit)
    if is_enabled is not None:
        payload["isEnabled"] = bool(is_enabled)
    return await self._battery_config_write_request(
        "POST",
        url,
        json_body=payload,
        schedule_type=schedule_type,
        endpoint_family="schedules",
    )


async def update_battery_schedule(
    self: EnphaseEVClient,
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
    """Update an existing battery schedule in-place.

    PUT /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules/{id}

    Parameters:
        schedule_id: The UUID of the schedule to update.
        schedule_type: ``CFG`` (charge from grid), ``DTG`` (discharge to grid),
                       or ``RBD`` (restrict battery discharge).
        start_time: ``HH:MM`` format.
        end_time: ``HH:MM`` format.
        limit: Target SoC percentage (0-100).
        days: List of weekday numbers (1=Mon … 7=Sun).
        timezone: IANA timezone string.
    """

    url = (
        f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
        f"{self._site}/schedules/{schedule_id}"
    )
    payload: JsonDict = {
        "timezone": timezone,
        "startTime": start_time[:5],
        "endTime": end_time[:5],
        "scheduleType": str(schedule_type).upper(),
        "days": [int(d) for d in days],
    }
    if limit is not None:
        payload["limit"] = int(limit)
    if is_enabled is not None:
        payload["isEnabled"] = bool(is_enabled)
    if is_deleted is not None:
        payload["isDeleted"] = bool(is_deleted)
    return await self._battery_config_write_request(
        "PUT",
        url,
        json_body=payload,
        schedule_type=schedule_type,
        endpoint_family="schedules",
    )


async def delete_battery_schedule(
    self: EnphaseEVClient,
    schedule_id: str | int,
    *,
    schedule_type: str = "cfg",
) -> JsonDict:
    """Delete a battery schedule by ID.

    POST /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules/{id}/delete
    """

    url = (
        f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
        f"{self._site}/schedules/{schedule_id}/delete"
    )
    return await self._battery_config_write_request(
        "POST",
        url,
        json_body={},
        schedule_type=schedule_type,
        endpoint_family="schedules",
    )


async def validate_battery_schedule(
    self: EnphaseEVClient, schedule_type: str = "cfg"
) -> JsonDict:
    """Validate a battery schedule configuration.

    POST /service/batteryConfig/api/v1/battery/sites/{site_id}/schedules/isValid

    Acquires a fresh XSRF token before validation because affected
    BatteryConfig sites reject this endpoint without ``X-XSRF-Token``.
    """

    url = (
        f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/"
        f"{self._site}/schedules/isValid"
    )
    payload = self._battery_schedule_validation_payload(schedule_type)
    return await self._battery_config_request(
        "POST",
        url,
        json_body=payload,
        schedule_type=schedule_type,
        endpoint_family="schedules",
        bootstrap_xsrf=True,
    )
