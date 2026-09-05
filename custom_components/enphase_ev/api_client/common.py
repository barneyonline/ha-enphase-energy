"""Shared cloud request/authentication helpers, independent of client instances."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from html import unescape
from time import monotonic
from typing import Any, Iterable
from urllib.parse import unquote

import aiohttp
from yarl import URL

from .. import api_parsers
from ..api_models import (
    ChargerInfo,
    SiteInfo,
)
from ..const import (
    BASE_URL,
    GS_BASE_URL,
)
from ..log_redaction import (
    redact_identifier,
    redact_text,
)
from ..request_metrics import record_request_timings
from . import transport as api_transport
from .errors import (
    EnlightenAuthTooManySessions,
    EnlightenAuthUnavailable,
    _safe_response_error_message,
)

_LOGGER = logging.getLogger("custom_components.enphase_ev.api")


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


_BATTERY_CONFIG_VARIANT_SESSION_COOKIE = "official_web_session_cookie"


_BATTERY_CONFIG_VARIANT_COOKIE_EAUTH = "cookie_eauth_compatible"


_BATTERY_CONFIG_VARIANT_MIXED = "mixed_auth_compatible"


_ACTIVATION_UI_URL_RE = re.compile(
    r"(?:(?:https?:)?//[^\"'<>\s]+)?/app/activation_ui/\?[^\"'<>\s]+",
    re.IGNORECASE,
)


_ACTIVATION_UI_TEMPLATE_RE = re.compile(
    r"https://activations-ui\.enphaseenergy\.com/?\?[^`\"'<>\s]+",
    re.IGNORECASE,
)


_ACTIVATION_TOKEN_ASSIGNMENT_RE = re.compile(
    r"\b(?:const|let|var)\s+token\s*=\s*['\"]"
    r"(?P<token>[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)['\"]",
    re.IGNORECASE,
)


_ACTIVATION_GRID_PROFILE_RE = re.compile(
    r"Gateway\s*-\s*(?P<serial>[A-Za-z0-9_-]+)\s*</h[1-6]>"
    r"(?:(?!</div>).){0,2000}?Grid\s+Profile\s*:\s*"
    r"(?P<name>.*?)(?=<button\b|</div>)",
    re.IGNORECASE | re.DOTALL,
)


_HTML_TAG_RE = re.compile(r"<[^>]+>")


_ENLIGHTEN_READ_CONCURRENCY_LIMIT = 3


_ENLIGHTEN_OPTIONAL_READ_CONCURRENCY_LIMIT = 2


_SYSTEM_EVENTS_PAGE_SIZE = 200


_SYSTEM_EVENTS_MAX_PAGES = 10


_SYSTEM_ALARMS_PAGE_SIZE = 200


_SYSTEM_ALARMS_MAX_PAGES = 10


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


_enlighten_read_semaphore: asyncio.Semaphore | None = None


_enlighten_optional_read_semaphore: asyncio.Semaphore | None = None


_enlighten_optional_read: ContextVar[bool] = ContextVar(
    "enphase_ev_enlighten_optional_read", default=False
)


_enlighten_read_limiter_bypass: ContextVar[bool] = ContextVar(
    "enphase_ev_enlighten_read_limiter_bypass", default=False
)


JsonDict = dict[str, Any]


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


def _truncate_preview(text: str, *, max_length: int = 256) -> str:
    """Return a compact payload preview capped to the requested size."""

    compact = " ".join(str(text or "").split()).strip()
    if len(compact) > max_length:
        return f"{compact[:max_length]}..."
    return compact


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
    query_string = url_obj.query_string
    query = getattr(url_obj, "query", None)
    cursor_query = {
        str(key): "[redacted]"
        for key in query or ()
        if str(key).strip().casefold() in {"cursor", "next"}
    }
    if cursor_query:
        query_string = url_obj.update_query(cursor_query).query_string
    if query_string:
        path = f"{path}?{query_string}" if path else f"?{query_string}"
    if path:
        return f"{method_text} {path}"
    return method_text


def _serialize_cookie_jar(
    jar: aiohttp.abc.AbstractCookieJar, urls: Iterable[str | URL]
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


def _activation_context_from_settings_html(
    payload: str,
) -> tuple[str, str] | None:
    """Extract the Activation UI JWT and referer embedded by Enlighten settings."""

    normalized = unescape(payload).replace(r"\u0026", "&").replace(r"\/", "/")
    for match in _ACTIVATION_UI_URL_RE.finditer(normalized):
        candidate = match.group(0)
        if candidate.startswith("//"):
            candidate = f"https:{candidate}"
        elif candidate.startswith("/"):
            candidate = f"{BASE_URL}{candidate}"
        try:
            activation_url = URL(candidate)
        except Exception:  # noqa: BLE001 - malformed unrelated page content
            continue
        if (
            activation_url.host != URL(BASE_URL).host
            or activation_url.path != "/app/activation_ui/"
        ):
            continue
        token = activation_url.query.get("token")
        if token and token.count(".") >= 2:
            return str(token), str(activation_url)

    # Current Settings pages create a cross-origin Activation iframe only after
    # Change is selected. Reconstruct that browser referer from the inert script
    # template without executing page JavaScript.
    token_match = _ACTIVATION_TOKEN_ASSIGNMENT_RE.search(normalized)
    template_match = _ACTIVATION_UI_TEMPLATE_RE.search(normalized)
    if token_match is None or template_match is None:
        return None
    token = token_match.group("token")
    candidate = template_match.group(0).replace("${token}", token)
    serial_match = re.search(
        r"showGridProfileModal\(\s*['\"](?P<serial>[A-Za-z0-9_-]+)['\"]\s*\)",
        normalized,
        re.IGNORECASE,
    )
    if serial_match is not None:
        candidate = candidate.replace("${serialnum}", serial_match.group("serial"))
    if "${" in candidate:
        return None
    try:
        activation_url = URL(candidate)
    except Exception:  # noqa: BLE001 - malformed unrelated page content
        return None
    if (
        activation_url.host != "activations-ui.enphaseenergy.com"
        or activation_url.path not in {"", "/"}
        or activation_url.query.get("token") != token
    ):
        return None
    return token, str(activation_url)


def _activation_grid_profiles_from_settings_html(
    payload: str,
) -> list[tuple[str, str]]:
    """Extract read-only Gateway Grid Profile labels from Settings HTML."""

    normalized = unescape(payload).replace(r"\u0026", "&").replace(r"\/", "/")
    profiles: list[tuple[str, str]] = []
    for match in _ACTIVATION_GRID_PROFILE_RE.finditer(normalized):
        serial = match.group("serial").strip()
        name = _HTML_TAG_RE.sub(" ", match.group("name"))
        name = " ".join(unescape(name).split())
        if serial and name:
            profiles.append((serial, name))
    return profiles


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
    return url_text.startswith((f"{BASE_URL}/", f"{GS_BASE_URL}/"))


def _get_enlighten_read_semaphore() -> asyncio.Semaphore:
    """Return the shared semaphore used to limit concurrent Enlighten reads."""

    global _enlighten_read_semaphore
    if _enlighten_read_semaphore is None:
        _enlighten_read_semaphore = asyncio.Semaphore(_ENLIGHTEN_READ_CONCURRENCY_LIMIT)
    return _enlighten_read_semaphore


def _get_enlighten_optional_read_semaphore() -> asyncio.Semaphore:
    """Return the limiter that reserves capacity for core Enlighten reads."""

    global _enlighten_optional_read_semaphore
    if _enlighten_optional_read_semaphore is None:
        _enlighten_optional_read_semaphore = asyncio.Semaphore(
            _ENLIGHTEN_OPTIONAL_READ_CONCURRENCY_LIMIT
        )
    return _enlighten_optional_read_semaphore


@contextmanager
def enlighten_optional_read_scope() -> Iterator[None]:
    """Mark browser-host reads in this context as optional background work."""

    token = _enlighten_optional_read.set(True)
    try:
        yield
    finally:
        _enlighten_optional_read.reset(token)


@contextmanager
def _enlighten_reauth_read_scope() -> Iterator[None]:
    """Let nested credential refreshes escape a limiter held by their caller."""

    token = _enlighten_read_limiter_bypass.set(True)
    try:
        yield
    finally:
        _enlighten_read_limiter_bypass.reset(token)


@asynccontextmanager
async def _enlighten_read_request_guard(
    method: object, url: object
) -> AsyncIterator[None]:
    """Limit concurrent GET/HEAD requests to the Enlighten web host."""

    if _enlighten_read_limiter_bypass.get() or not _should_limit_enlighten_read_request(
        method, url
    ):
        yield
        return
    if _enlighten_optional_read.get():
        async with _get_enlighten_optional_read_semaphore():
            async with _get_enlighten_read_semaphore():
                yield
        return
    async with _get_enlighten_read_semaphore():
        yield


@asynccontextmanager
async def _timed_enlighten_read_request_guard(
    method: object, url: object
) -> AsyncIterator[None]:
    """Measure limiter queueing for one scoped client request."""

    if not _should_limit_enlighten_read_request(method, url):
        yield
        return
    started = monotonic()
    acquired = False
    try:
        async with _enlighten_read_request_guard(method, url):
            acquired = True
            record_request_timings(queue_s=monotonic() - started)
            yield
    finally:
        if not acquired:
            record_request_timings(queue_s=monotonic() - started)


@asynccontextmanager
async def _timed_response_context(request_context: Any) -> AsyncIterator[Any]:
    """Measure the wait until response headers arrive for one HTTP attempt."""

    started = monotonic()
    headers_received = False
    try:
        async with request_context as response:
            headers_received = True
            record_request_timings(network_s=monotonic() - started)
            yield response
    finally:
        if not headers_received:
            record_request_timings(network_s=monotonic() - started)


async def _timed_response_json(response: Any) -> Any:
    """Read and decode a JSON body while recording parsing time."""

    started = monotonic()
    try:
        return await response.json()
    finally:
        record_request_timings(parsing_s=monotonic() - started)


async def _timed_response_text(response: Any) -> str:
    """Read a text body while recording parsing time."""

    started = monotonic()
    try:
        return str(await response.text())
    finally:
        record_request_timings(parsing_s=monotonic() - started)


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
    return await api_transport.request_json(
        session,
        method,
        url,
        timeout=timeout,
        headers=headers,
        data=data,
        json_data=json_data,
        request_guard=_enlighten_read_request_guard,
        request_label=_request_label,
        safe_error_message=_safe_response_error_message,
        is_session_limit=_is_too_many_active_sessions_response,
        unavailable_error=EnlightenAuthUnavailable,
        session_limit_error=EnlightenAuthTooManySessions,
    )


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
    return await api_transport.request_mfa_json(
        session,
        method,
        url,
        timeout=timeout,
        headers=headers,
        data=data,
        request_label=_request_label,
        safe_error_message=_safe_response_error_message,
        is_session_limit=_is_too_many_active_sessions_response,
        unavailable_error=EnlightenAuthUnavailable,
        session_limit_error=EnlightenAuthTooManySessions,
    )


def _normalize_sites(payload: Any) -> list[SiteInfo]:
    """Normalize site payloads from various Enlighten APIs."""

    return api_parsers.normalize_sites(payload)


def _normalize_chargers(payload: Any) -> list[ChargerInfo]:
    """Normalize charger list payloads into ChargerInfo entries."""

    return api_parsers.normalize_chargers(payload)
