"""Client and authentication helpers for Enphase Enlighten cloud endpoints.

This module intentionally keeps the HTTP boundary in one place. Enphase exposes
several browser-backed services with different header, cookie, token, and XSRF
expectations, so the client normalizes those variants before coordinator and
entity code consume the data.
"""

from __future__ import annotations

import asyncio
import base64
from builtins import ExceptionGroup
import copy
import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from http import HTTPStatus
from urllib.parse import unquote
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

import aiohttp
from yarl import URL

from .const import (
    AUTH_APP_SETTING,
    AUTH_RFID_SETTING,
    BASE_URL,
    DEFAULT_CHARGE_LEVEL_SETTING,
    DEFAULT_AUTH_TIMEOUT,
    ENTREZ_URL,
    GREEN_BATTERY_SETTING,
    LOGIN_FORM_URL,
    LOGIN_URL,
    MFA_RESEND_URL,
    MFA_VALIDATE_URL,
    SITE_SEARCH_URL,
)
from . import api_parsers
from .api_models import AuthTokens, ChargerInfo, SiteInfo, TextResponse
from .log_redaction import redact_identifier, redact_site_id, redact_text

_LOGGER = logging.getLogger(__name__)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
_DEBUG_KV_RE = re.compile(
    r"(?P<key>[A-Za-z][A-Za-z0-9_\-]*)(?P<sep>\s*[=:]\s*)(?P<value>[^,\s)]+)"
)
_XSRF_COOKIE_NAMES = ("xsrf-token", "bp-xsrf-token")
_ENLIGHTEN_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.3.1 Safari/605.1.15"
)
_BATTERY_CONFIG_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
_BATTERY_CONFIG_VARIANT_PRIMARY = "official_web_primary"
_BATTERY_CONFIG_VARIANT_LEAN = "official_web_lean"
_BATTERY_CONFIG_VARIANT_COOKIE_EAUTH = "cookie_eauth_compatible"
_BATTERY_CONFIG_VARIANT_MIXED = "mixed_auth_compatible"
_ENLIGHTEN_READ_CONCURRENCY_LIMIT = 2
OCPP_TRIGGER_MESSAGES = frozenset(
    {
        "BootNotification",
        "DiagnosticsStatusNotification",
        "FirmwareStatusNotification",
        "Heartbeat",
        "MeterValues",
        "StatusNotification",
    }
)
OCPP_TRIGGER_MESSAGES_REQUIRING_CONFIRMATION = frozenset(
    {
        "BootNotification",
        "DiagnosticsStatusNotification",
        "FirmwareStatusNotification",
    }
)
_OCPP_TRIGGER_MESSAGE_MAX_LENGTH = 64
_OCPP_TRIGGER_MESSAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
# Enlighten web pages and XHR endpoints share service capacity with the mobile
# app. A module-level limiter keeps parallel refresh helpers from creating a
# burst of browser-like reads during one Home Assistant update cycle.
_enlighten_read_semaphore: asyncio.Semaphore | None = None


@dataclass(frozen=True)
class _BatteryConfigWriteAttempt:
    """Describe one BatteryConfig write attempt shape."""

    attempt_id: str
    auth_mode: str
    omit_source: bool = False
    strip_devices: bool = False
    disclaimer_bool_true: bool = False
    merged_payload: bool = False
    preserve_base_devices: bool = False
    prefer_existing_xsrf: bool = False


class Unauthorized(Exception):
    pass


class EnphaseLoginWallUnauthorized(Unauthorized):
    """Raised when Enlighten serves the browser login wall to API requests."""

    def __init__(
        self,
        *,
        endpoint: str | None,
        request_label: str,
        status: int | None = None,
        content_type: str | None = None,
        body_preview_redacted: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.request_label = request_label
        self.status = status
        self.content_type = content_type
        self.body_preview_redacted = body_preview_redacted
        detail_parts: list[str] = []
        if endpoint:
            detail_parts.append(f"endpoint={endpoint}")
        if status is not None:
            detail_parts.append(f"status={status}")
        if content_type:
            detail_parts.append(f"content_type={content_type}")
        detail = ", ".join(detail_parts)
        message = "Enphase login wall returned HTML for API request"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


class EnlightenAuthError(Exception):
    """Base exception for Enlighten authentication failures."""


class EnlightenAuthInvalidCredentials(EnlightenAuthError):
    """Raised when credentials are rejected."""


class EnlightenAuthTooManySessions(EnlightenAuthError):
    """Raised when Enlighten rejects login because the account session quota is full."""


class EnlightenAuthMFARequired(EnlightenAuthError):
    """Raised when the API signals multi-factor authentication is required."""

    def __init__(
        self,
        message: str = "Account requires multi-factor authentication",
        tokens: AuthTokens | None = None,
    ) -> None:
        super().__init__(message)
        self.tokens = tokens


class EnlightenAuthInvalidOTP(EnlightenAuthError):
    """Raised when the MFA one-time code is invalid or expired."""


class EnlightenAuthOTPBlocked(EnlightenAuthError):
    """Raised when the MFA flow is blocked."""


class EnlightenAuthUnavailable(EnlightenAuthError):
    """Raised when the service is temporarily unavailable."""


class EnlightenTokenUnavailable(EnlightenAuthError):
    """Raised when a bearer token cannot be obtained for the account."""


class SchedulerUnavailable(Exception):
    """Raised when the scheduler service is unavailable."""


class SessionHistoryUnavailable(Exception):
    """Raised when the session history service is unavailable."""


class SiteEnergyUnavailable(Exception):
    """Raised when the site energy service is unavailable."""


class EVSETimeseriesUnavailable(Exception):
    """Raised when the EVSE timeseries service is unavailable."""


class AuthSettingsUnavailable(Exception):
    """Raised when the charger auth settings service is unavailable."""


class ChargerConfigUnavailable(Exception):
    """Raised when the charger config service is unavailable."""


class OptionalEndpointUnavailable(Exception):
    """Raised when an optional endpoint is unavailable but diagnostically useful."""


@dataclass(slots=True, frozen=True)
class PayloadFailureSignature:
    """Structured metadata describing an invalid payload response."""

    endpoint: str | None = None
    status: int | None = None
    content_type: str | None = None
    failure_kind: str | None = None
    decode_error: str | None = None
    body_length: int | None = None
    body_sha256: str | None = None
    body_preview_redacted: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a diagnostics-safe dictionary representation."""

        return {
            "endpoint": self.endpoint,
            "status": self.status,
            "content_type": self.content_type,
            "failure_kind": self.failure_kind,
            "decode_error": self.decode_error,
            "body_length": self.body_length,
            "body_sha256": self.body_sha256,
            "body_preview_redacted": self.body_preview_redacted,
        }

    def summary(self) -> str:
        """Return a compact human-readable summary."""

        if self.failure_kind == "shape":
            label = "Invalid payload shape"
        else:
            label = "Invalid JSON response"
        detail_parts: list[str] = []
        if self.status is not None:
            detail_parts.append(f"status={self.status}")
        if self.content_type:
            detail_parts.append(f"content_type={self.content_type}")
        if self.endpoint:
            detail_parts.append(f"endpoint={self.endpoint}")
        if self.failure_kind:
            detail_parts.append(f"failure_kind={self.failure_kind}")
        if self.decode_error:
            detail_parts.append(f"decode_error={self.decode_error}")
        if not detail_parts:
            return label
        return f"{label} ({', '.join(detail_parts)})"


class InvalidPayloadError(aiohttp.ClientError):
    """Raised when an endpoint returns malformed or non-JSON payload data."""

    def __init__(
        self,
        summary: str,
        *,
        status: int | None = None,
        content_type: str | None = None,
        endpoint: str | None = None,
        failure_kind: str | None = None,
        decode_error: str | None = None,
        body_length: int | None = None,
        body_sha256: str | None = None,
        body_preview_redacted: str | None = None,
    ) -> None:
        self.signature = PayloadFailureSignature(
            endpoint=endpoint,
            status=status,
            content_type=content_type,
            failure_kind=failure_kind,
            decode_error=decode_error,
            body_length=body_length,
            body_sha256=body_sha256,
            body_preview_redacted=body_preview_redacted,
        )
        compact = " ".join(str(summary or "").split()).strip()
        if not compact:
            compact = (
                self.signature.summary()
                or "Invalid JSON response from Enphase endpoint"
            )
        if len(compact) > 256:
            compact = f"{compact[:256]}…"
        self.summary = compact
        self.status = status
        self.content_type = content_type
        self.endpoint = endpoint
        self.failure_kind = failure_kind
        self.decode_error = decode_error
        self.body_length = body_length
        self.body_sha256 = body_sha256
        self.body_preview_redacted = body_preview_redacted
        super().__init__(self.summary)

    def signature_dict(self) -> dict[str, object]:
        """Return the structured payload signature as a dictionary."""

        return self.signature.to_dict()


def _is_optional_non_json_payload(err: InvalidPayloadError) -> bool:
    """Return True when an optional endpoint returned a non-JSON success page."""

    try:
        status = int(err.status or 0)
    except Exception:
        status = 0
    if status < 200 or status >= 300:
        return False
    content_type = str(err.content_type or "").lower()
    return "json" not in content_type


def _is_optional_html_payload(err: InvalidPayloadError) -> bool:
    """Return True when an optional endpoint returned HTML disguised as JSON."""

    try:
        status = int(err.status or 0)
    except Exception:
        status = 0
    if status < 200 or status >= 300:
        return False
    preview = str(err.body_preview_redacted or "").lower()
    return "<!doctype html" in preview or "<html" in preview


def _truncate_preview(text: str, *, max_length: int = 256) -> str:
    """Return a compact payload preview capped to the requested size."""

    compact = " ".join(str(text or "").split()).strip()
    if len(compact) > max_length:
        return f"{compact[:max_length]}..."
    return compact


def _safe_response_error_message(
    *,
    status: int,
    reason: str | None,
    headers: object,
    body_text: str | None,
) -> str:
    """Return a structured response error summary without raw response bodies."""

    detail_parts = [f"status={status}"]
    safe_reason = redact_text(reason or "", max_length=80)
    if safe_reason:
        detail_parts.append(f"reason={safe_reason}")
    content_type = ""
    try:
        header_get = getattr(headers, "get", None)
        if callable(header_get):
            content_type = str(header_get("Content-Type", "")).strip()
    except Exception:  # noqa: BLE001 - defensive header parsing
        content_type = ""
    if content_type:
        detail_parts.append(f"content_type={content_type}")
    if body_text is not None:
        detail_parts.append(f"body_length={len(body_text)}")
    return f"HTTP error from Enphase endpoint ({', '.join(detail_parts)})"


def _scheduler_error_context_from_text(
    text: str | None,
) -> tuple[str | None, str | None]:
    """Return a scheduler error code/display tuple from a response body."""

    if not text:
        return None, None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("errorMessageCode")
    display = error.get("displayMessage") or error.get("additionalInfo")
    return (str(code) if code else None, str(display) if display else None)


def _scheduler_error_context(
    err: aiohttp.ClientResponseError,
) -> tuple[str | None, str | None]:
    """Return scheduler error context attached to a response error."""

    context = getattr(err, "enphase_scheduler_error", None)
    if isinstance(context, dict):
        code = context.get("code")
        display = context.get("display")
        return (str(code) if code else None, str(display) if display else None)
    return _scheduler_error_context_from_text(err.message)


def _scheduler_error_code(err: aiohttp.ClientResponseError) -> str | None:
    """Return a scheduler error code from a response error when available."""

    return _scheduler_error_context(err)[0]


def _is_scheduler_charging_mode_endpoint(endpoint: str | None) -> bool:
    """Return True for IQ EV charger scheduler mode endpoints."""

    return "/service/evse_scheduler/api/v1/iqevc/charging-mode/" in str(endpoint or "")


def validate_ocpp_trigger_message(requested_message: object) -> str:
    """Return a supported OCPP trigger message name or raise ValueError."""

    raw_message = str(requested_message or "")
    message = raw_message.strip()
    if (
        not message
        or raw_message != message
        or len(message) > _OCPP_TRIGGER_MESSAGE_MAX_LENGTH
        or _OCPP_TRIGGER_MESSAGE_RE.fullmatch(message) is None
        or message not in OCPP_TRIGGER_MESSAGES
    ):
        allowed = ", ".join(sorted(OCPP_TRIGGER_MESSAGES))
        raise ValueError("Unsupported OCPP trigger message. " f"Use one of: {allowed}.")
    return message


def _is_enphase_login_wall(
    *,
    endpoint: str | None,
    payload: object,
) -> bool:
    """Return True when a JSON API request received the Enlighten browser login wall."""

    endpoint_text = str(endpoint or "").strip()
    if not endpoint_text.startswith(("/service/", "/app-api/", "/systems/", "/pv/")):
        return False
    try:
        body = str(payload or "")
    except Exception:  # noqa: BLE001
        return False
    preview = body.lower()
    if "<!doctype html" not in preview and "<html" not in preview:
        return False
    markers = (
        "window.optanonwrapper",
        "var otlang",
        "x-ua-compatible",
        "enphaseenergy.com",
        "/login/login",
        "one trust",
    )
    return any(marker in preview for marker in markers)


def _is_hems_invalid_site_error(err: aiohttp.ClientResponseError) -> bool:
    """Return True when HEMS reports the site is unsupported for HEMS endpoints."""

    try:
        if int(err.status or 0) != 550:
            return False
    except Exception:
        return False

    message = str(err.message or "").strip()
    if not message:
        return False
    try:
        payload = json.loads(message)
    except Exception:
        text = message.lower()
        return "invalid_site" in text or "not a valid hems site" in text
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if isinstance(error, dict):
        status = str(error.get("status") or "").strip().upper()
        code = error.get("code")
        message_text = str(error.get("message") or "").strip().lower()
        if status == "INVALID_SITE":
            return True
        if str(code).strip() == "900" and "valid hems site" in message_text:
            return True
    return False


def _redact_debug_json_body(
    payload: Any,
    *,
    site_ids: Iterable[object] | None = None,
) -> Any:
    """Return a JSON-safe payload with common identifiers redacted."""

    # Invalid payload previews are copied into diagnostics and repair context,
    # so redact before truncating to avoid exposing short tokens or site IDs.
    normalized_site_ids: set[str] = set()
    for site_id in site_ids or ():
        try:
            site_text = str(site_id).strip()
        except Exception:  # noqa: BLE001
            continue
        if site_text:
            normalized_site_ids.add(site_text)

    def _sanitize(key: str | None, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for child_key, child_value in value.items():
                try:
                    child_key_text = str(child_key)
                except Exception:  # noqa: BLE001
                    child_key_text = "key"
                sanitized[child_key_text] = _sanitize(child_key_text, child_value)
            return sanitized
        if isinstance(value, list):
            return [_sanitize(key, item) for item in value]
        if isinstance(value, str):
            compact_key = "".join(ch for ch in str(key or "").lower() if ch.isalnum())
            text = value.strip()
            if not text:
                return value
            if (
                compact_key in {"site", "siteid", "sitename"}
                or text in normalized_site_ids
            ):
                return "[site]"
            if any(
                token in compact_key
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
                return "[redacted]"
            if (
                "serial" in compact_key
                or "uid" in compact_key
                or compact_key.endswith("id")
            ):
                return redact_identifier(text)
            return redact_text(text, site_ids=site_ids, max_length=256)
        return value

    return _sanitize(None, payload)


def _payload_preview_and_hash(
    payload: object,
    *,
    site_ids: Iterable[object] | None = None,
    max_preview: int = 256,
) -> tuple[int | None, str | None, str | None]:
    """Return diagnostics-safe payload length, digest, and preview."""

    if payload is None:
        return None, None, None

    raw_text = ""
    preview = ""
    if isinstance(payload, bytes):
        raw_bytes = payload
        raw_text = payload.decode("utf-8", errors="replace")
    elif isinstance(payload, str):
        raw_text = payload
        raw_bytes = raw_text.encode("utf-8", errors="replace")
    else:
        try:
            raw_text = json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                default=str,
                sort_keys=True,
            )
        except Exception:  # noqa: BLE001
            raw_text = str(payload)
        raw_bytes = raw_text.encode("utf-8", errors="replace")

    try:
        parsed_payload = json.loads(raw_text)
    except Exception:
        preview = redact_text(raw_text, site_ids=site_ids, max_length=max_preview)
    else:
        try:
            preview = json.dumps(
                _redact_debug_json_body(parsed_payload, site_ids=site_ids),
                ensure_ascii=True,
                separators=(",", ":"),
                default=str,
            )
        except Exception:  # noqa: BLE001
            preview = redact_text(raw_text, site_ids=site_ids, max_length=max_preview)

    preview = _truncate_preview(preview, max_length=max_preview) if preview else ""
    body_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return len(raw_bytes), body_sha256, preview or None


_SYSTEM_DASHBOARD_DETAIL_QUERY_MAP: dict[str, str] = {
    "envoy": "envoys",
    "envoys": "envoys",
    "meter": "meters",
    "meters": "meters",
    "enpower": "enpowers",
    "enpowers": "enpowers",
    "encharge": "encharges",
    "encharges": "encharges",
    "modem": "modems",
    "modems": "modems",
    "microinverter": "inverters",
    "inverters": "inverters",
}


def _system_dashboard_query_type(type_key: object) -> str | None:
    """Normalize a dashboard query type to the observed endpoint value."""

    if type_key is None:
        return None
    try:
        text = str(type_key).strip().lower()
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    normalized = "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")
    if not normalized:
        return None
    return _SYSTEM_DASHBOARD_DETAIL_QUERY_MAP.get(normalized)


def _request_label(method: object, url: object) -> str:
    """Return a compact request label for debug logging."""

    try:
        method_text = str(method).strip().upper()
    except Exception:  # noqa: BLE001 - defensive casting
        method_text = "REQUEST"

    path = ""
    try:
        url_obj = url if isinstance(url, URL) else URL(str(url))
    except Exception:  # noqa: BLE001 - fallback to raw text
        try:
            raw = str(url).strip()
        except Exception:  # noqa: BLE001
            raw = ""
        if raw:
            return f"{method_text} {raw}"
        return method_text

    if url_obj.path:
        path = url_obj.path
    if url_obj.query_string:
        path = f"{path}?{url_obj.query_string}" if path else f"?{url_obj.query_string}"
    if path:
        return f"{method_text} {path}"
    return method_text


def _serialize_cookie_jar(
    jar: aiohttp.CookieJar, urls: Iterable[str | URL]
) -> tuple[str, dict[str, str]]:
    """Return a Cookie header string and mapping extracted from the jar."""

    cookies: dict[str, str] = {}
    for url in urls:
        try:
            url_obj = url if isinstance(url, URL) else URL(str(url))
        except Exception:  # noqa: BLE001 - defensive casting
            continue
        try:
            filtered = jar.filter_cookies(url_obj)
        except Exception:  # noqa: BLE001 - defensive: filter_cookies may raise
            continue
        for key, morsel in filtered.items():
            cookies[key] = morsel.value
    header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return header, cookies


def _cookie_header_from_map(cookies: dict[str, str] | None) -> str:
    """Return a Cookie header string from a raw cookie map."""

    if not cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _decode_jwt_exp(token: str) -> int | None:
    """Decode the exp claim from a JWT-like token without validation."""

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001 - defensive parsing
        return None
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return int(exp)
    return None


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without validation."""

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001 - defensive parsing
        return None
    return payload if isinstance(payload, dict) else None


def _jwt_user_id(token: str | None) -> str | None:
    """Extract user_id from a JWT payload when available."""

    if not token:
        return None
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    for key in ("user_id", "userId", "userid"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("user_id", "userId", "userid"):
            value = data.get(key)
            if value is not None:
                return str(value)
    return None


def _jwt_session_id(token: str | None) -> str | None:
    """Extract session_id from a JWT payload when available."""

    if not token:
        return None
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    for key in ("session_id", "sessionId", "session"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("session_id", "sessionId", "session"):
            value = data.get(key)
            if value is not None:
                return str(value)
    return None


def _extract_xsrf_token(cookies: dict[str, str] | None) -> str | None:
    """Return the XSRF token value from the cookie jar map."""

    if not cookies:
        return None
    for preferred in _XSRF_COOKIE_NAMES:
        for name, value in cookies.items():
            if name and name.lower() == preferred:
                try:
                    token = str(value).strip()
                except Exception:  # noqa: BLE001 - defensive parsing
                    continue
                if token.startswith('"') and token.endswith('"') and len(token) >= 2:
                    token = token[1:-1]
                if not token:
                    continue
                try:
                    return unquote(token)
                except Exception:  # noqa: BLE001 - defensive decoding
                    return token
    return None


def _coerce_cookie_map(cookies: object) -> dict[str, str]:
    """Normalize cookie containers to a simple string mapping."""

    items = getattr(cookies, "items", None)
    if not callable(items):
        return {}

    normalized: dict[str, str] = {}
    try:
        cookie_items = list(items())
    except Exception:  # noqa: BLE001 - defensive cookie parsing
        return normalized

    for name, morsel in cookie_items:
        try:
            cookie_name = str(name).strip()
        except Exception:  # noqa: BLE001 - defensive parsing
            continue
        if not cookie_name:
            continue
        raw_value = getattr(morsel, "value", morsel)
        try:
            normalized[cookie_name] = str(raw_value).strip()
        except Exception:  # noqa: BLE001 - defensive parsing
            continue
    return normalized


def _cookie_map_from_header(cookie_header: object) -> dict[str, str]:
    """Parse a Cookie header string into a simple mapping."""

    try:
        text = str(cookie_header or "")
    except Exception:  # noqa: BLE001 - defensive cookie parsing
        return {}

    if not text:
        return {}

    cookies: dict[str, str] = {}
    for part in text.split(";"):
        item = part.strip()
        if not item:
            continue
        name, sep, value = item.partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        cookies[name] = value.strip()
    return cookies


def _cookie_names_from_header(cookie_header: object) -> list[str]:
    """Return sorted cookie names parsed from a Cookie header string."""

    return sorted(_cookie_map_from_header(cookie_header).keys())


def _authorization_bearer_token(headers: dict[str, str]) -> str | None:
    """Return the bearer token from an Authorization header when present."""

    raw = headers.get("Authorization")
    if not isinstance(raw, str):
        return None
    prefix = "Bearer "
    if not raw.startswith(prefix):
        return None
    token = raw[len(prefix) :].strip()
    return token or None


def _request_failure_debug_family(method: object, path_or_url: object) -> str | None:
    """Return the debug-log label for curated opaque request failures."""

    try:
        method_text = str(method).strip().upper()
    except Exception:  # noqa: BLE001 - defensive casting
        method_text = ""
    try:
        target = str(path_or_url).strip()
    except Exception:  # noqa: BLE001 - defensive casting
        target = ""

    if method_text in {"PUT", "POST"} and "/service/batteryConfig/api/v1/" in target:
        return "BatteryConfig write"
    if method_text in {"PUT", "POST", "PATCH"} and (
        "/service/evse_controller/" in target
        or "/service/evse_scheduler/api/v1/" in target
    ):
        return "EVSE control write"
    if (
        method_text in {"GET", "POST"}
        and "grid_toggle_otp.json" in target
        or method_text == "POST"
        and (
            "/pv/settings/grid_state.json" in target
            or "/pv/settings/log_grid_change.json" in target
        )
    ):
        return "Grid control toggle"
    return None


def _should_limit_enlighten_read_request(method: object, url: object) -> bool:
    """Return True when the request should use the shared Enlighten read limiter."""

    try:
        method_text = str(method).strip().upper()
    except Exception:  # noqa: BLE001 - defensive casting
        return False
    if method_text not in {"GET", "HEAD"}:
        return False
    try:
        url_text = str(url).strip()
    except Exception:  # noqa: BLE001 - defensive casting
        return False
    return url_text.startswith(f"{BASE_URL}/")


def _get_enlighten_read_semaphore() -> asyncio.Semaphore:
    """Return the shared semaphore used to limit concurrent Enlighten reads."""

    global _enlighten_read_semaphore
    if _enlighten_read_semaphore is None:
        _enlighten_read_semaphore = asyncio.Semaphore(_ENLIGHTEN_READ_CONCURRENCY_LIMIT)
    return _enlighten_read_semaphore


@asynccontextmanager
async def _enlighten_read_request_guard(method: object, url: object):
    """Limit concurrent GET/HEAD requests to the Enlighten web host."""

    if not _should_limit_enlighten_read_request(method, url):
        yield
        return
    async with _get_enlighten_read_semaphore():
        yield


def _seed_cookie_jar(session: aiohttp.ClientSession, cookies: dict[str, str]) -> None:
    """Ensure the session cookie jar contains the supplied cookies."""

    jar = getattr(session, "cookie_jar", None)
    if jar is None or not cookies:
        return
    try:
        jar.update_cookies(cookies, response_url=URL(BASE_URL))
    except Exception:  # noqa: BLE001 - best-effort for config flow cookie handling
        return


def _extract_login_session(payload: Any) -> tuple[str | None, str | None]:
    """Extract session id and manager token from login responses."""

    if not isinstance(payload, dict):
        return None, None
    session_id = (
        payload.get("session_id") or payload.get("sessionId") or payload.get("session")
    )
    manager_token = payload.get("manager_token") or payload.get("managerToken")
    return (
        str(session_id) if session_id else None,
        str(manager_token) if manager_token else None,
    )


def _is_too_many_active_sessions_response(payload: Any) -> bool:
    """Return True when an Enlighten auth response reports session exhaustion."""

    def _contains_session_limit(value: Any) -> bool:
        if isinstance(value, dict):
            return any(_contains_session_limit(item) for item in value.values())
        if isinstance(value, list):
            return any(_contains_session_limit(item) for item in value)
        if not isinstance(value, str):
            return False
        text = value.strip().lower()
        if not text:
            return False
        if "too many active sessions" in text:
            return True
        if "active sessions" in text and "too many" in text:
            return True
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                return False
            return _contains_session_limit(parsed)
        return False

    return _contains_session_limit(payload)


def is_scheduler_unavailable_error(
    message: str | None,
    status: int | None = None,
    url: str | URL | None = None,
) -> bool:
    """Return True if the error payload indicates scheduler unavailability."""

    try:
        text = str(message or "").lower()
    except Exception:
        text = ""
    url_text = ""
    if url:
        try:
            url_text = str(url).lower()
        except Exception:
            url_text = ""

    scheduler_tokens = ("iqevc-scheduler", "scheduler ms", "evse_scheduler")
    status_tokens = (500, 502, 503, 504)
    if url_text and "/evse_scheduler/" in url_text and status in status_tokens:
        return True
    if any(token in text for token in scheduler_tokens):
        if (
            status in status_tokens
            or "service unavailable" in text
            or "refused" in text
        ):
            return True
        if "unavailable" in text:
            return True
    if "scheduler" in text and (
        "service unavailable" in text or "refused" in text or "unavailable" in text
    ):
        return True
    if "schedules/status" in text and "service unavailable" in text:
        return True
    return False


def is_session_history_unavailable_error(
    message: str | None,
    status: int | None = None,
    url: str | URL | None = None,
) -> bool:
    """Return True if the error payload indicates session history unavailability."""
    try:
        text = str(message or "").lower()
    except Exception:
        text = ""
    url_text = ""
    if url:
        try:
            url_text = str(url).lower()
        except Exception:
            url_text = ""
    if (
        url_text
        and "/enho_historical_events_ms/" in url_text
        and status
        in (
            500,
            502,
            503,
            504,
            550,
        )
    ):
        return True
    if "historical_events" in text and "service unavailable" in text:
        return True
    if "session history" in text and "unavailable" in text:
        return True
    return False


def is_site_energy_unavailable_error(
    message: str | None,
    status: int | None = None,
    url: str | URL | None = None,
) -> bool:
    """Return True if the error payload indicates site energy unavailability."""
    try:
        text = str(message or "").lower()
    except Exception:
        text = ""
    url_text = ""
    if url:
        try:
            url_text = str(url).lower()
        except Exception:
            url_text = ""
    if url_text and "/pv/systems/" in url_text and "lifetime_energy" in url_text:
        if status in (500, 502, 503, 504):
            return True
    if "lifetime_energy" in text and "service unavailable" in text:
        return True
    return False


def is_evse_timeseries_unavailable_error(
    message: str | None,
    status: int | None = None,
    url: str | URL | None = None,
) -> bool:
    """Return True if the error payload indicates EVSE timeseries unavailability."""

    try:
        text = str(message or "").lower()
    except Exception:
        text = ""
    url_text = ""
    if url:
        try:
            url_text = str(url).lower()
        except Exception:
            url_text = ""
    if (
        url_text
        and "/service/timeseries/evse/timeseries/" in url_text
        and status in (500, 502, 503, 504)
    ):
        return True
    if "evse" in text and "timeseries" in text and "unavailable" in text:
        return True
    if "daily_energy" in text and "service unavailable" in text:
        return True
    if "lifetime_energy" in text and "service unavailable" in text:
        return True
    return False


def is_auth_settings_unavailable_error(
    message: str | None,
    status: int | None = None,
    url: str | URL | None = None,
) -> bool:
    """Return True if the error payload indicates auth settings unavailability."""
    try:
        text = str(message or "").lower()
    except Exception:
        text = ""
    url_text = ""
    if url:
        try:
            url_text = str(url).lower()
        except Exception:
            url_text = ""
    if (
        url_text
        and "/evse_controller/api/v1/" in url_text
        and "ev_charger_config" in url_text
    ):
        if status in (500, 502, 503, 504):
            return True
    if "ev_charger_config" in text and "service unavailable" in text:
        return True
    return False


async def _request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    timeout: int,
    headers: dict[str, str] | None = None,
    data: Any | None = None,
    json_data: Any | None = None,
) -> Any:
    """Perform an HTTP request returning JSON with timeout handling."""

    req_kwargs: dict[str, Any] = {}
    if headers is not None:
        req_kwargs["headers"] = headers
    if data is not None:
        req_kwargs["data"] = data
    if json_data is not None:
        req_kwargs["json"] = json_data

    async with _enlighten_read_request_guard(method, url):
        async with asyncio.timeout(timeout):
            async with session.request(
                method, url, allow_redirects=True, **req_kwargs
            ) as resp:
                if resp.status >= 500:
                    raise EnlightenAuthUnavailable(
                        f"Server error {resp.status} at {_request_label(method, url)}"
                    )
                if resp.status >= 400:
                    body_text = ""
                    try:
                        body_text = await resp.text()
                    except Exception:  # noqa: BLE001 - best effort auth diagnostics
                        body_text = ""
                    if _is_too_many_active_sessions_response(body_text):
                        raise EnlightenAuthTooManySessions(
                            "Too many active Enlighten sessions"
                        )
                resp.raise_for_status()
                ctype = resp.headers.get("Content-Type", "")
                if "json" not in ctype:
                    text = await resp.text()
                    if _is_too_many_active_sessions_response(text):
                        raise EnlightenAuthTooManySessions(
                            "Too many active Enlighten sessions"
                        )
                    raise EnlightenAuthUnavailable(
                        _safe_response_error_message(
                            status=int(resp.status),
                            reason="Unexpected non-JSON response",
                            headers=resp.headers,
                            body_text=text,
                        )
                    )
                payload = await resp.json()
                if _is_too_many_active_sessions_response(payload):
                    raise EnlightenAuthTooManySessions(
                        "Too many active Enlighten sessions"
                    )
                return payload


async def _request_mfa_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    timeout: int,
    headers: dict[str, str] | None = None,
    data: Any | None = None,
) -> Any:
    """Perform an MFA HTTP request with tolerant JSON parsing."""

    req_kwargs: dict[str, Any] = {}
    if headers is not None:
        req_kwargs["headers"] = headers
    if data is not None:
        req_kwargs["data"] = data

    async with asyncio.timeout(timeout):
        async with session.request(
            method, url, allow_redirects=True, **req_kwargs
        ) as resp:
            if resp.status >= 500:
                raise EnlightenAuthUnavailable(
                    f"Server error {resp.status} at {_request_label(method, url)}"
                )
            if resp.status in (204, 205):
                return {}
            if resp.status >= 400:
                body_text = ""
                try:
                    body_text = await resp.text()
                except Exception:  # noqa: BLE001 - best effort auth diagnostics
                    body_text = ""
                if _is_too_many_active_sessions_response(body_text):
                    raise EnlightenAuthTooManySessions(
                        "Too many active Enlighten sessions"
                    )
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                payload = await resp.json()
                if _is_too_many_active_sessions_response(payload):
                    raise EnlightenAuthTooManySessions(
                        "Too many active Enlighten sessions"
                    )
                return payload
            text = await resp.text()
            if _is_too_many_active_sessions_response(text):
                raise EnlightenAuthTooManySessions("Too many active Enlighten sessions")
            if not text.strip():
                return {}
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as err:
                raise EnlightenAuthUnavailable(
                    _safe_response_error_message(
                        status=int(resp.status),
                        reason="Unexpected MFA response",
                        headers=resp.headers,
                        body_text=text,
                    )
                ) from err
            if _is_too_many_active_sessions_response(payload):
                raise EnlightenAuthTooManySessions("Too many active Enlighten sessions")
            return payload


def _mfa_headers(cookies: dict[str, str] | None) -> dict[str, str]:
    """Return headers for MFA endpoints with cookie/XSRF handling."""

    headers = _login_headers()
    headers["Accept"] = "application/json, text/plain, */*"
    cookie_header = _cookie_header_from_map(cookies)
    if cookie_header:
        headers["Cookie"] = cookie_header
    xsrf_token = _extract_xsrf_token(cookies)
    if xsrf_token:
        headers["X-CSRF-Token"] = xsrf_token
    return headers


def _login_headers() -> dict[str, str]:
    """Return headers for the initial Enlighten login request."""

    return {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": f"{BASE_URL}/",
        "User-Agent": _ENLIGHTEN_BROWSER_USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
    }


def _login_form_headers() -> dict[str, str]:
    """Return browser-style headers for the HTML form login flow."""

    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "User-Agent": _ENLIGHTEN_BROWSER_USER_AGENT,
    }


def _extract_login_session_from_cookies(
    cookies: dict[str, str] | None,
) -> tuple[str | None, str | None]:
    """Extract session details from post-login cookies."""

    if not cookies:
        return None, None

    session_id = (
        cookies.get("_enlighten_4_session")
        or cookies.get("enlighten_session")
        or cookies.get("_enlighten_session")
    )
    manager_token = cookies.get("enlighten_manager_token_production")
    if not session_id and manager_token:
        session_id = _jwt_session_id(manager_token)
    return (
        str(session_id) if session_id else None,
        str(manager_token) if manager_token else None,
    )


async def _submit_login_form(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    *,
    timeout: int,
) -> tuple[str | None, str | None]:
    """Submit the browser form login flow and derive auth state from cookies."""

    payload = {"user[email]": email, "user[password]": password}

    async with asyncio.timeout(timeout):
        async with session.request(
            "POST",
            LOGIN_FORM_URL,
            allow_redirects=True,
            headers=_login_form_headers(),
            data=payload,
        ) as resp:
            body_text = ""
            if resp.status >= 500:
                raise EnlightenAuthUnavailable(
                    f"Server error {resp.status} at {LOGIN_FORM_URL}"
                )
            try:
                body_text = await resp.text()
            except Exception:  # noqa: BLE001 - best effort auth diagnostics
                body_text = ""
            if _is_too_many_active_sessions_response(body_text):
                raise EnlightenAuthTooManySessions("Too many active Enlighten sessions")
            resp.raise_for_status()

    _, cookie_map = _serialize_cookie_jar(session.cookie_jar, (BASE_URL, ENTREZ_URL))
    return _extract_login_session_from_cookies(cookie_map)


def _normalize_sites(payload: Any) -> list[SiteInfo]:
    """Normalize site payloads from various Enlighten APIs."""

    return api_parsers.normalize_sites(payload)


def _normalize_chargers(payload: Any) -> list[ChargerInfo]:
    """Normalize charger list payloads into ChargerInfo entries."""

    return api_parsers.normalize_chargers(payload)


async def _build_tokens_and_sites(
    session: aiohttp.ClientSession,
    email: str,
    session_id: str | None,
    *,
    timeout: int,
) -> tuple[AuthTokens, list[SiteInfo]]:
    """Build auth tokens and discover accessible sites from an authenticated session."""

    cookie_header, cookie_map = _serialize_cookie_jar(
        session.cookie_jar, (BASE_URL, ENTREZ_URL)
    )
    tokens = AuthTokens(
        cookie=cookie_header,
        session_id=str(session_id) if session_id else None,
        raw_cookies=cookie_map,
    )

    # Attempt to obtain a bearer/e-auth token. If not available, proceed with cookie-only mode.
    token_payload: Any | None = None
    if tokens.session_id:
        try:
            token_payload = await _request_json(
                session,
                "POST",
                f"{ENTREZ_URL}/tokens",
                timeout=timeout,
                headers={"Accept": "application/json"},
                json_data={"session_id": tokens.session_id, "email": email},
            )
        except aiohttp.ClientResponseError as err:  # noqa: BLE001
            if err.status in (401, 403):
                raise EnlightenAuthInvalidCredentials from err
            safe_error = redact_text(err)
            if err.status in (404, 422, 429):
                _LOGGER.debug(
                    "Token endpoint unavailable (%s): %s",
                    err.status,
                    safe_error,
                )
            else:
                _LOGGER.debug(
                    "Token endpoint error (%s): %s",
                    err.status,
                    safe_error,
                )
        except EnlightenAuthUnavailable as err:
            safe_error = redact_text(err)
            _LOGGER.debug(
                "Token endpoint unavailable: %s",
                safe_error,
            )
        except aiohttp.ClientError as err:  # noqa: BLE001
            safe_error = redact_text(err)
            _LOGGER.debug(
                "Token endpoint client error: %s",
                safe_error,
            )

    if isinstance(token_payload, dict):
        token = (
            token_payload.get("token")
            or token_payload.get("auth_token")
            or token_payload.get("access_token")
        )
        if token:
            tokens.access_token = str(token)
            exp = (
                token_payload.get("expires_at")
                or token_payload.get("expiresAt")
                or token_payload.get("expiration")
            )
            if exp is None:
                exp = _decode_jwt_exp(tokens.access_token)
            tokens.token_expires_at = (
                int(exp) if isinstance(exp, (int, float)) else None
            )

    xsrf_token = _extract_xsrf_token(tokens.raw_cookies)

    # Collect accessible sites for the authenticated user.
    site_headers = {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/",
        "User-Agent": _ENLIGHTEN_BROWSER_USER_AGENT,
    }
    if xsrf_token:
        site_headers["X-CSRF-Token"] = xsrf_token
    if tokens.cookie:
        site_headers["Cookie"] = tokens.cookie
    if tokens.access_token:
        site_headers["Authorization"] = f"Bearer {tokens.access_token}"
        site_headers["e-auth-token"] = tokens.access_token

    sites: list[SiteInfo] = []
    for url in (SITE_SEARCH_URL,):
        try:
            site_payload = await _request_json(
                session,
                "GET",
                url,
                timeout=timeout,
                headers=dict(site_headers),
            )
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                raise EnlightenAuthInvalidCredentials from err
            safe_error = redact_text(err)
            _LOGGER.debug(
                "Site discovery endpoint error (%s): %s",
                err.status,
                safe_error,
            )
            continue
        except EnlightenAuthUnavailable as err:
            safe_error = redact_text(err)
            _LOGGER.debug(
                "Site discovery unavailable: %s",
                safe_error,
            )
            continue
        except aiohttp.ClientError as err:  # noqa: BLE001
            safe_error = redact_text(err)
            _LOGGER.debug(
                "Site discovery client error: %s",
                safe_error,
            )
            continue
        sites = _normalize_sites(site_payload)
        if sites:
            break

    return tokens, sites


async def async_authenticate(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> tuple[AuthTokens, list[SiteInfo]]:
    """Authenticate with Enlighten and return auth tokens and accessible sites."""

    payload = {"user[email]": email, "user[password]": password}
    headers = _login_headers()
    data: Any | None = None

    try:
        data = await _request_json(
            session,
            "POST",
            LOGIN_URL,
            timeout=timeout,
            headers=headers,
            data=payload,
        )
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            raise EnlightenAuthInvalidCredentials from err
        if err.status == 406:
            try:
                session_id, manager_token = await _submit_login_form(
                    session, email, password, timeout=timeout
                )
            except aiohttp.ClientResponseError as fallback_err:
                if fallback_err.status in (401, 403):
                    raise EnlightenAuthInvalidCredentials from fallback_err
                raise
            except aiohttp.ClientError as fallback_err:  # noqa: BLE001
                raise EnlightenAuthUnavailable from fallback_err
            if session_id or manager_token:
                if not session_id:
                    raise EnlightenAuthInvalidCredentials("Missing session identifier")
                return await _build_tokens_and_sites(
                    session, email, session_id, timeout=timeout
                )
            raise EnlightenAuthInvalidCredentials("Unexpected login response")
        raise
    except aiohttp.ClientError as err:  # noqa: BLE001
        raise EnlightenAuthUnavailable from err

    cookie_header, cookie_map = _serialize_cookie_jar(
        session.cookie_jar, (BASE_URL, ENTREZ_URL)
    )

    session_id, manager_token = _extract_login_session(data)

    if isinstance(data, dict) and data.get("requires_mfa"):
        tokens = AuthTokens(cookie=cookie_header, raw_cookies=cookie_map)
        raise EnlightenAuthMFARequired(
            "Account requires multi-factor authentication", tokens=tokens
        )

    if isinstance(data, dict) and data.get("isBlocked") is True:
        raise EnlightenAuthInvalidCredentials("Account is blocked")

    if session_id or manager_token:
        if not session_id:
            raise EnlightenAuthInvalidCredentials("Missing session identifier")
        return await _build_tokens_and_sites(
            session, email, session_id, timeout=timeout
        )

    if isinstance(data, dict) and data.get("success") is True:
        if cookie_map.get("login_otp_nonce"):
            tokens = AuthTokens(cookie=cookie_header, raw_cookies=cookie_map)
            raise EnlightenAuthMFARequired(
                "Account requires multi-factor authentication", tokens=tokens
            )
        raise EnlightenAuthInvalidCredentials("MFA challenge missing")

    if isinstance(data, dict) and not data:
        return await _build_tokens_and_sites(session, email, None, timeout=timeout)

    raise EnlightenAuthInvalidCredentials("Unexpected login response")


async def async_validate_login_otp(
    session: aiohttp.ClientSession,
    email: str,
    otp: str,
    cookies: dict[str, str],
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> tuple[AuthTokens, list[SiteInfo]]:
    """Validate an MFA one-time code and return auth tokens and sites."""

    email = email.strip()
    otp = otp.strip()
    if not email or not otp:
        raise EnlightenAuthInvalidCredentials("Missing OTP credentials")

    _seed_cookie_jar(session, cookies)

    payload = {
        "email": base64.b64encode(email.encode("utf-8")).decode("ascii"),
        "otp": base64.b64encode(otp.encode("utf-8")).decode("ascii"),
        "xhrFields[withCredentials]": "true",
    }
    headers = _mfa_headers(cookies)

    try:
        data = await _request_mfa_json(
            session,
            "POST",
            MFA_VALIDATE_URL,
            timeout=timeout,
            headers=headers,
            data=payload,
        )
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            _LOGGER.warning(
                "MFA validation rejected by Enlighten (status=%s)", err.status
            )
            raise EnlightenAuthInvalidCredentials from err
        if err.status == 429:
            _LOGGER.warning("MFA validation rate limited by Enlighten")
            raise EnlightenAuthOTPBlocked("MFA is blocked") from err
        if err.status in (400, 404, 409, 422):
            _LOGGER.warning(
                "MFA validation failed with client error (status=%s)", err.status
            )
            raise EnlightenAuthInvalidOTP("Invalid MFA code") from err
        raise
    except aiohttp.ClientError as err:  # noqa: BLE001
        raise EnlightenAuthUnavailable from err

    if isinstance(data, dict) and data.get("isValid") is False:
        if data.get("isBlocked") is True:
            _LOGGER.warning("MFA validation blocked by Enlighten response")
            raise EnlightenAuthOTPBlocked("MFA is blocked")
        _LOGGER.warning("MFA validation rejected by Enlighten response")
        raise EnlightenAuthInvalidOTP("Invalid MFA code")

    session_id, manager_token = _extract_login_session(data)
    if not session_id and manager_token:
        raise EnlightenAuthInvalidCredentials("Missing session identifier")
    if not session_id:
        looks_successful = False
        if isinstance(data, dict):
            looks_successful = bool(
                data.get("message") == "success"
                or data.get("success") is True
                or data.get("isValid") is True
            )
        if looks_successful or not data:
            _LOGGER.warning(
                "MFA validation missing session id; attempting token recovery"
            )
            try:
                return await _build_tokens_and_sites(
                    session, email, None, timeout=timeout
                )
            except EnlightenAuthInvalidCredentials as err:
                raise EnlightenAuthInvalidOTP("Missing MFA session identifier") from err
        raise EnlightenAuthInvalidOTP("Missing MFA session identifier")

    return await _build_tokens_and_sites(session, email, session_id, timeout=timeout)


async def async_resend_login_otp(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    *,
    timeout: int = DEFAULT_AUTH_TIMEOUT,
) -> AuthTokens:
    """Request a new MFA one-time code and return refreshed cookie state."""

    _seed_cookie_jar(session, cookies)

    headers = _mfa_headers(cookies)

    try:
        data = await _request_mfa_json(
            session,
            "POST",
            MFA_RESEND_URL,
            timeout=timeout,
            headers=headers,
            data={"locale": "en"},
        )
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            _LOGGER.warning("MFA resend rejected by Enlighten (status=%s)", err.status)
            raise EnlightenAuthInvalidCredentials from err
        if err.status == 429:
            _LOGGER.warning("MFA resend rate limited by Enlighten")
            raise EnlightenAuthOTPBlocked("MFA is blocked") from err
        raise
    except aiohttp.ClientError as err:  # noqa: BLE001
        raise EnlightenAuthUnavailable from err

    if isinstance(data, dict) and data.get("isBlocked") is True:
        _LOGGER.warning("MFA resend blocked by Enlighten response")
        raise EnlightenAuthOTPBlocked("MFA is blocked")
    if isinstance(data, dict) and data.get("success") is False:
        _LOGGER.warning("MFA resend rejected by Enlighten response")
        raise EnlightenAuthInvalidCredentials("MFA resend rejected")
    if not data:
        _LOGGER.warning("MFA resend returned empty response; using existing cookies")
        data = {"success": True}
    if not (isinstance(data, dict) and data.get("success") is True):
        _LOGGER.warning("MFA resend returned unexpected response")
        raise EnlightenAuthInvalidCredentials("MFA resend rejected")

    cookie_header, cookie_map = _serialize_cookie_jar(
        session.cookie_jar, (BASE_URL, ENTREZ_URL)
    )
    if not cookie_map and cookies:
        _LOGGER.warning("MFA resend did not return updated cookies; reusing existing")
        cookie_map = dict(cookies)
        cookie_header = _cookie_header_from_map(cookie_map)
    return AuthTokens(cookie=cookie_header, raw_cookies=cookie_map)


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

    def _payload_inverters(
        payload: dict[str, object],
    ) -> tuple[list[dict[str, object]], str]:
        inverters = payload.get("inverters")
        if isinstance(inverters, list):
            return ([item for item in inverters if isinstance(item, dict)], "root")
        result = payload.get("result")
        if isinstance(result, dict):
            inverters = result.get("inverters")
            if isinstance(inverters, list):
                return (
                    [item for item in inverters if isinstance(item, dict)],
                    "result",
                )
        return ([], "")

    def _payload_total(payload: dict[str, object], default: int) -> int:
        raw_total = payload.get("total")
        try:
            total = int(raw_total)
        except (TypeError, ValueError):
            return default
        return total if total >= 0 else default

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
        payload = await _fetch_page(0)
        if payload is None:
            return None

        inverters, storage_key = _payload_inverters(payload)
        total_expected = _payload_total(payload, len(inverters))
        if storage_key and total_expected > len(inverters):
            merged = list(inverters)
            next_offset = len(merged)
            while next_offset < total_expected:
                next_payload = await _fetch_page(next_offset)
                if next_payload is None:
                    break
                next_inverters, _ = _payload_inverters(next_payload)
                if not next_inverters:
                    break
                merged.extend(next_inverters)
                total_expected = max(
                    total_expected,
                    _payload_total(next_payload, total_expected),
                )
                next_offset += len(next_inverters)
            payload = dict(payload)
            if storage_key == "root":
                payload["inverters"] = merged
            else:
                result = payload.get("result")
                result_dict = dict(result) if isinstance(result, dict) else {}
                result_dict["inverters"] = merged
                payload["result"] = result_dict
        return payload
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


class EnphaseEVClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        site_id: str,
        eauth: str | None,
        cookie: str | None,
        timeout: int = 15,
        reauth_callback: Callable[[], Awaitable[bool]] | None = None,
    ):
        self._timeout = int(timeout)
        self._s = session
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
        self._hems_site_supported: bool | None = None
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

    def set_reauth_callback(
        self, callback: Callable[[], Awaitable[bool]] | None
    ) -> None:
        """Register coroutine used to refresh credentials on 401."""

        self._reauth_cb = callback

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
        """Update headers when auth credentials change."""

        if eauth is not None:
            self._eauth = eauth or None
        if cookie is not None:
            self._cookie = cookie or ""

        if self._cookie:
            self._h["Cookie"] = self._cookie
        else:
            self._h.pop("Cookie", None)

        if self._eauth:
            self._h["e-auth-token"] = self._eauth
        else:
            self._h.pop("e-auth-token", None)

        # If XSRF cookies are present, add matching CSRF header some endpoints expect.
        try:
            xsrf = self._xsrf_token()
            if xsrf:
                self._h["X-CSRF-Token"] = xsrf
            else:
                self._h.pop("X-CSRF-Token", None)
        except Exception:  # noqa: BLE001 - defensive: header should never break setup
            self._h.pop("X-CSRF-Token", None)

    def _bearer(self) -> str | None:
        """Extract Authorization bearer token from cookies if present.

        Enlighten sets an `enlighten_manager_token_production` cookie with a JWT the
        frontend uses as an Authorization Bearer token for some scheduler endpoints.
        """
        try:
            parts = [p.strip() for p in (self._cookie or "").split(";")]
            for p in parts:
                if p.startswith("enlighten_manager_token_production="):
                    return p.split("=", 1)[1]
        except Exception:
            return None
        return None

    def scheduler_bearer(self) -> str | None:
        """Public bearer accessor for scheduler feature checks."""

        return self._bearer()

    def has_scheduler_bearer(self) -> bool:
        """Return True when scheduler bearer auth can be derived."""

        return bool(self.scheduler_bearer())

    @property
    def hems_site_supported(self) -> bool | None:
        """Return whether HEMS has been positively identified for this site."""

        return self._hems_site_supported

    def base_header_names(self) -> list[str]:
        """Return base header names without exposing values."""

        return sorted(self._h.keys())

    def _mark_payload_healthy(self, endpoint: str | None) -> None:
        """Log endpoint recovery once after a prior invalid payload."""

        endpoint_key = str(endpoint or "").strip() or "<unknown>"
        previous = self._payload_failure_log_state.pop(endpoint_key, None)
        if previous is None:
            return
        endpoint_safe = redact_text(endpoint_key, site_ids=(self._site,))
        _LOGGER.info(
            "Payload recovered for site %s endpoint %s",
            redact_site_id(self._site),
            endpoint_safe,
        )

    def _log_invalid_payload(self, err: InvalidPayloadError) -> None:
        """Log invalid payload details once per endpoint failure transition."""

        signature = err.signature
        endpoint_key = str(signature.endpoint or "").strip() or "<unknown>"
        previous = self._payload_failure_log_state.get(endpoint_key)
        self._payload_failure_log_state[endpoint_key] = signature
        if previous is not None:
            return
        endpoint_safe = redact_text(endpoint_key, site_ids=(self._site,))
        _LOGGER.warning(
            "Invalid payload for site %s endpoint %s "
            "(status=%s, content_type=%s, failure_kind=%s, decode_error=%s, "
            "body_length=%s, body_sha256=%s, preview=%s)",
            redact_site_id(self._site),
            endpoint_safe,
            signature.status,
            signature.content_type or "<missing>",
            signature.failure_kind or "<unknown>",
            signature.decode_error or "<none>",
            signature.body_length,
            signature.body_sha256 or "<none>",
            signature.body_preview_redacted or "<empty>",
        )

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
        """Build and log a structured invalid payload error."""

        body_length, body_sha256, body_preview = _payload_preview_and_hash(
            payload,
            site_ids=(self._site,),
        )
        err = InvalidPayloadError(
            summary or "",
            status=status,
            content_type=content_type,
            endpoint=endpoint,
            failure_kind=failure_kind,
            decode_error=decode_error,
            body_length=body_length,
            body_sha256=body_sha256,
            body_preview_redacted=body_preview,
        )
        if log_warning:
            self._log_invalid_payload(err)
        return err

    def _login_wall_unauthorized(
        self,
        *,
        endpoint: str | None,
        request_label: str,
        status: int | None,
        content_type: str | None,
        payload: object,
    ) -> EnphaseLoginWallUnauthorized:
        """Build a structured unauthorized error for Enlighten login-wall responses."""

        _, _, body_preview = _payload_preview_and_hash(payload, site_ids=(self._site,))
        return EnphaseLoginWallUnauthorized(
            endpoint=endpoint,
            request_label=request_label,
            status=status,
            content_type=content_type,
            body_preview_redacted=body_preview,
        )

    def _history_bearer(self) -> str | None:
        """Return the preferred bearer token for session history calls."""

        return self._eauth or self._bearer()

    def _session_history_username(self) -> str | None:
        """Return the user id expected by the session history service."""

        return _jwt_user_id(self._history_bearer())

    def _session_history_headers(
        self, request_id: str | None, username: str | None
    ) -> dict[str, str]:
        """Return headers for session history endpoints."""

        headers = dict(self._h)
        headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        bearer = self._history_bearer()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        session_id = _jwt_session_id(bearer)
        if session_id:
            headers["e-auth-token"] = session_id
        else:
            headers.pop("e-auth-token", None)
        if request_id:
            headers["requestid"] = request_id
        if username:
            headers["username"] = username
        return headers

    def _evse_timeseries_headers(
        self,
        request_id: str | None,
        username: str | None,
    ) -> dict[str, str]:
        """Return headers for EVSE timeseries endpoints."""

        return self._session_history_headers(request_id, username)

    def _site_web_graph_referer(self, view: str, *, graph_range: str = "years") -> str:
        """Return a web-app graph referer for a site-scoped Enlighten view."""

        query = ""
        app_version = _cookie_map_from_header(self._cookie).get("appVersion")
        if app_version:
            query = f"?v={app_version}"
        return f"{BASE_URL}/web/{self._site}/{view}/graph/{graph_range}{query}"

    def _site_web_referer(self, view: str) -> str:
        """Return the default years-graph referer for site XHR families."""

        return self._site_web_graph_referer(view)

    def _root_xhr_headers(self) -> dict[str, str]:
        """Return base headers for root-scoped Enlighten XHR requests."""

        headers = dict(self._h)
        headers["Accept"] = "*/*"
        headers["Referer"] = f"{BASE_URL}/"
        return headers

    def _history_headers(self) -> dict[str, str]:
        """Return headers for app-api and pv/settings history-family requests."""

        headers = dict(self._h)
        headers["Accept"] = "*/*"
        headers["Referer"] = self._site_web_referer("history")
        return headers

    def _today_headers(self) -> dict[str, str]:
        """Return headers for EV today-page XHR requests."""

        headers = dict(self._h)
        headers["Accept"] = "*/*"
        headers["Referer"] = self._site_web_graph_referer("today", graph_range="hours")
        return headers

    def _today_json_headers(self) -> dict[str, str]:
        """Return headers for EV today-page JSON/XHR requests."""

        headers = self._today_headers()
        headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        return headers

    def _history_form_headers(self) -> dict[str, str]:
        """Return headers for history-family form POST requests."""

        headers = self._history_headers()
        headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["Origin"] = BASE_URL
        return headers

    def _layout_headers(self) -> dict[str, str]:
        """Return headers for systems/layout-family requests."""

        headers = dict(self._h)
        headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        headers["Referer"] = self._site_web_referer("layout")
        return headers

    def _systems_html_headers(self, referer: str | None = None) -> dict[str, str]:
        """Return browser-style headers for site-scoped HTML /systems routes."""

        headers = dict(self._h)
        headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        )
        headers["Referer"] = referer or f"{BASE_URL}/systems/{self._site}/devices"
        return headers

    def _systems_json_headers(self) -> dict[str, str]:
        """Return headers for site-scoped /systems JSON endpoints."""

        headers = dict(self._h)
        headers["Accept"] = "application/json"
        headers["Referer"] = self._site_web_referer("layout")
        return headers

    def _control_headers(self) -> dict[str, str]:
        """Return Authorization header overrides for control-plane requests."""

        bearer = self._bearer() or self._eauth
        if bearer:
            return {"Authorization": f"Bearer {bearer}"}
        return {}

    def control_headers(self) -> dict[str, str]:
        """Public control header helper for read-only diagnostics checks."""

        return self._control_headers()

    def _system_dashboard_headers(self) -> dict[str, str]:
        """Return headers for system dashboard read endpoints."""

        headers = dict(self._h)
        headers["Accept"] = "application/json"
        headers["Referer"] = (
            f"{BASE_URL}/app/system_dashboard/sites/{self._site}/summary"
        )
        headers.update(self._control_headers())
        return headers

    def _hems_auth_context(self) -> tuple[str | None, str | None]:
        """Return the preferred HEMS bearer token and resolved user id."""

        bearer = self._bearer() or self._eauth
        return bearer, _jwt_user_id(bearer)

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
    ) -> dict | None:
        """Fetch a system dashboard payload from the modern route with fallback."""

        headers = self._system_dashboard_headers()
        for url in (modern_url, legacy_url):
            try:
                data = await self._json("GET", url, headers=headers)
            except Exception as err:  # noqa: BLE001
                if self._system_dashboard_is_optional_error(err):
                    continue
                raise
            return data if isinstance(data, dict) else None

        return None

    def _hems_headers(self) -> dict[str, str]:
        """Return headers for HEMS read endpoints."""

        headers = dict(self._h)
        headers["Accept"] = "application/json"
        headers["Origin"] = BASE_URL
        bearer, username = self._hems_auth_context()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if username:
            headers["username"] = username
        headers["requestId"] = str(uuid.uuid4())
        return headers

    def _battery_config_user_id(self) -> str | None:
        """Return the user id for BatteryConfig requests when available."""

        _token, user_id = self._battery_config_auth_context()
        return user_id

    def _battery_config_single_auth_token(self) -> str | None:
        """Return the single-token auth candidate used by external clients."""

        return self._bearer() or self._eauth

    def _battery_config_user_id_for_token(self, token: str | None = None) -> str | None:
        """Return the preferred BatteryConfig user id for a specific token."""

        if token:
            user_id = _jwt_user_id(token)
            if user_id:
                return user_id
        return self._battery_config_user_id()

    def _battery_config_auth_source_label(self, token: str | None) -> str:
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
        self,
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

    @staticmethod
    def _merge_request_headers(
        base_headers: dict[str, str],
        extra_headers: dict[str, str | None] | None,
    ) -> dict[str, str]:
        """Merge request headers, treating ``None`` values as explicit removals."""

        merged = dict(base_headers)
        if not isinstance(extra_headers, dict):
            return merged
        for header_key, header_value in extra_headers.items():
            if header_value is None:
                merged.pop(header_key, None)
            else:
                merged[header_key] = header_value
        return merged

    def _battery_config_auth_context(self) -> tuple[str | None, str | None]:
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
        self,
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
                if variant == _BATTERY_CONFIG_VARIANT_PRIMARY
                else None
            ),
        }
        token, user_id = self._battery_config_auth_context()
        if variant == _BATTERY_CONFIG_VARIANT_PRIMARY:
            headers["e-auth-token"] = token
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

    def _tariff_headers(self, *, write: bool = False) -> dict[str, str | None]:
        """Return headers for tariff microservice calls."""

        token, user_id = self._battery_config_auth_context()
        headers: dict[str, str | None] = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Cookie": self._cookie or None,
            "e-auth-token": token,
            "Requestid": str(uuid.uuid4()),
            "X-Requested-With": "XMLHttpRequest",
        }
        if user_id:
            headers["Username"] = user_id
        xsrf = self._xsrf_token()
        if xsrf:
            headers["x-xsrf-token"] = xsrf
        if write:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = BASE_URL
            headers["Referer"] = f"{BASE_URL}/"
        return headers

    def _battery_config_cookie_eauth_headers(
        self,
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

    def _battery_config_cookie(self, *, include_xsrf: bool = False) -> str | None:
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

    def _battery_config_cookie_header_xsrf_token(self) -> str | None:
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
        self,
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
        self, schedule_type: str = "cfg"
    ) -> dict[str, object]:
        """Return the XSRF bootstrap / validation payload for a schedule family."""

        normalized = str(schedule_type).lower()
        payload: dict[str, object] = {"scheduleType": normalized}
        if normalized == "cfg":
            payload["forceScheduleOpted"] = True
        return payload

    def _battery_config_params(
        self,
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

    def _battery_config_endpoint_family(self, url: str) -> str:
        """Return the cache family for a BatteryConfig endpoint URL."""

        if "/batterySettings/" in url:
            return "battery_settings"
        if "/battery/sites/" in url and "/schedules" in url:
            return "schedules"
        return "profile"

    def _battery_config_variant_cache_key(
        self, endpoint_family: str
    ) -> tuple[str, str, str]:
        """Return the cache key for BatteryConfig request variants."""

        user_id = self._battery_config_user_id() or "<unknown>"
        return (str(self._site), user_id, str(endpoint_family))

    def _battery_config_cached_variant(self, endpoint_family: str) -> str | None:
        """Return the cached request variant for a BatteryConfig family."""

        key = self._battery_config_variant_cache_key(endpoint_family)
        return self._battery_config_variant_cache.get(key)

    def _cache_battery_config_variant(self, endpoint_family: str, variant: str) -> None:
        """Remember the working request variant for a BatteryConfig family."""

        key = self._battery_config_variant_cache_key(endpoint_family)
        self._battery_config_variant_cache[key] = variant

    def _battery_config_variant_order(self, endpoint_family: str) -> list[str]:
        """Return the ordered variants to try for a BatteryConfig family."""

        cached = self._battery_config_cached_variant(endpoint_family)
        variants = [
            cached,
            _BATTERY_CONFIG_VARIANT_PRIMARY,
            _BATTERY_CONFIG_VARIANT_LEAN,
        ]
        return [
            variant
            for variant in dict.fromkeys(variants)
            if variant
            in {_BATTERY_CONFIG_VARIANT_PRIMARY, _BATTERY_CONFIG_VARIANT_LEAN}
        ]

    def _battery_config_write_attempt_cache_key(
        self,
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
        self,
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
                                attempt_id=(
                                    "battery_settings_stateful_primary_no_source"
                                ),
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
                                attempt_id=(
                                    "battery_settings_stateful_disclaimer_true"
                                ),
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
                    0
                    if attempt.auth_mode == _BATTERY_CONFIG_VARIANT_COOKIE_EAUTH
                    else 1
                ),
            )
        return attempts

    def _battery_config_prefers_cookie_compat(self) -> bool:
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
        self,
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
        self,
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
        self,
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
        self,
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
                if attempt.omit_source
                and isinstance(params, dict)
                and "source" in params
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

    @staticmethod
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
        """Return the nested BatteryConfig data payload when available."""

        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload

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

    def _xsrf_token(self) -> str | None:
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
    ) -> dict:
        """Issue a BatteryConfig request using the observed first-party variants."""

        family = endpoint_family or self._battery_config_endpoint_family(url)
        variants = self._battery_config_variant_order(family)

        try:
            for index, variant in enumerate(variants):
                try:
                    if bootstrap_xsrf:
                        await self._acquire_xsrf_token(schedule_type, variant=variant)
                    headers = self._battery_config_headers(
                        include_xsrf=bootstrap_xsrf,
                        variant=variant,
                    )
                    if json_body is not None:
                        headers.setdefault("Content-Type", "application/json")
                    result = await self._json(
                        method,
                        url,
                        json=json_body,
                        headers=headers,
                        params=params,
                        debug_auth_source=variant,
                    )
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
                return result
        finally:
            if bootstrap_xsrf:
                self._bp_xsrf_token = None

        raise aiohttp.ClientError("BatteryConfig request exhausted variants")

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
    ) -> dict:
        """Issue a BatteryConfig write using endpoint-specific compatibility attempts."""

        family = endpoint_family or self._battery_config_endpoint_family(url)
        attempts = self._battery_config_write_attempts(
            family,
            write_intent=write_intent,
            supports_mqtt=supports_mqtt,
            params=params,
            json_body=json_body,
        )
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
                    headers = self._battery_config_attempt_headers(
                        attempt,
                        include_xsrf=True,
                    )
                    if attempt_json_body is not None:
                        headers.setdefault("Content-Type", "application/json")
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
                            isinstance(next_json_body, dict)
                            and "devices" in next_json_body
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
                return result
        finally:
            self._bp_xsrf_token = None

        if last_error is not None:
            raise last_error
        raise aiohttp.ClientError("BatteryConfig request exhausted variants")

    @staticmethod
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

    @staticmethod
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
        self,
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
                            return _remember_xsrf(
                                token, "isValid error response cookies"
                            )
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

                    _LOGGER.warning(
                        "isValid endpoint did not return BP-XSRF-Token cookie"
                    )
                    return None
        except Exception:  # noqa: BLE001 - XSRF acquisition is best-effort
            _LOGGER.warning("Failed to acquire XSRF token", exc_info=True)
            return None

    def _redact_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Return a copy of headers with sensitive values masked."""

        redacted: dict[str, str] = {}
        for key, value in headers.items():
            if key.lower() in {
                "cookie",
                "authorization",
                "e-auth-token",
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
        """Return a short, non-reversible debug representation for IDs."""

        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001 - defensive casting
            return None
        if not text:
            return None
        if len(text) <= 2:
            return "[redacted]"
        if len(text) <= 8:
            return f"{text[:1]}...{text[-1:]}"
        return f"{text[:4]}...{text[-4:]}"

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
        **kwargs,
    ):
        """Perform an HTTP request returning JSON with sane header handling.

        Accepts optional ``headers`` in kwargs which will be merged with the
        default headers for this client, allowing call-sites to add/override
        fields (e.g. Authorization) without causing duplicate parameter errors.
        Header values explicitly set to ``None`` are removed from the merged
        request headers, which allows per-request suppression of defaults such
        as ``e-auth-token``.
        ``headers`` may also be a zero-argument callable so retries can rebuild
        auth-sensitive headers after a successful reauthentication callback.
        """
        extra_headers = kwargs.pop("headers", None)
        use_cookie_header_only = kwargs.pop("use_cookie_header_only", False)
        debug_auth_source = kwargs.pop("debug_auth_source", None)
        debug_battery_attempt_id = kwargs.pop("debug_battery_attempt_id", None)
        debug_battery_attempt_changes = kwargs.pop(
            "debug_battery_attempt_changes",
            None,
        )
        allow_reauth = bool(kwargs.pop("allow_reauth", True))
        attempt = 0
        request_label = _request_label(method, url)
        safe_request_label = redact_text(
            request_label, site_ids=(self._site,), max_length=256
        )
        endpoint = ""
        try:
            endpoint = URL(url).path
        except Exception:  # noqa: BLE001 - defensive URL parsing
            endpoint = ""
        while True:
            base_headers = dict(self._h)
            if callable(extra_headers):
                attempt_headers = extra_headers()
            else:
                attempt_headers = extra_headers
            if isinstance(attempt_headers, dict):
                base_headers = self._merge_request_headers(
                    base_headers, attempt_headers
                )

            async with _enlighten_read_request_guard(method, url):
                async with asyncio.timeout(self._timeout):
                    async with self._request_session(
                        cookie_header_only=use_cookie_header_only
                    ) as request_session:
                        self._request_count += 1
                        async with request_session.request(
                            method, url, headers=base_headers, **kwargs
                        ) as r:
                            if r.status == 401:
                                self._last_unauthorized_request = safe_request_label
                                hems_api_endpoint = self._is_hems_api_endpoint(
                                    endpoint or None
                                )
                                reauth_allowed = allow_reauth and not hems_api_endpoint
                                if not reauth_allowed:
                                    _LOGGER.debug(
                                        "Received 401 for %s with stored-credential refresh disabled for this endpoint family",
                                        safe_request_label,
                                    )
                                elif self._reauth_cb and attempt == 0:
                                    _LOGGER.debug(
                                        "Received 401 for %s; attempting stored-credential refresh",
                                        safe_request_label,
                                    )
                                    attempt += 1
                                    reauth_ok = await self._reauth_cb()
                                    if reauth_ok:
                                        _LOGGER.debug(
                                            "Stored-credential refresh succeeded for %s; retrying request",
                                            safe_request_label,
                                        )
                                        continue
                                    _LOGGER.debug(
                                        "Stored-credential refresh failed for %s",
                                        safe_request_label,
                                    )
                                else:
                                    _LOGGER.debug(
                                        "Received 401 for %s with no stored-credential refresh available",
                                        safe_request_label,
                                    )
                                raise Unauthorized()
                            if r.status in (204, 205):
                                if mark_payload_success:
                                    self._mark_payload_healthy(endpoint or None)
                                return {}
                            if r.status >= 400:
                                body_text: str | None = None
                                try:
                                    body_text = await r.text()
                                except (
                                    Exception
                                ):  # noqa: BLE001 - fall back to generic message
                                    body_text = None
                                message = _safe_response_error_message(
                                    status=int(r.status),
                                    reason=r.reason,
                                    headers=r.headers,
                                    body_text=body_text,
                                )
                                family = _request_failure_debug_family(
                                    method,
                                    endpoint or url,
                                )
                                if family is not None:
                                    params = kwargs.get("params")
                                    if isinstance(params, dict):
                                        params_summary: object = (
                                            _redact_debug_json_body(
                                                params,
                                                site_ids=(self._site,),
                                            )
                                        )
                                    elif params is None:
                                        params_summary = None
                                    else:
                                        params_summary = redact_text(
                                            params,
                                            site_ids=(self._site,),
                                            max_length=256,
                                        )
                                    payload_summary: object = None
                                    json_payload = kwargs.get("json")
                                    data_payload = kwargs.get("data")
                                    if isinstance(json_payload, dict):
                                        payload_summary = {
                                            "scheduleType": json_payload.get(
                                                "scheduleType"
                                            ),
                                            "json_keys": sorted(
                                                str(key) for key in json_payload.keys()
                                            ),
                                        }
                                    elif isinstance(json_payload, list):
                                        key_union: set[str] = set()
                                        for item in json_payload:
                                            if isinstance(item, dict):
                                                key_union.update(
                                                    str(key) for key in item.keys()
                                                )
                                        payload_summary = {
                                            "json_item_count": len(json_payload),
                                            "json_keys": sorted(key_union),
                                        }
                                    elif isinstance(data_payload, dict):
                                        payload_summary = {
                                            "data_keys": sorted(
                                                str(key) for key in data_payload.keys()
                                            )
                                        }
                                    header_flags = (
                                        self._battery_config_header_debug_flags(
                                            base_headers,
                                            auth_source_override=debug_auth_source,
                                        )
                                    )
                                    _LOGGER.debug(
                                        "%s failed for %s: status=%s params=%s payload=%s "
                                        "attempt_id=%s attempt_changes=%s header_flags=%s "
                                        "cookie_names=%s headers=%s response=%s",
                                        family,
                                        safe_request_label,
                                        r.status,
                                        params_summary,
                                        payload_summary,
                                        debug_battery_attempt_id,
                                        debug_battery_attempt_changes,
                                        header_flags,
                                        _cookie_names_from_header(
                                            base_headers.get("Cookie")
                                        ),
                                        self._redact_headers(base_headers),
                                        redact_text(
                                            body_text or message,
                                            site_ids=(self._site,),
                                            max_length=256,
                                        ),
                                    )
                                response_error = aiohttp.ClientResponseError(
                                    r.request_info,
                                    r.history,
                                    status=r.status,
                                    message=message,
                                    headers=r.headers,
                                )
                                response_error.enphase_routing_not_found = (
                                    r.status == 404
                                    and self._is_routing_not_found(body_text)
                                )
                                response_error.enphase_invalid_charge_level = (
                                    r.status == 500
                                    and self._is_invalid_charge_level_error(body_text)
                                )
                                if _is_scheduler_charging_mode_endpoint(endpoint):
                                    code, display = _scheduler_error_context_from_text(
                                        body_text
                                    )
                                    if code or display:
                                        response_error.enphase_scheduler_error = {
                                            "code": code,
                                            "display": display,
                                        }
                                raise response_error
                            try:
                                payload = await r.json()
                            except (aiohttp.ContentTypeError, ValueError) as err:
                                status = int(getattr(r, "status", 0) or 0)
                                content_type = ""
                                try:
                                    content_type = str(
                                        r.headers.get("Content-Type", "")
                                    ).strip()
                                except (
                                    Exception
                                ):  # noqa: BLE001 - defensive header parsing
                                    content_type = ""
                                try:
                                    body_text = await r.text()
                                except Exception as text_err:  # noqa: BLE001
                                    body_text = (
                                        f"<unavailable:{text_err.__class__.__name__}>"
                                    )
                                if _is_enphase_login_wall(
                                    endpoint=endpoint or None,
                                    payload=body_text,
                                ):
                                    self._last_unauthorized_request = safe_request_label
                                    raise self._login_wall_unauthorized(
                                        endpoint=endpoint or None,
                                        request_label=safe_request_label,
                                        status=status or None,
                                        content_type=content_type or None,
                                        payload=body_text,
                                    ) from err
                                failure_kind = (
                                    "content_type"
                                    if isinstance(err, aiohttp.ContentTypeError)
                                    else "json_decode"
                                )
                                raise self._invalid_payload_error(
                                    endpoint=endpoint or None,
                                    status=status or None,
                                    content_type=content_type or None,
                                    failure_kind=failure_kind,
                                    decode_error=err.__class__.__name__,
                                    payload=body_text,
                                    log_warning=log_invalid_payload,
                                ) from err
                            if mark_payload_success:
                                self._mark_payload_healthy(endpoint or None)
                            return payload

    @asynccontextmanager
    async def _request_session(self, *, cookie_header_only: bool = False):
        """Yield the HTTP session to use for a request.

        Cookie-backed BatteryConfig writes need the explicit raw Cookie header to be
        sent without any session-jar merging. A short-lived stateless session avoids
        hidden cookie mutations from the shared client while preserving the normal
        shared session for all other requests.
        """

        if not cookie_header_only:
            yield self._s
            return

        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
            yield s

    async def _text_response(
        self,
        method: str,
        url: str,
        *,
        expected_statuses: tuple[int, ...] | None = None,
        mark_payload_success: bool = True,
        **kwargs,
    ) -> TextResponse:
        """Perform an HTTP request returning text plus response metadata."""

        extra_headers = kwargs.pop("headers", None)
        attempt = 0
        request_label = _request_label(method, url)
        safe_request_label = redact_text(
            request_label, site_ids=(self._site,), max_length=256
        )
        endpoint = ""
        try:
            endpoint = URL(url).path
        except Exception:  # noqa: BLE001
            endpoint = ""
        while True:
            base_headers = dict(self._h)
            if callable(extra_headers):
                attempt_headers = extra_headers()
            else:
                attempt_headers = extra_headers
            if isinstance(attempt_headers, dict):
                for header_key, header_value in attempt_headers.items():
                    if header_value is None:
                        base_headers.pop(header_key, None)
                    else:
                        base_headers[header_key] = header_value

            async with _enlighten_read_request_guard(method, url):
                async with asyncio.timeout(self._timeout):
                    async with self._s.request(
                        method, url, headers=base_headers, **kwargs
                    ) as r:
                        if r.status == 401:
                            self._last_unauthorized_request = safe_request_label
                            if self._reauth_cb and attempt == 0:
                                attempt += 1
                                if await self._reauth_cb():
                                    continue
                            raise Unauthorized()
                        if expected_statuses and r.status in expected_statuses:
                            text = await r.text()
                            if _is_enphase_login_wall(
                                endpoint=endpoint or None, payload=text
                            ):
                                self._last_unauthorized_request = safe_request_label
                                raise self._login_wall_unauthorized(
                                    endpoint=endpoint or None,
                                    request_label=safe_request_label,
                                    status=int(r.status),
                                    content_type=r.headers.get("Content-Type"),
                                    payload=text,
                                )
                            if mark_payload_success:
                                self._mark_payload_healthy(endpoint or None)
                            return TextResponse(
                                status=int(r.status),
                                text=text,
                                url=str(r.url),
                                headers={str(k): str(v) for k, v in r.headers.items()},
                                location=r.headers.get("Location"),
                            )
                        if r.status >= 400:
                            body_text: str | None = None
                            try:
                                body_text = await r.text()
                            except Exception:  # noqa: BLE001
                                body_text = None
                            message = _safe_response_error_message(
                                status=int(r.status),
                                reason=r.reason,
                                headers=r.headers,
                                body_text=body_text,
                            )
                            raise aiohttp.ClientResponseError(
                                r.request_info,
                                r.history,
                                status=r.status,
                                message=message,
                                headers=r.headers,
                            )
                        text = await r.text()
                        if _is_enphase_login_wall(
                            endpoint=endpoint or None, payload=text
                        ):
                            self._last_unauthorized_request = safe_request_label
                            raise self._login_wall_unauthorized(
                                endpoint=endpoint or None,
                                request_label=safe_request_label,
                                status=int(r.status),
                                content_type=r.headers.get("Content-Type"),
                                payload=text,
                            )
                        if mark_payload_success:
                            self._mark_payload_healthy(endpoint or None)
                        return TextResponse(
                            status=int(r.status),
                            text=text,
                            url=str(r.url),
                            headers={str(k): str(v) for k, v in r.headers.items()},
                            location=r.headers.get("Location"),
                        )

    async def _text(
        self,
        method: str,
        url: str,
        *,
        expected_statuses: tuple[int, ...] | None = None,
        mark_payload_success: bool = True,
        **kwargs,
    ) -> str:
        """Perform an HTTP request returning text only."""

        response = await self._text_response(
            method,
            url,
            expected_statuses=expected_statuses,
            mark_payload_success=mark_payload_success,
            **kwargs,
        )
        return response.text

    async def status(self) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/status"
        endpoint = f"/service/evse_controller/{self._site}/ev_chargers/status"
        try:
            data = await self._json("GET", url, headers=self._today_headers())
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err) or _is_optional_html_payload(err):
                raise OptionalEndpointUnavailable(err.summary) from err
            raise
        if not isinstance(data, dict):
            raise self._invalid_payload_error(
                endpoint=endpoint,
                summary="EVSE status payload must be an object",
                failure_kind="shape",
                payload=data,
            )

        # If response is { data: { chargers: [...] } }, map to evChargerData
        try:
            inner = data.get("data") if isinstance(data, dict) else None
            chargers = inner.get("chargers") if isinstance(inner, dict) else None
            if isinstance(chargers, list) and chargers:
                out = []
                for c in chargers:
                    conn = (c.get("connectors") or [{}])[0]
                    raw_session = c.get("session_d")
                    sess = dict(raw_session) if isinstance(raw_session, dict) else {}
                    connectors = c.get("connectors")
                    if not connectors:
                        connectors = [conn] if conn else []
                    # Derive start_time in seconds (strt_chrg appears in ms)
                    start_raw = sess.get("start_time")
                    from_strt_chrg = False
                    if start_raw is None:
                        start_raw = sess.get("strt_chrg")
                        from_strt_chrg = start_raw is not None
                    start_sec: int | None = None
                    if isinstance(start_raw, (int, float)):
                        try:
                            start_val = int(start_raw)
                            if from_strt_chrg:
                                start_val = int(start_val / 1000)
                            elif start_val > 10**12:
                                start_val = start_val // 1000
                            start_sec = start_val
                        except Exception:
                            start_sec = None
                    elif isinstance(start_raw, str):
                        text = start_raw.strip()
                        if text.isdigit():
                            try:
                                start_val = int(text)
                                if from_strt_chrg:
                                    start_val = int(start_val / 1000)
                                elif start_val > 10**12:
                                    start_val = start_val // 1000
                                start_sec = start_val
                            except Exception:
                                start_sec = None
                    if start_sec is not None and sess.get("start_time") is None:
                        sess["start_time"] = start_sec
                    sch_raw = c.get("sch_d")
                    sch = dict(sch_raw) if isinstance(sch_raw, dict) else {}
                    smart_ev = c.get("smartEV")
                    if not isinstance(smart_ev, dict):
                        smart_ev = {}
                    out.append(
                        {
                            "sn": c.get("sn"),
                            "name": c.get("name"),
                            "displayName": c.get("displayName"),
                            "connected": bool(c.get("connected")),
                            "pluggedIn": bool(
                                c.get("pluggedIn") or conn.get("pluggedIn")
                            ),
                            "charging": bool(c.get("charging")),
                            "faulted": bool(c.get("faulted")),
                            "commissioned": c.get("commissioned"),
                            "mode": c.get("mode"),
                            "offGrid": c.get("offGrid"),
                            "offlineAt": c.get("offlineAt"),
                            "evManufacturerName": c.get("evManufacturerName"),
                            "isEVDetailsSet": c.get("isEVDetailsSet"),
                            "smartEV": smart_ev,
                            "sch_d": sch,
                            "chargingLevel": c.get("chargingLevel"),
                            "connectorStatusType": conn.get("connectorStatusType"),
                            "connectors": connectors,
                            "session_d": sess,
                        }
                    )
                return {
                    "evChargerData": out,
                    "ts": data.get("meta", {}).get("serverTimeStamp"),
                }
        except Exception:
            # If mapping fails, fall back to raw
            pass

        return data

    @staticmethod
    def _payload_has_level(payload: dict | None) -> bool:
        """Return True when a payload explicitly includes a charging level."""

        if not isinstance(payload, dict):
            return False
        return any(key in payload for key in ("chargingLevel", "charging_level"))

    def _start_charging_candidates(
        self, sn: str, level: int, connector_id: int
    ) -> list[tuple[str, str, dict | None]]:
        return [
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"chargingLevel": level, "connectorId": connector_id},
            ),
            (
                "PUT",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"chargingLevel": level, "connectorId": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/start_charging",
                {"chargingLevel": level, "connectorId": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"charging_level": level, "connector_id": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"connectorId": connector_id},
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                None,
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/start_charging",
                None,
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/start_charging",
                {"chargingLevel": level},
            ),
        ]

    async def start_charging(
        self,
        sn: str,
        amps: int,
        connector_id: int = 1,
        *,
        include_level: bool | None = None,
        strict_preference: bool = False,
    ) -> dict:
        """Start charging or set the charging level.

        The Enlighten API has variations across deployments (method, path, and payload keys).
        We try a sequence of known variants until one succeeds.
        When ``include_level`` is provided, variants that explicitly send the charging
        amps are preferred (include_level=True) or avoided (include_level=False).
        """
        level = int(amps)
        candidates = self._start_charging_candidates(sn, level, connector_id)
        if not candidates:
            raise aiohttp.ClientError("start_charging has no request candidates")

        indices = list(range(len(candidates)))
        level_indices = [
            idx for idx in indices if self._payload_has_level(candidates[idx][2])
        ]
        no_level_indices = [idx for idx in indices if idx not in level_indices]

        def _cache_for_preference() -> int | None:
            if include_level is True:
                return self._start_variant_idx_with_level
            if include_level is False:
                return self._start_variant_idx_no_level
            return self._start_variant_idx

        if include_level is True:
            order = list(level_indices)
            if not order and strict_preference:
                raise aiohttp.ClientError(
                    "No start_charging variants support charging level payloads"
                )
            if not strict_preference:
                order += no_level_indices
        elif include_level is False:
            order = list(no_level_indices)
            if not order and strict_preference:
                raise aiohttp.ClientError(
                    "No start_charging variants omit charging level payloads"
                )
            if not strict_preference:
                order += level_indices
        else:
            order = indices

        if not order:
            raise aiohttp.ClientError("No start_charging request candidates available")

        cache_idx = _cache_for_preference()
        if cache_idx is not None and cache_idx in order:
            order.remove(cache_idx)
            order.insert(0, cache_idx)

        def _record_variant(idx: int) -> None:
            payload = candidates[idx][2]
            has_level = self._payload_has_level(payload)
            if include_level is True and has_level:
                self._start_variant_idx_with_level = idx
                return
            if include_level is False and not has_level:
                self._start_variant_idx_no_level = idx
                return
            if include_level is None:
                self._start_variant_idx = idx
                return
            # Fallback: remember last working variant for general calls
            self._start_variant_idx = idx

        def _interpret_start_error(message: str) -> dict | None:
            """Return a benign response when backend reports non-fatal errors."""

            if not message:
                return None
            text = message.strip()
            if not text:
                return None
            lower = text.lower()
            if "already in charging state" in lower:
                return {"status": "already_charging"}
            if "not plugged" in lower:
                return {"status": "not_ready"}

            def _load_payload(raw: str) -> Any:
                try:
                    return json.loads(raw)
                except Exception:
                    stripped = raw.strip("\"'")
                    if stripped == raw:
                        raise
                    return json.loads(stripped)

            try:
                parsed = _load_payload(text)
            except Exception:
                return None
            if not isinstance(parsed, dict):
                return None
            error_obj = parsed.get("error") or parsed

            def _extract_code(obj: Any) -> str | None:
                if isinstance(obj, dict):
                    candidate = obj.get("errorMessageCode") or obj.get("code")
                    if isinstance(candidate, str):
                        return candidate.lower()
                return None

            def _extract_message(obj: Any) -> str | None:
                if not isinstance(obj, dict):
                    return None
                for key in ("displayMessage", "errorMessage", "message"):
                    val = obj.get(key)
                    if isinstance(val, str):
                        return val
                return None

            for candidate in (error_obj, parsed):
                code = _extract_code(candidate)
                if code == "iqevc_ms-10012":
                    return {"status": "already_charging"}
                if code == "iqevc_ms-10008":
                    return {"status": "not_ready"}
                display = _extract_message(candidate)
                if isinstance(display, str):
                    disp_lower = display.lower()
                    if "already in charging state" in disp_lower:
                        return {"status": "already_charging"}
                    if "not plugged" in disp_lower:
                        return {"status": "not_ready"}
            return None

        last_exc: Exception | None = None
        variant_failures: list[dict[str, Any]] = []
        extra_headers = self._control_headers()
        base_headers = self._today_json_headers()
        base_headers.update(extra_headers)
        for idx in order:
            method, url, payload = candidates[idx]
            headers = self._today_json_headers()
            headers.update(extra_headers)
            try:
                if payload is None:
                    result = await self._json(method, url, headers=headers)
                else:
                    result = await self._json(
                        method, url, json=payload, headers=headers
                    )
                # Cache the working variant index for future calls
                _record_variant(idx)
                return result
            except aiohttp.ClientResponseError as e:
                if e.status >= 500:
                    raise
                # 409/422 (and similar) often indicate not plugged in or not ready.
                # Treat these as benign no-ops instead of surfacing as errors.
                if e.status in (409, 422):
                    _record_variant(idx)
                    return {"status": "not_ready"}
                if e.status == 400:
                    interpreted = _interpret_start_error(e.message or "")
                    if interpreted is not None:
                        _record_variant(idx)
                        status = interpreted.get("status")
                        _LOGGER.debug(
                            "start_charging treated as benign status %s for charger %s: %s %s payload=%s; response=%s",
                            status,
                            redact_identifier(sn),
                            method,
                            redact_text(
                                url,
                                site_ids=(self._site,),
                                identifiers=(sn,),
                            ),
                            (
                                self._debug_error_message(payload, device_uid=sn)
                                if payload is not None
                                else "<no-body>"
                            ),
                            self._debug_error_message(e.message, device_uid=sn),
                        )
                        return interpreted
                    variant_failures.append(
                        {
                            "idx": idx,
                            "method": method,
                            "url": url,
                            "payload": payload if payload is not None else "<no-body>",
                            "response": e.message or "",
                            "headers": self._redact_headers(base_headers),
                        }
                    )
                # 400/404/405 variations likely indicate method/path mismatch; try next.
                last_exc = e
                continue
        if last_exc:
            if (
                isinstance(last_exc, aiohttp.ClientResponseError)
                and last_exc.status == 400
                and variant_failures
            ):
                sample = variant_failures[0]
                attempted = ", ".join(
                    f"{item['method']} idx {item['idx']}"
                    for item in variant_failures[1:]
                )
                attempt_suffix = (
                    f"; other variants tried: {attempted}" if attempted else ""
                )
                _LOGGER.warning(
                    "start_charging rejected (400) for charger %s: %s %s payload=%s; headers=%s; response=%s%s",
                    redact_identifier(sn),
                    sample["method"],
                    redact_text(
                        sample["url"],
                        site_ids=(self._site,),
                        identifiers=(sn,),
                    ),
                    self._debug_error_message(sample["payload"], device_uid=sn),
                    sample["headers"],
                    self._debug_error_message(sample["response"], device_uid=sn),
                    attempt_suffix,
                )
            raise last_exc
        # Should not happen, but keep static analyzer happy
        raise aiohttp.ClientError(
            "start_charging failed with all variants"
        )  # pragma: no cover

    def _stop_charging_candidates(self, sn: str) -> list[tuple[str, str, dict | None]]:
        return [
            (
                "PUT",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/stop_charging",
                None,
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/{sn}/stop_charging",
                None,
            ),
            (
                "POST",
                f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/stop_charging",
                None,
            ),
        ]

    @staticmethod
    def _is_routing_not_found(message: str | None) -> bool:
        """Return True when a 404 is a routing miss rather than action state."""

        if not message:
            return False
        text = message.strip()
        lower = text.lower()
        if "no static resource" in lower:
            return True
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return False
        if not isinstance(parsed, dict):
            return False
        detail_parts = [
            parsed.get("detail"),
            parsed.get("title"),
            parsed.get("message"),
            parsed.get("error"),
        ]
        return any(
            isinstance(part, str) and "no static resource" in part.lower()
            for part in detail_parts
        )

    @staticmethod
    def _is_invalid_charge_level_error(message: str | None) -> bool:
        """Return True when a response reports an invalid charge level."""

        if not message:
            return False
        return "invalid charge level" in message.lower()

    async def stop_charging(self, sn: str) -> dict:
        """Stop charging; try multiple endpoint variants."""
        candidates = self._stop_charging_candidates(sn)
        order = list(range(len(candidates)))
        if self._stop_variant_idx is not None and 0 <= self._stop_variant_idx < len(
            candidates
        ):
            order.remove(self._stop_variant_idx)
            order.insert(0, self._stop_variant_idx)

        last_exc: Exception | None = None
        extra_headers = self._control_headers()
        for idx in order:
            method, url, payload = candidates[idx]
            headers = self._today_json_headers()
            headers.update(extra_headers)
            try:
                if payload is None:
                    result = await self._json(method, url, headers=headers)
                else:
                    result = await self._json(
                        method, url, json=payload, headers=headers
                    )
                self._stop_variant_idx = idx
                return result
            except aiohttp.ClientResponseError as e:
                if e.status >= 500:
                    raise
                if e.status == 404 and (
                    getattr(e, "enphase_routing_not_found", False)
                    or self._is_routing_not_found(e.message)
                ):
                    last_exc = e
                    continue
                # If charger is not plugged in or already stopped, some backends
                # respond with 400/404/409. Treat these as benign no-ops.
                if e.status in (400, 404, 409, 422):
                    return {"status": "not_active"}
                last_exc = e
                continue
        if last_exc:
            raise last_exc
        raise aiohttp.ClientError("stop_charging failed with all variants")

    async def trigger_message(self, sn: str, requested_message: str) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_charger/{sn}/trigger_message"
        payload = {"requestedMessage": validate_ocpp_trigger_message(requested_message)}
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        return await self._json("POST", url, json=payload, headers=headers)

    async def start_live_stream(self) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/start_live_stream"
        headers = self._today_headers()
        headers.update(self._control_headers())
        return await self._json("GET", url, headers=headers)

    async def stop_live_stream(self) -> dict:
        url = f"{BASE_URL}/service/evse_controller/{self._site}/ev_chargers/stop_live_stream"
        headers = self._today_headers()
        headers.update(self._control_headers())
        return await self._json("GET", url, headers=headers)

    async def charge_mode(self, sn: str) -> str | None:
        """Fetch the current charge mode via scheduler API.

        GET /service/evse_scheduler/api/v1/iqevc/charging-mode/<site>/<sn>/preference
        Requires Authorization: Bearer <jwt> in addition to existing cookies.
        Returns one of: SMART_CHARGING, GREEN_CHARGING, SCHEDULED_CHARGING,
        MANUAL_CHARGING when enabled.
        """
        url = f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/{self._site}/{sn}/preference"
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        try:
            data = await self._json("GET", url, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise
        try:
            modes = (data.get("data") or {}).get("modes") or {}
            # Prefer the mode whose 'enabled' is true
            for key in (
                "smartCharging",
                "greenCharging",
                "scheduledCharging",
                "manualCharging",
            ):
                m = modes.get(key)
                if isinstance(m, dict) and m.get("enabled"):
                    return m.get("chargingMode")
        except Exception:
            return None
        return None

    async def set_charge_mode(
        self, sn: str, mode: str, *, previous_mode: str | None = None
    ) -> dict:
        """Set the charging mode via scheduler API.

        PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/<site>/<sn>/preference
        Body: { "mode": "MANUAL_CHARGING" | "SCHEDULED_CHARGING" |
        "GREEN_CHARGING" | "SMART_CHARGING" }
        """
        url = f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/{self._site}/{sn}/preference"
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        normalized_mode = str(mode)
        observed_previous_mode = str(previous_mode) if previous_mode else None
        if observed_previous_mode is None:
            try:
                observed_previous_mode = await self.charge_mode(sn)
            except SchedulerUnavailable:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                observed_previous_mode = None
        if observed_previous_mode == normalized_mode:
            return {
                "status": "already_set",
                "mode": normalized_mode,
            }
        payload = {"mode": normalized_mode}
        try:
            return await self._json("PUT", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            if (
                err.status == 400
                and observed_previous_mode is not None
                and observed_previous_mode != normalized_mode
                and _scheduler_error_code(err) != "iqevc_sch_10031"
                and await self._charge_mode_write_landed(sn, normalized_mode)
            ):
                return {
                    "status": "accepted",
                    "mode": normalized_mode,
                    "verified_after_error": True,
                }
            raise

    async def _charge_mode_write_landed(self, sn: str, mode: str) -> bool:
        """Return True if a failed preference write is visible on read-back."""

        for attempt in range(6):
            if attempt:
                await asyncio.sleep(2)
            try:
                current_mode = await self.charge_mode(sn)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return False
            if current_mode == mode:
                return True
        return False

    async def green_charging_settings(self, sn: str) -> list[dict[str, Any]]:
        """Return green charging settings for the charger.

        GET /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/<site>/<sn>/settings
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"GREEN_CHARGING/{self._site}/{sn}/settings"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        try:
            payload = await self._json("GET", url, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def set_green_battery_setting(self, sn: str, *, enabled: bool) -> dict:
        """Toggle green charging battery support.

        PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/<site>/<sn>/settings
        Body: {
          "chargerSettingList": [
            { "chargerSettingName": "USE_BATTERY_FOR_SELF_CONSUMPTION", "enabled": true }
          ]
        }
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"GREEN_CHARGING/{self._site}/{sn}/settings"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        payload = {
            "chargerSettingList": [
                {
                    "chargerSettingName": GREEN_BATTERY_SETTING,
                    "enabled": bool(enabled),
                    "value": None,
                    "loader": False,
                }
            ]
        }
        try:
            return await self._json("PUT", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

    async def storm_guard_alert(self) -> dict:
        """Return Storm Guard alert status for the site.

        GET /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
        """
        url = f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/{self._site}/stormAlert"
        return await self._battery_config_request("GET", url)

    async def opt_out_storm_alert(self, *, alert_id: str, name: str) -> dict:
        """Opt out of a specific Storm Guard alert.

        PUT /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
        Body: {
          "stormAlerts": [
            {"id": "<alert_id>", "name": "<alert_name>", "status": "opted-out"}
          ]
        }
        """
        url = f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/{self._site}/stormAlert"
        headers = self._battery_config_headers(include_xsrf=True)
        payload = {
            "stormAlerts": [
                {
                    "id": str(alert_id),
                    "name": str(name),
                    "status": "opted-out",
                }
            ]
        }
        return await self._json("PUT", url, json=payload, headers=headers)

    async def storm_guard_profile(self, *, locale: str | None = None) -> dict:
        """Return Storm Guard state and EVSE settings for the site.

        GET /service/batteryConfig/api/v1/profile/<site_id>?source=enho&userId=<user_id>&locale=<locale>
        """
        return await self.battery_profile_details(locale=locale)

    async def battery_site_settings(self) -> dict:
        """Return BatteryConfig site settings and feature flags."""

        url = f"{BASE_URL}/service/batteryConfig/api/v1/siteSettings/{self._site}"
        params = self._battery_config_params()
        return await self._battery_config_request("GET", url, params=params)

    async def site_tariff_billing_details(self) -> dict:
        """Return site tariff billing-cycle details."""

        url = (
            f"{BASE_URL}/service/tariff/tariff-ms/systems/{self._site}/billing-details"
        )
        return await self._json("GET", url, headers=self._tariff_headers())

    async def site_tariff_billing_update(
        self,
        payload: dict[str, Any],
        *,
        request_date: date | datetime | str | None = None,
    ) -> dict:
        """Update site tariff billing-cycle details."""

        if request_date is None:
            request_date_text = date.today().isoformat()
        elif isinstance(request_date, datetime):
            request_date_text = request_date.date().isoformat()
        elif isinstance(request_date, date):
            request_date_text = request_date.isoformat()
        else:
            request_date_text = str(request_date)
        url = (
            f"{BASE_URL}/service/tariff/tariff-ms/systems/{self._site}/billing-details"
        )
        return await self._json(
            "POST",
            url,
            json=payload,
            params={"date": request_date_text},
            headers=self._tariff_headers(write=True),
        )

    async def site_tariff(self) -> dict:
        """Return site import/export tariff configuration."""

        url = f"{BASE_URL}/service/tariff/tariff-ms/systems/{self._site}/tariff"
        return await self._json(
            "GET",
            url,
            params={"include-site-details": "true"},
            headers=self._tariff_headers(),
        )

    async def site_tariff_rates(
        self,
        *,
        rate_type: str,
        request_date: date | datetime | str | None = None,
    ) -> dict:
        """Return dated tariff rates for a site tariff branch."""

        if request_date is None:
            request_date_text = date.today().isoformat()
        elif isinstance(request_date, datetime):
            request_date_text = request_date.date().isoformat()
        elif isinstance(request_date, date):
            request_date_text = request_date.isoformat()
        else:
            request_date_text = str(request_date)
        url = f"{BASE_URL}/service/tariff/tariff-ms/systems/{self._site}/tariffs"
        return await self._json(
            "GET",
            url,
            params={
                "rateType": str(rate_type).upper(),
                "date": request_date_text,
                "includeUtility": "",
            },
            headers=self._tariff_headers(),
        )

    async def site_tariff_bundle(self) -> tuple[dict, dict]:
        """Return billing details and tariff configuration for the site."""

        try:
            async with asyncio.TaskGroup() as task_group:
                billing_task = task_group.create_task(
                    self.site_tariff_billing_details(),
                    name="enphase_ev_site_tariff_billing",
                )
                tariff_task = task_group.create_task(
                    self.site_tariff(),
                    name="enphase_ev_site_tariff_config",
                )
        except ExceptionGroup as err:
            raise err.exceptions[0] from err
        return billing_task.result(), tariff_task.result()

    async def site_tariff_update(self, payload: dict[str, Any]) -> dict:
        """Update site import/export tariff configuration."""

        _token, user_id = self._battery_config_auth_context()
        url = f"{BASE_URL}/service/tariff/tariff-ms/systems/{self._site}/tariff"
        params = {"user-id": user_id} if user_id else None
        return await self._json(
            "PUT",
            url,
            json=payload,
            params=params,
            headers=self._tariff_headers(write=True),
        )

    async def notify_tariff_change(self) -> dict:
        """Notify the EVSE scheduler service that site tariff data changed."""

        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/siteConfig/"
            f"{self._site}/tariff_change"
        )
        return await self._json(
            "PUT",
            url,
            json=None,
            headers=self._tariff_headers(write=True),
        )

    async def battery_profile_details(self, *, locale: str | None = None) -> dict:
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

    async def battery_settings_details(self) -> dict:
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
        self, disclaimer_type: str = "itc"
    ) -> dict:
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
        self,
        payload: dict[str, Any],
        *,
        schedule_type: str = "cfg",
    ) -> dict:
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
        self,
        payload: dict[str, Any],
        *,
        schedule_type: str = "cfg",
        include_source: bool = True,
        merged_payload: bool = False,
        strip_devices: bool = False,
    ) -> dict:
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
        )

    async def set_battery_profile(
        self,
        *,
        profile: str,
        battery_backup_percentage: int,
        operation_mode_sub_type: str | None = None,
        devices: list[dict[str, Any]] | None = None,
    ) -> dict:
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

    async def cancel_battery_profile_update(self) -> dict:
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

    async def set_storm_guard(self, *, enabled: bool, evse_enabled: bool) -> dict:
        """Toggle Storm Guard and the EVSE charge-to-100% option.

        PUT /service/batteryConfig/api/v1/stormGuard/toggle/<site_id>?userId=<user_id>
        """
        url = f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/toggle/{self._site}"
        params = self._battery_config_params(include_source=True)
        payload = {
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

    # ------------------------------------------------------------------
    # Battery schedule CRUD (newer /battery/sites/{id}/schedules API)
    # ------------------------------------------------------------------

    async def battery_schedules(self) -> dict:
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
        self,
        *,
        schedule_type: str,
        start_time: str,
        end_time: str,
        limit: int | None,
        days: list[int],
        timezone: str = "UTC",
        is_enabled: bool | None = None,
    ) -> dict:
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
        payload = {
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
    ) -> dict:
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
        payload = {
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
        self,
        schedule_id: str | int,
        *,
        schedule_type: str = "cfg",
    ) -> dict:
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

    async def validate_battery_schedule(self, schedule_type: str = "cfg") -> dict:
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

    async def charger_auth_settings(self, sn: str) -> list[dict[str, Any]]:
        """Return authentication settings for the charger.

        POST /service/evse_controller/api/v1/<site>/<sn>/ev_charger_config
        Body: [{ "key": "rfidSessionAuthentication" }, { "key": "sessionAuthentication" }]
        """
        url = (
            f"{BASE_URL}/service/evse_controller/api/v1/{self._site}/ev_chargers/"
            f"{sn}/ev_charger_config"
        )
        try:
            return await self.charger_config(
                sn,
                [AUTH_RFID_SETTING, AUTH_APP_SETTING],
            )
        except aiohttp.ClientResponseError as err:
            if is_auth_settings_unavailable_error(err.message, err.status, url):
                raise AuthSettingsUnavailable(str(err)) from err
            raise

    async def charger_config(
        self,
        sn: str,
        keys: Iterable[str],
    ) -> list[dict[str, Any]]:
        """Return raw charger config entries for the requested keys."""

        normalized_keys: list[str] = []
        seen: set[str] = set()
        for key in keys:
            try:
                key_text = str(key).strip()
            except Exception:
                continue
            if not key_text or key_text in seen:
                continue
            seen.add(key_text)
            normalized_keys.append(key_text)
        if not normalized_keys:
            return []

        url = (
            f"{BASE_URL}/service/evse_controller/api/v1/{self._site}/ev_chargers/"
            f"{sn}/ev_charger_config"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        payload = [{"key": key} for key in normalized_keys]

        async def _retry_without_control_auth() -> dict[str, Any]:
            retry_headers = self._today_json_headers()
            retry_headers["Authorization"] = None
            retry_headers["e-auth-token"] = None
            return await self._json(
                "POST",
                url,
                json=payload,
                headers=retry_headers,
            )

        try:
            response = await self._json("POST", url, json=payload, headers=headers)
        except Unauthorized:
            if headers.get("Authorization"):
                response = await _retry_without_control_auth()
            else:
                raise
        except aiohttp.ClientResponseError as err:
            if err.status == 403 and headers.get("Authorization"):
                response = await _retry_without_control_auth()
            else:
                raise
        if not isinstance(response, dict):
            return []
        data = response.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def set_app_authentication(self, sn: str, *, enabled: bool) -> dict:
        """Enable or disable session authentication via app.

        PUT /service/evse_controller/api/v1/<site>/<sn>/ev_charger_config
        Body: [{ "key": "sessionAuthentication", "value": "enabled" | "disabled" }]
        """
        url = (
            f"{BASE_URL}/service/evse_controller/api/v1/{self._site}/ev_chargers/"
            f"{sn}/ev_charger_config"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        payload = [
            {
                "key": AUTH_APP_SETTING,
                "value": "enabled" if enabled else "disabled",
            }
        ]
        try:
            return await self._json("PUT", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_auth_settings_unavailable_error(err.message, err.status, url):
                raise AuthSettingsUnavailable(str(err)) from err
            raise

    async def set_default_charge_level(self, sn: str, amps: int) -> dict:
        """Set the charger's stored default charge level.

        PUT /service/evse_controller/api/v1/<site>/<sn>/ev_charger_config
        Body: [{ "key": "DefaultChargeLevel", "value": <amps> }]
        """
        url = (
            f"{BASE_URL}/service/evse_controller/api/v1/{self._site}/ev_chargers/"
            f"{sn}/ev_charger_config"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        payload = [{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": int(amps)}]
        try:
            response = await self._json("PUT", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_auth_settings_unavailable_error(err.message, err.status, url):
                raise ChargerConfigUnavailable(str(err)) from err
            raise
        return response if isinstance(response, dict) else {}

    async def get_schedules(self, sn: str) -> dict:
        """Return scheduler config and slots for the charger.

        GET /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedules
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"SCHEDULED_CHARGING/{self._site}/{sn}/schedules"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        try:
            payload = await self._json("GET", url, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise
        if not isinstance(payload, dict):
            return {"meta": None, "config": None, "slots": []}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            "meta": payload.get("meta"),
            "config": (data or {}).get("config"),
            "slots": (data or {}).get("slots") or [],
        }

    async def patch_schedules(
        self, sn: str, *, server_timestamp: str, slots: list[dict]
    ) -> dict:
        """Patch the scheduler slots for the charger.

        PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedules
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"SCHEDULED_CHARGING/{self._site}/{sn}/schedules"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        payload = {
            "meta": {"serverTimeStamp": server_timestamp, "rowCount": len(slots)},
            "data": slots,
        }
        try:
            return await self._json("PATCH", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

    async def patch_schedule_states(
        self, sn: str, *, slot_states: dict[str, bool]
    ) -> dict:
        """Patch schedule slot enabled states for the charger.

        PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedules
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"SCHEDULED_CHARGING/{self._site}/{sn}/schedules"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        payload = {
            str(slot_id): "ENABLED" if enabled else "DISABLED"
            for slot_id, enabled in slot_states.items()
        }
        try:
            return await self._json("PATCH", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

    async def patch_schedule(self, sn: str, slot_id: str, slot: dict) -> dict:
        """Patch a single schedule slot for the charger.

        PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedule/<slot_id>
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"SCHEDULED_CHARGING/{self._site}/{sn}/schedule/{slot_id}"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        try:
            return await self._json("PATCH", url, json=slot, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

    async def create_schedule(self, sn: str, slot: dict) -> dict:
        """Create a single schedule slot for the charger.

        POST /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedule
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"SCHEDULED_CHARGING/{self._site}/{sn}/schedule"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        try:
            return await self._json("POST", url, json=slot, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

    async def delete_schedule(self, sn: str, slot_id: str) -> dict:
        """Delete a single schedule slot for the charger.

        DELETE /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site>/<sn>/schedule/<slot_id>
        """
        url = (
            f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
            f"SCHEDULED_CHARGING/{self._site}/{sn}/schedule/{slot_id}"
        )
        headers = self._today_json_headers()
        headers.update(self._control_headers())
        try:
            return await self._json("DELETE", url, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_scheduler_unavailable_error(err.message, err.status, url):
                raise SchedulerUnavailable(str(err)) from err
            raise

    async def lifetime_energy(self) -> dict | None:
        """Return lifetime energy buckets for the configured site.

        GET /pv/systems/<site_id>/lifetime_energy
        """
        url = f"{BASE_URL}/pv/systems/{self._site}/lifetime_energy"
        try:
            data = await self._json("GET", url, headers=self._layout_headers())
        except aiohttp.ClientResponseError as err:
            if is_site_energy_unavailable_error(err.message, err.status, url):
                raise SiteEnergyUnavailable(str(err)) from err
            raise
        return self._normalize_lifetime_energy_payload(data)

    @classmethod
    def _normalize_latest_power_payload(
        cls, payload: object
    ) -> dict[str, object] | None:
        """Normalize app-api latest power payloads into a common shape."""

        return api_parsers.normalize_latest_power_payload(payload)

    async def latest_power(self) -> dict[str, object] | None:
        """Return the latest site power sample for the configured site.

        GET /app-api/<site_id>/get_latest_power
        """

        url = f"{BASE_URL}/app-api/{self._site}/get_latest_power"
        data = await self._json("GET", url, headers=self._history_headers())
        normalized = self._normalize_latest_power_payload(data)
        if normalized is not None:
            return normalized

        top_level_keys: list[str] = []
        nested_keys: list[str] = []
        payload_type = type(data).__name__
        if isinstance(data, dict):
            top_level_keys = sorted(str(key) for key in data.keys())
            nested = data.get("latest_power")
            if not isinstance(nested, dict):
                candidate = data.get("data")
                if isinstance(candidate, dict):
                    nested = candidate.get("latest_power")
                    if not isinstance(nested, dict):
                        nested = candidate
            if isinstance(nested, dict):
                nested_keys = sorted(str(key) for key in nested.keys())

        _LOGGER.debug(
            "Invalid latest power payload for site %s (payload_type=%s, top_level_keys=%s, nested_keys=%s)",
            redact_site_id(self._site),
            payload_type,
            top_level_keys,
            nested_keys,
        )
        return None

    async def show_livestream(
        self, *, allow_reauth: bool = True
    ) -> dict[str, object] | None:
        """Return live-status/vitals capability flags when available."""

        url = f"{BASE_URL}/app-api/{self._site}/show_livestream"
        try:
            data = await self._json(
                "GET",
                url,
                headers=self._system_dashboard_headers(),
                allow_reauth=allow_reauth,
            )
        except Unauthorized:
            if not allow_reauth:
                raise
            return None
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                return None
            raise
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403, 404):
                if not allow_reauth and err.status in (401, 403):
                    raise
                return None
            raise
        return data if isinstance(data, dict) else None

    @staticmethod
    def _normalize_evse_timeseries_serial(value: object) -> str | None:
        return api_parsers.normalize_evse_timeseries_serial(value)

    @staticmethod
    def _parse_evse_timeseries_date_key(value: object) -> str | None:
        return api_parsers.parse_evse_timeseries_date_key(value)

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
        """Return EVSE daily timeseries keyed by charger serial."""

        request_id = request_id or str(uuid.uuid4())
        if username is None:
            username = self._session_history_username()
        start_date_key = self._parse_evse_timeseries_date_key(start_date)
        if start_date_key is None:
            start_date_key = datetime.now(timezone.utc).date().isoformat()
        query = {
            "site_id": self._site,
            "source": "evse",
            "requestId": request_id,
            "start_date": start_date_key,
        }
        if username:
            query["username"] = username
        url = URL(
            f"{BASE_URL}/service/timeseries/evse/timeseries/daily_energy"
        ).with_query(query)
        headers = self._evse_timeseries_headers(request_id, username)
        try:
            data = await self._json("GET", str(url), headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_evse_timeseries_unavailable_error(err.message, err.status, url):
                raise EVSETimeseriesUnavailable(str(err)) from err
            raise
        return self._normalize_evse_timeseries_payload(data, daily=True)

    async def evse_timeseries_lifetime_energy(
        self,
        *,
        request_id: str | None = None,
        username: str | None = None,
    ) -> dict[str, dict[str, object]] | None:
        """Return EVSE lifetime timeseries keyed by charger serial."""

        request_id = request_id or str(uuid.uuid4())
        if username is None:
            username = self._session_history_username()
        query = {"site_id": self._site, "source": "evse", "requestId": request_id}
        if username:
            query["username"] = username
        url = URL(
            f"{BASE_URL}/service/timeseries/evse/timeseries/lifetime_energy"
        ).with_query(query)
        headers = self._evse_timeseries_headers(request_id, username)
        try:
            data = await self._json("GET", str(url), headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_evse_timeseries_unavailable_error(err.message, err.status, url):
                raise EVSETimeseriesUnavailable(str(err)) from err
            raise
        return self._normalize_evse_timeseries_payload(data, daily=False)

    @classmethod
    def _coerce_non_boolean_number(cls, value: object) -> float | None:
        """Normalize numeric values while rejecting JSON booleans."""

        return api_parsers.coerce_non_boolean_number(value)

    @classmethod
    def _normalize_lifetime_energy_payload(cls, payload: object) -> dict | None:
        """Normalize site/HEMS lifetime-energy payloads into a common shape."""

        return api_parsers.normalize_lifetime_energy_payload(payload)

    async def hems_consumption_lifetime(self) -> dict | None:
        """Return HEMS lifetime consumption buckets when available.

        GET /systems/<site_id>/hems_consumption_lifetime
        """

        url = f"{BASE_URL}/systems/{self._site}/hems_consumption_lifetime"
        try:
            data = await self._json(
                "GET",
                url,
                headers=self._systems_json_headers(),
                log_invalid_payload=False,
                allow_reauth=False,
            )
            self._hems_site_supported = True
        except Unauthorized:
            _LOGGER.debug(
                "HEMS lifetime endpoint unavailable for site %s (unauthorized)",
                redact_site_id(self._site),
            )
            raise
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                _LOGGER.debug(
                    "HEMS lifetime endpoint unavailable for site %s (%s)",
                    redact_site_id(self._site),
                    redact_text(err.summary, site_ids=(self._site,)),
                )
                return None
            self._log_invalid_payload(err)
            raise
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                _LOGGER.debug(
                    "HEMS lifetime endpoint auth failure for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                raise
            if err.status == 404 or _is_hems_invalid_site_error(err):
                if _is_hems_invalid_site_error(err):
                    self._hems_site_supported = False
                _LOGGER.debug(
                    "HEMS lifetime endpoint unavailable for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                return None
            raise
        return self._normalize_lifetime_energy_payload(data)

    @staticmethod
    def _clean_optional_text(value: object) -> str | None:
        """Return a trimmed string value when present."""
        return api_parsers.clean_optional_text(value)

    @classmethod
    def _heatpump_sg_ready_mode_details(cls, value: object) -> dict[str, object]:
        """Map raw HEMS SG Ready mode labels to app-facing semantics."""

        return api_parsers.heatpump_sg_ready_mode_details(value)

    @classmethod
    def _normalize_hems_heatpump_state_payload(cls, payload: object) -> dict | None:
        """Normalize HEMS heat-pump runtime state payloads."""

        return api_parsers.normalize_hems_heatpump_state_payload(payload)

    @classmethod
    def _normalize_hems_daily_consumption_entry(
        cls, payload: object
    ) -> dict[str, object] | None:
        """Normalize a HEMS daily-consumption device entry."""

        return api_parsers.normalize_hems_daily_consumption_entry(payload)

    @classmethod
    def _normalize_hems_energy_consumption_payload(cls, payload: object) -> dict | None:
        """Normalize HEMS daily energy-consumption payloads."""

        return api_parsers.normalize_hems_energy_consumption_payload(payload)

    @classmethod
    def _normalize_pv_system_today_payload(cls, payload: object) -> dict | None:
        """Normalize site-today payloads used by heat-pump daily totals."""

        return api_parsers.normalize_pv_system_today_payload(payload)

    async def hems_heatpump_state(
        self, device_uid: str, *, timezone: str | None = None
    ) -> dict | None:
        """Return HEMS heat-pump runtime state when available."""

        device_uid = str(device_uid or "").strip()
        if not device_uid:
            return None
        url = URL(
            f"https://hems-integration.enphaseenergy.com/api/v1/hems/{self._site}/heatpump/{device_uid}/state"
        )
        if timezone:
            url = url.update_query({"timezone": str(timezone).strip()})
        try:
            data = await self._json(
                "GET",
                str(url),
                headers=self._hems_headers,
                allow_reauth=False,
            )
            self._hems_site_supported = True
        except Unauthorized:
            _LOGGER.debug(
                "HEMS heat pump state endpoint unavailable for site %s (unauthorized)",
                redact_site_id(self._site),
            )
            raise
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                _LOGGER.debug(
                    "HEMS heat pump state endpoint unavailable for site %s (%s)",
                    redact_site_id(self._site),
                    redact_text(err.summary, site_ids=(self._site,)),
                )
                return None
            raise
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                _LOGGER.debug(
                    "HEMS heat pump state endpoint auth failure for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                raise
            if err.status == 404 or _is_hems_invalid_site_error(err):
                if _is_hems_invalid_site_error(err):
                    self._hems_site_supported = False
                _LOGGER.debug(
                    "HEMS heat pump state endpoint unavailable for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                return None
            raise
        return self._normalize_hems_heatpump_state_payload(data)

    async def hems_energy_consumption(
        self,
        *,
        start_at: str,
        end_at: str,
        timezone: str,
        step: str = "P1D",
    ) -> dict | None:
        """Return HEMS daily device energy-consumption buckets when available."""

        url = str(
            URL(
                f"https://hems-integration.enphaseenergy.com/api/v1/hems/{self._site}/energy-consumption"
            ).update_query(
                {
                    "from": start_at,
                    "to": end_at,
                    "timezone": timezone,
                    "step": step,
                }
            )
        )
        try:
            data = await self._json(
                "GET",
                url,
                headers=self._hems_headers,
                allow_reauth=False,
            )
            self._hems_site_supported = True
        except Unauthorized:
            _LOGGER.debug(
                "HEMS energy consumption endpoint unavailable for site %s (unauthorized)",
                redact_site_id(self._site),
            )
            raise
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                _LOGGER.debug(
                    "HEMS energy consumption endpoint unavailable for site %s (%s)",
                    redact_site_id(self._site),
                    redact_text(err.summary, site_ids=(self._site,)),
                )
                return None
            raise
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                _LOGGER.debug(
                    "HEMS energy consumption endpoint auth failure for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                raise
            if err.status == 404 or _is_hems_invalid_site_error(err):
                if _is_hems_invalid_site_error(err):
                    self._hems_site_supported = False
                _LOGGER.debug(
                    "HEMS energy consumption endpoint unavailable for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                return None
            raise
        return self._normalize_hems_energy_consumption_payload(data)

    async def pv_system_today(self, *, allow_reauth: bool = True) -> dict | None:
        """Return the site today payload when available."""

        url = f"{BASE_URL}/pv/systems/{self._site}/today"
        try:
            data = await self._json(
                "GET",
                url,
                headers=self._today_json_headers,
                allow_reauth=allow_reauth,
            )
        except Unauthorized:
            if not allow_reauth:
                raise
            _LOGGER.debug(
                "PV site today endpoint unavailable for site %s (unauthorized)",
                redact_site_id(self._site),
            )
            return None
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                _LOGGER.debug(
                    "PV site today endpoint unavailable for site %s (%s)",
                    redact_site_id(self._site),
                    redact_text(err.summary, site_ids=(self._site,)),
                )
                return None
            raise
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403, 404):
                if not allow_reauth and err.status in (401, 403):
                    raise
                _LOGGER.debug(
                    "PV site today endpoint unavailable for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                return None
            raise
        return self._normalize_pv_system_today_payload(data)

    async def heat_pump_events_json(self, device_uid: str) -> dict | list | None:
        """Return per-device HEMS heat-pump events payload when available."""

        if not str(device_uid or "").strip():
            return None
        url = str(
            URL(f"{BASE_URL}/systems/{self._site}/heat_pump/{device_uid}/events.json")
        )
        try:
            data = await self._json(
                "GET",
                url,
                headers=self._systems_json_headers(),
                log_invalid_payload=False,
                allow_reauth=False,
            )
        except Unauthorized:
            _LOGGER.debug(
                "Heat pump events endpoint unavailable for site %s (unauthorized)",
                redact_site_id(self._site),
            )
            raise
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                _LOGGER.debug(
                    "Heat pump events endpoint unavailable for site %s (%s)",
                    redact_site_id(self._site),
                    redact_text(err.summary, site_ids=(self._site,)),
                )
                return None
            if _is_optional_html_payload(err):
                raise OptionalEndpointUnavailable(err.summary) from err
            self._log_invalid_payload(err)
            raise
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                _LOGGER.debug(
                    "Heat pump events endpoint auth failure for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                raise
            if err.status == 404 or _is_hems_invalid_site_error(err):
                _LOGGER.debug(
                    "Heat pump events endpoint unavailable for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                return None
            raise
        if isinstance(data, (dict, list)):
            return data
        return None

    async def iq_er_events_json(self, device_uid: str) -> dict | list | None:
        """Return per-device HEMS IQ Energy Router events payload when available."""

        if not str(device_uid or "").strip():
            return None
        url = str(
            URL(f"{BASE_URL}/systems/{self._site}/iq_er/{device_uid}/events.json")
        )
        try:
            data = await self._json(
                "GET",
                url,
                headers=self._systems_json_headers(),
                log_invalid_payload=False,
                allow_reauth=False,
            )
        except Unauthorized:
            _LOGGER.debug(
                "IQ Energy Router events endpoint unavailable for site %s (unauthorized)",
                redact_site_id(self._site),
            )
            raise
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                _LOGGER.debug(
                    "IQ Energy Router events endpoint unavailable for site %s (%s)",
                    redact_site_id(self._site),
                    redact_text(err.summary, site_ids=(self._site,)),
                )
                return None
            if _is_optional_html_payload(err):
                raise OptionalEndpointUnavailable(err.summary) from err
            self._log_invalid_payload(err)
            raise
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                _LOGGER.debug(
                    "IQ Energy Router events endpoint auth failure for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                raise
            if err.status == 404 or _is_hems_invalid_site_error(err):
                _LOGGER.debug(
                    "IQ Energy Router events endpoint unavailable for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                return None
            raise
        if isinstance(data, (dict, list)):
            return data
        return None

    async def summary_v2(self) -> list[dict] | None:
        """Fetch charger summary v2 list.

        GET /service/evse_controller/api/v2/<site_id>/ev_chargers/summary?filter_retired=true
        Returns a list of charger objects with serialNumber and other properties.
        """
        url = f"{BASE_URL}/service/evse_controller/api/v2/{self._site}/ev_chargers/summary?filter_retired=true"
        try:
            data = await self._json("GET", url, headers=self._today_headers())
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err) or _is_optional_html_payload(err):
                raise OptionalEndpointUnavailable(err.summary) from err
            raise
        try:
            return data.get("data") or []
        except Exception:
            return None

    async def evse_fw_details(self) -> list[dict[str, Any]] | None:
        """Fetch EVSE firmware details for the current site.

        GET /service/evse_management/fwDetails/<site_id>
        Returns a list of charger firmware-detail objects keyed by serialNumber.
        """

        url = f"{BASE_URL}/service/evse_management/fwDetails/{self._site}"
        try:
            data = await self._json(
                "GET",
                url,
                headers=self._today_headers(),
                mark_payload_success=False,
            )
        except Unauthorized:
            _LOGGER.debug(
                "EVSE firmware details endpoint unavailable for site %s (unauthorized)",
                redact_site_id(self._site),
            )
            return None
        except aiohttp.ClientResponseError as err:
            if err.status in (403, 404):
                _LOGGER.debug(
                    "EVSE firmware details endpoint unavailable for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                return None
            raise

        endpoint = f"/service/evse_management/fwDetails/{self._site}"
        if data is None:
            self._mark_payload_healthy(endpoint)
            return []
        if isinstance(data, list):
            self._mark_payload_healthy(endpoint)
            return [item for item in data if isinstance(item, dict)]
        raise self._invalid_payload_error(
            endpoint=endpoint,
            summary="EVSE firmware details payload must be a list",
            failure_kind="shape",
            payload=data,
        )

    async def evse_feature_flags(self, *, country: str | None = None) -> dict | None:
        """Return EVSE feature flags and UI gating details for the site.

        GET /service/evse_management/api/v1/config/feature-flags?site_id=<site_id>[&country=<country>]
        """

        url = str(
            URL(
                f"{BASE_URL}/service/evse_management/api/v1/config/feature-flags"
            ).update_query(
                {
                    key: value
                    for key, value in {
                        "site_id": self._site,
                        "country": country,
                    }.items()
                    if value is not None
                }
            )
        )
        try:
            data = await self._json("GET", url, headers=self._today_headers())
        except EnphaseLoginWallUnauthorized:
            raise
        except Unauthorized:
            _LOGGER.debug(
                "EVSE feature flags endpoint unavailable for site %s (unauthorized)",
                redact_site_id(self._site),
            )
            return None
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403, 404):
                _LOGGER.debug(
                    "EVSE feature flags endpoint unavailable for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                return None
            raise
        return data if isinstance(data, dict) else None

    async def devices_inventory(self) -> dict:
        """Return site device inventory grouped by hardware type.

        GET /app-api/<site_id>/devices.json
        """
        url = f"{BASE_URL}/app-api/{self._site}/devices.json"
        data = await self._json("GET", url, headers=self._history_headers())
        if isinstance(data, dict):
            return data
        return {}

    async def devices_tree(self) -> dict | None:
        """Return the system dashboard device hierarchy when available.

        GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices-tree
        Fallback: GET /pv/systems/<site_id>/system_dashboard/devices-tree
        """

        modern_url = (
            f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/"
            f"{self._site}/devices-tree"
        )
        legacy_url = f"{BASE_URL}/pv/systems/{self._site}/system_dashboard/devices-tree"
        return await self._system_dashboard_get(modern_url, legacy_url)

    async def system_dashboard_summary(
        self, *, allow_reauth: bool = True
    ) -> dict | None:
        """Return the system dashboard capability summary when available.

        GET /service/system_dashboard/api_internal/cs/sites/<site_id>/summary
        """

        url = (
            f"{BASE_URL}/service/system_dashboard/api_internal/cs/sites/"
            f"{self._site}/summary"
        )
        headers = self._system_dashboard_headers()
        try:
            data = await self._json(
                "GET",
                url,
                headers=headers,
                allow_reauth=allow_reauth,
            )
        except Exception as err:  # noqa: BLE001
            if not allow_reauth and (
                isinstance(err, Unauthorized)
                or (
                    isinstance(err, aiohttp.ClientResponseError)
                    and err.status in (401, 403)
                )
            ):
                raise
            if self._system_dashboard_is_optional_error(err):
                return None
            raise

        if not isinstance(data, dict):
            return None

        is_hems = data.get("is_hems")
        if isinstance(is_hems, bool):
            self._hems_site_supported = is_hems

        return data

    async def devices_details(self, type_key: str) -> dict | None:
        """Return system dashboard per-type device details when available.

        GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=<observed_type>
        Fallback: GET /pv/systems/<site_id>/system_dashboard/devices_details?type=<observed_type>
        """

        normalized = _system_dashboard_query_type(type_key)
        if not normalized:
            return None
        modern_url = str(
            URL(
                f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/{self._site}/devices_details"
            ).update_query({"type": normalized})
        )
        legacy_url = str(
            URL(
                f"{BASE_URL}/pv/systems/{self._site}/system_dashboard/devices_details"
            ).update_query({"type": normalized})
        )
        return await self._system_dashboard_get(modern_url, legacy_url)

    async def hems_devices(self, *, refresh_data: bool = False) -> dict | None:
        """Return dedicated HEMS device inventory when available.

        GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/hems-devices
        """

        url = str(
            URL(
                f"https://hems-integration.enphaseenergy.com/api/v1/hems/{self._site}/hems-devices"
            ).update_query({"refreshData": str(bool(refresh_data)).lower()})
        )
        try:
            data = await self._json(
                "GET",
                url,
                headers=self._hems_headers,
                allow_reauth=False,
            )
            self._hems_site_supported = True
        except Unauthorized:
            _LOGGER.debug(
                "HEMS devices endpoint unavailable for site %s (unauthorized)",
                redact_site_id(self._site),
            )
            raise
        except InvalidPayloadError as err:
            if _is_optional_non_json_payload(err):
                _LOGGER.debug(
                    "HEMS devices endpoint unavailable for site %s (%s)",
                    redact_site_id(self._site),
                    redact_text(err.summary, site_ids=(self._site,)),
                )
                return None
            raise
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403):
                _LOGGER.debug(
                    "HEMS devices endpoint auth failure for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                raise
            if err.status == 404 or _is_hems_invalid_site_error(err):
                if _is_hems_invalid_site_error(err):
                    self._hems_site_supported = False
                _LOGGER.debug(
                    "HEMS devices endpoint unavailable for site %s (status=%s)",
                    redact_site_id(self._site),
                    err.status,
                )
                return None
            raise
        return data if isinstance(data, dict) else None

    async def grid_control_check(self) -> dict:
        """Return site-level grid control eligibility guard flags.

        GET /app-api/<site_id>/grid_control_check.json
        """

        url = f"{BASE_URL}/app-api/{self._site}/grid_control_check.json"
        data = await self._json("GET", url, headers=self._history_headers())
        if isinstance(data, dict):
            return data
        return {}

    async def request_grid_toggle_otp(self) -> dict:
        """Request OTP delivery for a site grid-mode toggle.

        GET /app-api/<site_id>/grid_toggle_otp.json
        """

        url = f"{BASE_URL}/app-api/{self._site}/grid_toggle_otp.json"
        headers = self._history_headers()
        headers.update(self._control_headers())
        data = await self._json("GET", url, headers=headers)
        if isinstance(data, dict):
            return data
        return {}

    async def validate_grid_toggle_otp(self, otp: str) -> bool:
        """Validate a grid-mode OTP for the configured site.

        POST /app-api/grid_toggle_otp.json
        """

        url = f"{BASE_URL}/app-api/grid_toggle_otp.json"
        headers = self._history_form_headers()
        headers.update(self._control_headers())
        payload = {"otp": str(otp), "site_id": str(self._site)}
        data = await self._json("POST", url, data=payload, headers=headers)
        if not isinstance(data, dict):
            return False
        return data.get("valid") is True

    async def set_grid_state(self, envoy_serial_number: str, state: int) -> dict:
        """Submit a grid relay state-change request.

        POST /pv/settings/grid_state.json
        """

        url = f"{BASE_URL}/pv/settings/grid_state.json"
        headers = self._history_form_headers()
        headers.update(self._control_headers())
        payload = {
            "envoy_serial_number": str(envoy_serial_number),
            "state": int(state),
        }
        data = await self._json("POST", url, data=payload, headers=headers)
        if isinstance(data, dict):
            return data
        return {}

    async def log_grid_change(
        self,
        envoy_serial_number: str,
        old_state: str,
        new_state: str,
    ) -> dict:
        """Write grid relay transition audit metadata.

        POST /pv/settings/log_grid_change.json
        """

        url = f"{BASE_URL}/pv/settings/log_grid_change.json"
        headers = self._history_form_headers()
        headers.update(self._control_headers())
        payload = {
            "envoy_serial_number": str(envoy_serial_number),
            "old_state": str(old_state),
            "new_state": str(new_state),
        }
        data = await self._json("POST", url, data=payload, headers=headers)
        if isinstance(data, dict):
            return data
        return {}

    async def battery_backup_history(self) -> dict:
        """Return battery backup outage history for the site.

        GET /app-api/<site_id>/battery_backup_history.json
        """

        url = f"{BASE_URL}/app-api/{self._site}/battery_backup_history.json"
        data = await self._json("GET", url, headers=self._history_headers())
        if isinstance(data, dict):
            return data
        return {}

    async def battery_status(self) -> dict:
        """Return battery status payload used by the Enlighten battery card.

        GET /pv/settings/<site_id>/battery_status.json
        """

        url = f"{BASE_URL}/pv/settings/{self._site}/battery_status.json"
        data = await self._json("GET", url, headers=self._history_headers())
        if isinstance(data, dict):
            return data
        return {}

    async def ac_battery_devices_page(self, *, status: str = "active") -> str:
        """Return the AC Battery devices page HTML for the site."""

        url = str(
            URL(f"{BASE_URL}/systems/{self._site}/devices").update_query(
                {"status": status}
            )
        )
        headers = self._systems_html_headers(
            f"{BASE_URL}/systems/{self._site}/devices?status={status}"
        )
        return await self._text("GET", url, headers=headers)

    async def ac_battery_detail_page(self, battery_id: str) -> str:
        """Return the AC Battery detail page HTML."""

        url = f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}"
        headers = self._systems_html_headers(
            f"{BASE_URL}/systems/{self._site}/devices?status=active"
        )
        return await self._text("GET", url, headers=headers)

    async def ac_battery_events_page(self, battery_id: str) -> str:
        """Return the AC Battery events page HTML."""

        url = f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}/events"
        headers = self._systems_html_headers(
            f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}"
        )
        return await self._text("GET", url, headers=headers)

    async def ac_battery_show_stat_data(self, battery_id: str) -> str:
        """Return the AC Battery telemetry HTML fragment."""

        url = (
            f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}/show_stat_data"
        )
        headers = self._layout_headers()
        headers["Accept"] = "*/*"
        headers["Referer"] = (
            f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}"
        )
        return await self._text("GET", url, headers=headers)

    async def set_ac_battery_sleep(
        self, battery_id: str, sleep_min_soc: int
    ) -> TextResponse:
        """Request AC Battery sleep mode using the Enlighten web route."""

        url = str(
            URL(
                f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}/sleep"
            ).update_query({"sleep_min_soc": int(sleep_min_soc)})
        )
        headers = self._systems_html_headers(
            f"{BASE_URL}/systems/{self._site}/devices?status=active"
        )
        return await self._text_response(
            "GET",
            url,
            headers=headers,
            allow_redirects=False,
            expected_statuses=(302,),
        )

    async def set_ac_battery_wake(self, battery_id: str) -> TextResponse:
        """Request AC Battery wake/cancel using the Enlighten web route."""

        url = f"{BASE_URL}/systems/{self._site}/ac_batteries/{battery_id}/wake"
        headers = self._systems_html_headers(
            f"{BASE_URL}/systems/{self._site}/devices?status=active"
        )
        return await self._text_response(
            "GET",
            url,
            headers=headers,
            allow_redirects=False,
            expected_statuses=(302,),
        )

    async def dry_contacts_settings(self) -> dict:
        """Return dry-contact settings payload used by site settings views.

        GET /pv/settings/<site_id>/dry_contacts
        """

        url = f"{BASE_URL}/pv/settings/{self._site}/dry_contacts"
        data = await self._json("GET", url, headers=self._history_headers())
        if isinstance(data, dict):
            return data
        return {}

    async def inverters_inventory(
        self,
        *,
        limit: int = 1000,
        offset: int = 0,
        search: str = "",
    ) -> dict:
        """Return site inverter inventory used by legacy microinverter views.

        GET /app-api/<site_id>/inverters.json
        """

        url = URL(f"{BASE_URL}/app-api/{self._site}/inverters.json").with_query(
            {
                "limit": int(limit),
                "offset": int(offset),
                "search": str(search),
            }
        )
        data = await self._json("GET", str(url), headers=self._history_headers())
        if not isinstance(data, dict):
            return {}
        return data

    async def inverter_status(self) -> dict[str, dict[str, Any]]:
        """Return inverter status map keyed by inverter id.

        GET /systems/<site_id>/inverter_status_x.json
        """

        url = f"{BASE_URL}/systems/{self._site}/inverter_status_x.json"
        data = await self._json("GET", url, headers=self._layout_headers())
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            key_text = str(key).strip()
            if not key_text:
                continue
            out[key_text] = dict(value)
        return out

    async def inverter_production(
        self,
        *,
        start_date: str,
        end_date: str,
    ) -> dict:
        """Return inverter production totals for a date range.

        GET /systems/<site_id>/inverter_data_x/energy.json?start_date=...&end_date=...
        """

        url = URL(
            f"{BASE_URL}/systems/{self._site}/inverter_data_x/energy.json"
        ).with_query({"start_date": str(start_date), "end_date": str(end_date)})
        data = await self._json("GET", str(url), headers=self._layout_headers())
        if not isinstance(data, dict):
            return {}
        production_raw = data.get("production")
        production: dict[str, float] = {}
        if isinstance(production_raw, dict):
            for key, value in production_raw.items():
                key_text = str(key).strip()
                if not key_text:
                    continue
                try:
                    production[key_text] = float(value)
                except (TypeError, ValueError):
                    continue
        return {
            "production": production,
            "start_date": data.get("start_date"),
            "end_date": data.get("end_date"),
        }

    async def session_history_filter_criteria(
        self,
        *,
        request_id: str | None = None,
        username: str | None = None,
    ) -> dict:
        """Fetch session history filter criteria for a site."""

        request_id = request_id or str(uuid.uuid4())
        if username is None:
            username = self._session_history_username()
        query = {"source": "evse", "requestId": request_id}
        if username:
            query["username"] = username
        url = URL(
            f"{BASE_URL}/service/enho_historical_events_ms/{self._site}/filter_criteria"
        ).with_query(query)
        headers = self._session_history_headers(request_id, username)
        return await self._json("GET", str(url), headers=headers)

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
    ) -> dict:
        """Fetch charging sessions for a charger between the provided dates.

        POST /service/enho_historical_events_ms/<site_id>/sessions/<sn>/history
        Dates must be formatted as DD-MM-YYYY in the site locale.
        """
        url = f"{BASE_URL}/service/enho_historical_events_ms/{self._site}/sessions/{sn}/history"
        request_id = request_id or str(uuid.uuid4())
        if username is None:
            username = self._session_history_username()
        payload: dict[str, Any] = {
            "source": "evse",
            "params": {
                "offset": int(offset),
                "limit": int(limit),
                "startDate": start_date,
                "endDate": end_date or start_date,
            },
        }
        if timezone:
            payload["params"]["timezone"] = timezone
        headers = self._session_history_headers(request_id, username)
        try:
            return await self._json("POST", url, json=payload, headers=headers)
        except aiohttp.ClientResponseError as err:
            if is_session_history_unavailable_error(err.message, err.status, url):
                raise SessionHistoryUnavailable(str(err)) from err
            raise
