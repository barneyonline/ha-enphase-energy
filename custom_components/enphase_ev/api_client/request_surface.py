"""Request surface for the stable Enphase client facade."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

import aiohttp
from yarl import URL

from ..api_models import (
    TextResponse,
)
from ..log_redaction import (
    redact_site_id,
    redact_text,
)
from ..request_metrics import record_request_attempt
from .errors import (
    EnphaseLoginWallUnauthorized,
    InvalidPayloadError,
    Unauthorized,
    _is_enphase_login_wall,
    _is_scheduler_charging_mode_endpoint,
    _scheduler_error_context_from_text,
    make_response_error,
)

if TYPE_CHECKING:
    from ..api import EnphaseEVClient

from .common import (
    _LOGGER,
    _cookie_names_from_header,
    _enlighten_reauth_read_scope,
    _payload_preview_and_hash,
    _redact_debug_json_body,
    _request_failure_debug_family,
    _request_label,
    _timed_enlighten_read_request_guard,
    _timed_response_context,
    _timed_response_json,
    _timed_response_text,
)


def _mark_payload_healthy(self: EnphaseEVClient, endpoint: str | None) -> None:
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


def _log_invalid_payload(self: EnphaseEVClient, err: InvalidPayloadError) -> None:
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
    self: EnphaseEVClient,
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
    self: EnphaseEVClient,
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


async def _json(
    self: EnphaseEVClient,
    method: str,
    url: str,
    *,
    mark_payload_success: bool = True,
    log_invalid_payload: bool = True,
    **kwargs: Any,
) -> Any:
    """Perform an HTTP request returning JSON with sane header handling.

    Accepts optional ``headers`` in kwargs which will be merged with the
    default headers for this client, allowing call-sites to add/override
    fields (e.g. Authorization) without causing duplicate parameter errors.
    Header values explicitly set to ``None`` are removed from the merged
    request headers, which allows per-request suppression of defaults such
    as ``e-auth-token``.
    ``headers`` may also be a zero-argument callable so retries can rebuild
    auth-sensitive headers after a successful reauthentication callback.
    ``allow_empty_success`` accepts an empty body after a successful status while
    preserving normal JSON validation for non-empty responses.
    """
    extra_headers = kwargs.pop("headers", None)
    use_cookie_header_only = kwargs.pop("use_cookie_header_only", False)
    debug_auth_source = kwargs.pop("debug_auth_source", None)
    debug_battery_attempt_id = kwargs.pop("debug_battery_attempt_id", None)
    debug_battery_attempt_changes = kwargs.pop(
        "debug_battery_attempt_changes",
        None,
    )
    redaction_identifiers = kwargs.pop("redaction_identifiers", None)
    allow_reauth = bool(kwargs.pop("allow_reauth", True))
    allow_empty_success = bool(kwargs.pop("allow_empty_success", False))
    attempt = 0
    request_label = _request_label(method, url)
    safe_request_label = redact_text(
        request_label,
        site_ids=(self._site,),
        identifiers=redaction_identifiers,
        max_length=256,
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
            if inspect.isawaitable(attempt_headers):
                attempt_headers = await attempt_headers
        else:
            attempt_headers = extra_headers
        if isinstance(attempt_headers, dict):
            base_headers = self._merge_request_headers(base_headers, attempt_headers)

        async with asyncio.timeout(self._timeout):
            async with _timed_enlighten_read_request_guard(method, url):
                async with self._request_session(
                    cookie_header_only=use_cookie_header_only
                ) as request_session:
                    self._request_count += 1
                    record_request_attempt()
                    async with _timed_response_context(
                        request_session.request(
                            method, url, headers=base_headers, **kwargs
                        )
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
                                with _enlighten_reauth_read_scope():
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
                                body_text = await _timed_response_text(r)
                            except (
                                Exception
                            ):  # noqa: BLE001 - fall back to generic message
                                body_text = None
                            response_error = make_response_error(r, body_text)
                            message = response_error.message
                            family = _request_failure_debug_family(
                                method,
                                endpoint or url,
                            )
                            if family is not None:
                                params = kwargs.get("params")
                                if isinstance(params, dict):
                                    params_summary: object = _redact_debug_json_body(
                                        params,
                                        site_ids=(self._site,),
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
                                header_flags = self._battery_config_header_debug_flags(
                                    base_headers,
                                    auth_source_override=debug_auth_source,
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
                            setattr(
                                response_error,
                                "enphase_routing_not_found",
                                (
                                    r.status == 404
                                    and self._is_routing_not_found(body_text)
                                ),
                            )
                            setattr(
                                response_error,
                                "enphase_invalid_charge_level",
                                (
                                    r.status == 500
                                    and self._is_invalid_charge_level_error(body_text)
                                ),
                            )
                            if _is_scheduler_charging_mode_endpoint(endpoint):
                                code, display = _scheduler_error_context_from_text(
                                    body_text
                                )
                                if code or display:
                                    setattr(
                                        response_error,
                                        "enphase_scheduler_error",
                                        {
                                            "code": code,
                                            "display": display,
                                        },
                                    )
                            raise response_error
                        try:
                            payload = await _timed_response_json(r)
                        except (aiohttp.ContentTypeError, ValueError) as err:
                            status = int(getattr(r, "status", 0) or 0)
                            content_type = ""
                            try:
                                content_type = str(
                                    r.headers.get("Content-Type", "")
                                ).strip()
                            except Exception:  # noqa: BLE001 - defensive header parsing
                                content_type = ""
                            try:
                                body_text = await _timed_response_text(r)
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
                            if allow_empty_success and not body_text.strip():
                                if mark_payload_success:
                                    self._mark_payload_healthy(endpoint or None)
                                return {}
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
                        if allow_empty_success and payload is None:
                            payload = {}
                        if mark_payload_success:
                            self._mark_payload_healthy(endpoint or None)
                        return payload


async def _text_response(
    self: EnphaseEVClient,
    method: str,
    url: str,
    *,
    expected_statuses: tuple[int, ...] | None = None,
    mark_payload_success: bool = True,
    **kwargs: Any,
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
            if inspect.isawaitable(attempt_headers):
                attempt_headers = await attempt_headers
        else:
            attempt_headers = extra_headers
        if isinstance(attempt_headers, dict):
            base_headers = self._merge_request_headers(base_headers, attempt_headers)

        async with asyncio.timeout(self._timeout):
            async with _timed_enlighten_read_request_guard(method, url):
                self._request_count += 1
                record_request_attempt()
                async with _timed_response_context(
                    self._s.request(method, url, headers=base_headers, **kwargs)
                ) as r:
                    if r.status == 401:
                        self._last_unauthorized_request = safe_request_label
                        if self._reauth_cb and attempt == 0:
                            attempt += 1
                            with _enlighten_reauth_read_scope():
                                reauth_ok = await self._reauth_cb()
                            if reauth_ok:
                                continue
                        raise Unauthorized()
                    if expected_statuses and r.status in expected_statuses:
                        text = await _timed_response_text(r)
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
                            body_text = await _timed_response_text(r)
                        except Exception:  # noqa: BLE001
                            body_text = None
                        response_error = make_response_error(r, body_text)
                        raise response_error
                    text = await _timed_response_text(r)
                    if _is_enphase_login_wall(endpoint=endpoint or None, payload=text):
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
    self: EnphaseEVClient,
    method: str,
    url: str,
    *,
    expected_statuses: tuple[int, ...] | None = None,
    mark_payload_success: bool = True,
    **kwargs: Any,
) -> str:
    """Perform an HTTP request returning text only."""

    response = await self._text_response(
        method,
        url,
        expected_statuses=expected_statuses,
        mark_payload_success=mark_payload_success,
        **kwargs,
    )
    return str(response.text)
