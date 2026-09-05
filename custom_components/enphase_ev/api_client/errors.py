"""Typed, sanitized errors shared by Enphase HTTP endpoint surfaces."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import aiohttp

from ..api_models import AuthTokens
from ..log_redaction import redact_text

_ENPHASE_ERROR_STATUS_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


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


class ActivationAccessDenied(OptionalEndpointUnavailable):
    """Raised when Activation explicitly denies installer-level access."""


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


class InvalidPayloadError(aiohttp.ClientError):  # type: ignore[misc, unused-ignore]
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


def _enphase_error_status_from_text(text: str | None) -> str | None:
    """Return a bounded Enphase error status without retaining the response body."""

    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    status = error.get("status")
    if not isinstance(status, str):
        return None
    normalized = status.strip().upper()
    if not _ENPHASE_ERROR_STATUS_RE.fullmatch(normalized):
        return None
    return normalized


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


def make_response_error(
    response: aiohttp.ClientResponse, body_text: str | None
) -> aiohttp.ClientResponseError:
    """Preserve bounded backend status without retaining an unsafe response body."""

    error = aiohttp.ClientResponseError(
        response.request_info,
        response.history,
        status=response.status,
        message=_safe_response_error_message(
            status=int(response.status),
            reason=response.reason,
            headers=response.headers,
            body_text=body_text,
        ),
        headers=response.headers,
    )
    setattr(error, "enphase_error_status", _enphase_error_status_from_text(body_text))
    return error
