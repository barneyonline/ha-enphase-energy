"""Header surface for the stable Enphase client facade."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Callable

from ..const import (
    BASE_URL,
)

if TYPE_CHECKING:
    from ..api import EnphaseEVClient

from .common import (
    _cookie_map_from_header,
    _jwt_session_id,
    _jwt_user_id,
)


def update_credentials(
    self: EnphaseEVClient,
    *,
    eauth: str | None = None,
    cookie: str | None = None,
) -> None:
    """Update headers when auth credentials change."""

    credentials_changed = bool(
        (eauth is not None and (eauth or None) != self._eauth)
        or (cookie is not None and (cookie or "") != self._cookie)
    )
    if eauth is not None:
        self._eauth = eauth or None
    if cookie is not None:
        self._cookie = cookie or ""
    if credentials_changed:
        self._clear_activation_auth_context()

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


def _bearer(self: EnphaseEVClient) -> str | None:
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


def scheduler_bearer(self: EnphaseEVClient) -> str | None:
    """Public bearer accessor for scheduler feature checks."""

    return self._bearer()


def has_scheduler_bearer(self: EnphaseEVClient) -> bool:
    """Return True when scheduler bearer auth can be derived."""

    return bool(self.scheduler_bearer())


def _history_bearer(self: EnphaseEVClient) -> str | None:
    """Return the preferred bearer token for session history calls."""

    return self._eauth or self._bearer()


def _session_history_username(self: EnphaseEVClient) -> str | None:
    """Return the user id expected by the session history service."""

    return _jwt_user_id(self._history_bearer())


def _session_history_headers(
    self: EnphaseEVClient, request_id: str | None, username: str | None
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
    self: EnphaseEVClient,
    request_id: str | None,
    username: str | None,
) -> dict[str, str]:
    """Return headers for EVSE timeseries endpoints."""

    return self._session_history_headers(request_id, username)


def _site_web_graph_referer(
    self: EnphaseEVClient, view: str, *, graph_range: str = "years"
) -> str:
    """Return a web-app graph referer for a site-scoped Enlighten view."""

    query = ""
    app_version = _cookie_map_from_header(self._cookie).get("appVersion")
    if app_version:
        query = f"?v={app_version}"
    return f"{BASE_URL}/web/{self._site}/{view}/graph/{graph_range}{query}"


def _site_web_referer(self: EnphaseEVClient, view: str) -> str:
    """Return the default years-graph referer for site XHR families."""

    return self._site_web_graph_referer(view)


def _root_xhr_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Return base headers for root-scoped Enlighten XHR requests."""

    headers = dict(self._h)
    headers["Accept"] = "*/*"
    headers["Referer"] = f"{BASE_URL}/"
    return headers


def _history_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Return headers for app-api and pv/settings history-family requests."""

    headers = dict(self._h)
    headers["Accept"] = "*/*"
    headers["Referer"] = self._site_web_referer("history")
    return headers


def _homeowner_events_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Return browser-style headers for the homeowner event-history feed."""

    headers = self._history_headers()
    headers["Accept"] = "application/json"
    headers["Content-Type"] = "application/json"
    headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


def _today_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Return headers for EV today-page XHR requests."""

    headers = dict(self._h)
    headers["Accept"] = "*/*"
    headers["Referer"] = self._site_web_graph_referer("today", graph_range="hours")
    return headers


def _today_json_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Return headers for EV today-page JSON/XHR requests."""

    headers = self._today_headers()
    headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    return headers


def _history_form_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Return headers for history-family form POST requests."""

    headers = self._history_headers()
    headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    headers["Origin"] = BASE_URL
    return headers


def _layout_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Return headers for systems/layout-family requests."""

    headers = dict(self._h)
    headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    headers["Referer"] = self._site_web_referer("layout")
    return headers


def _systems_html_headers(
    self: EnphaseEVClient, referer: str | None = None
) -> dict[str, str]:
    """Return browser-style headers for site-scoped HTML /systems routes."""

    headers = dict(self._h)
    headers["Accept"] = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    )
    headers["Referer"] = referer or f"{BASE_URL}/systems/{self._site}/devices"
    return headers


def _systems_json_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Return headers for site-scoped /systems JSON endpoints."""

    headers = dict(self._h)
    headers["Accept"] = "application/json"
    headers["Referer"] = self._site_web_referer("layout")
    return headers


def _control_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Return Authorization header overrides for control-plane requests."""

    bearer = self._bearer() or self._eauth
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}
    return {}


def _control_request_headers(
    self: EnphaseEVClient, base: Callable[[], dict[str, str]]
) -> dict[str, str]:
    """Build a control request using the current credentials."""

    return {**base(), **self._control_headers()}


def _vpp_headers(self: EnphaseEVClient) -> dict[str, str | None]:
    """Return isolated browser headers for the Grid Services host."""

    # Observed GS requests send the control token without a Bearer prefix.
    token = self._bearer() or self._eauth
    headers: dict[str, str | None] = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "Cookie": None,
        "X-CSRF-Token": None,
        "X-Requested-With": None,
        "e-auth-token": None,
        "Content-Type": None,
        "Authorization": token,
    }
    return headers


def control_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Public control header helper for read-only diagnostics checks."""

    return self._control_headers()


def _system_dashboard_headers(self: EnphaseEVClient) -> dict[str, str]:
    """Return headers for system dashboard read endpoints."""

    headers = dict(self._h)
    headers["Accept"] = "application/json"
    headers["Referer"] = f"{BASE_URL}/app/system_dashboard/sites/{self._site}/summary"
    headers.update(self._control_headers())
    return headers


def _hems_auth_context(self: EnphaseEVClient) -> tuple[str | None, str | None]:
    """Return the preferred HEMS bearer token and resolved user id."""

    bearer = self._bearer() or self._eauth
    return bearer, _jwt_user_id(bearer)


def _hems_headers(self: EnphaseEVClient) -> dict[str, str]:
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


def _tariff_headers(
    self: EnphaseEVClient, *, write: bool = False
) -> dict[str, str | None]:
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
