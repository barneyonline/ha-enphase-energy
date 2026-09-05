"""Login, MFA and token/site discovery using an injected HTTP session."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import aiohttp

from ..api_models import (
    AuthTokens,
    SiteInfo,
)
from ..const import (
    BASE_URL,
    DEFAULT_AUTH_TIMEOUT,
    ENTREZ_URL,
    LOGIN_FORM_URL,
    LOGIN_URL,
    MFA_RESEND_URL,
    MFA_VALIDATE_URL,
    SELF_TOKEN_URL,
    SITE_SEARCH_URL,
)
from ..log_redaction import (
    redact_text,
)
from . import auth as api_auth
from .common import (
    _ENLIGHTEN_BROWSER_USER_AGENT,
    _LOGGER,
    _cookie_header_from_map,
    _decode_jwt_exp,
    _extract_login_session,
    _extract_xsrf_token,
    _is_too_many_active_sessions_response,
    _jwt_session_id,
    _normalize_sites,
    _request_json,
    _request_mfa_json,
    _seed_cookie_jar,
    _serialize_cookie_jar,
)
from .errors import (
    EnlightenAuthInvalidCredentials,
    EnlightenAuthInvalidOTP,
    EnlightenAuthMFARequired,
    EnlightenAuthOTPBlocked,
    EnlightenAuthTooManySessions,
    EnlightenAuthUnavailable,
)


def _mfa_headers(cookies: dict[str, str] | None) -> dict[str, str]:
    """Return headers for MFA endpoints with cookie/XSRF handling."""
    return api_auth.mfa_headers(
        cookies,
        base_headers=_login_headers(),
        cookie_header=_cookie_header_from_map,
        xsrf_token=_extract_xsrf_token,
    )


def _login_headers() -> dict[str, str]:
    """Return headers for the initial Enlighten login request."""
    return api_auth.login_headers(
        base_url=BASE_URL,
        user_agent=_ENLIGHTEN_BROWSER_USER_AGENT,
    )


def _login_form_headers() -> dict[str, str]:
    """Return browser-style headers for the HTML form login flow."""
    return api_auth.login_form_headers(
        base_url=BASE_URL,
        user_agent=_ENLIGHTEN_BROWSER_USER_AGENT,
    )


def _extract_login_session_from_cookies(
    cookies: dict[str, str] | None,
) -> tuple[str | None, str | None]:
    """Extract session details from post-login cookies."""
    return api_auth.login_session_from_cookies(
        cookies,
        jwt_session_id=_jwt_session_id,
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


async def _build_tokens_and_sites(
    session: aiohttp.ClientSession,
    _email: str,
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

    # Obtain the bearer/e-auth token from the same session-backed route used by
    # the Enlighten web application. If it is temporarily unavailable, retain
    # cookie-only mode so core site discovery can still proceed.
    token_payload: Any | None = None
    if tokens.session_id or tokens.cookie:
        try:
            token_payload = await _request_json(
                session,
                "GET",
                SELF_TOKEN_URL,
                timeout=timeout,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"{BASE_URL}/",
                    "User-Agent": _ENLIGHTEN_BROWSER_USER_AGENT,
                    "X-Requested-With": "XMLHttpRequest",
                },
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

    # Enlighten may rotate the session cookie while minting the token. Persist
    # the post-mint cookie jar and use it for site discovery rather than the
    # snapshot captured immediately after login.
    cookie_header, cookie_map = _serialize_cookie_jar(
        session.cookie_jar, (BASE_URL, ENTREZ_URL)
    )
    tokens.cookie = cookie_header
    tokens.raw_cookies = cookie_map

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
