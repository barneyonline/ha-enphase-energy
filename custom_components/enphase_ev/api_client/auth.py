"""Authentication request-shaping helpers for the Enphase cloud client."""

from __future__ import annotations

from collections.abc import Callable, Mapping


def login_headers(*, base_url: str, user_agent: str) -> dict[str, str]:
    """Return headers for the initial Enlighten login request."""

    return {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": f"{base_url}/",
        "User-Agent": user_agent,
        "X-Requested-With": "XMLHttpRequest",
    }


def login_form_headers(*, base_url: str, user_agent: str) -> dict[str, str]:
    """Return browser-style headers for the HTML form login flow."""

    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": base_url,
        "Referer": f"{base_url}/",
        "User-Agent": user_agent,
    }


def mfa_headers(
    cookies: dict[str, str] | None,
    *,
    base_headers: Mapping[str, str],
    cookie_header: Callable[[dict[str, str] | None], str],
    xsrf_token: Callable[[dict[str, str] | None], str | None],
) -> dict[str, str]:
    """Return headers for MFA endpoints with cookie/XSRF handling."""

    headers = dict(base_headers)
    headers["Accept"] = "application/json, text/plain, */*"
    serialized_cookies = cookie_header(cookies)
    if serialized_cookies:
        headers["Cookie"] = serialized_cookies
    token = xsrf_token(cookies)
    if token:
        headers["X-CSRF-Token"] = token
    return headers


def login_session_from_cookies(
    cookies: Mapping[str, str] | None,
    *,
    jwt_session_id: Callable[[str | None], str | None],
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
        session_id = jwt_session_id(manager_token)
    return (
        str(session_id) if session_id else None,
        str(manager_token) if manager_token else None,
    )
