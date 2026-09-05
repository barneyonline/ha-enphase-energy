from __future__ import annotations

import aiohttp
import pytest
from yarl import URL

from custom_components.enphase_ev import api
from custom_components.enphase_ev.api_client import authentication as api_authentication


@pytest.mark.asyncio
async def test_async_authenticate_populates_site_headers(monkeypatch):
    site_headers: list[dict[str, str]] = []
    token_requests: list[tuple[str, dict[str, str]]] = []

    async def _fake_request_json(
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        timeout: int,
        headers: dict[str, str] | None = None,
        data=None,
        json_data=None,
    ):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies(
                {
                    "XSRF-TOKEN": "xsrf123",
                    "enlighten_session": "sess123",
                },
                response_url=URL(api.BASE_URL),
            )
            return {"session_id": "sid123"}
        if url == api.SELF_TOKEN_URL:
            token_requests.append((method, headers or {}))
            session.cookie_jar.update_cookies(
                {"enlighten_session": "rotated-session"},
                response_url=URL(api.BASE_URL),
            )
            return {"token": "token123", "expires_at": 1700000000}
        if url == api.SITE_SEARCH_URL:
            site_headers.append(headers or {})
            return {"sites": [{"id": 7812456, "title": "Garage"}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api_authentication, "_request_json", _fake_request_json)

    class StubSession:
        def __init__(self):
            self.cookie_jar = aiohttp.CookieJar()

    session = StubSession()
    tokens, sites = await api.async_authenticate(session, "user@example.com", "secret")

    assert tokens.access_token == "token123"
    assert tokens.cookie and "rotated-session" in tokens.cookie
    assert sites and sites[0].site_id == "7812456"
    assert site_headers, "Site discovery request headers were not captured"
    assert token_requests == [
        (
            "GET",
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{api.BASE_URL}/",
                "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
    ]

    captured = site_headers[0]
    assert captured["Accept"] == "*/*"
    assert captured["X-CSRF-Token"] == "xsrf123"
    assert captured["X-Requested-With"] == "XMLHttpRequest"
    assert captured["Referer"] == f"{api.BASE_URL}/"
    assert captured["User-Agent"] == api._ENLIGHTEN_BROWSER_USER_AGENT
    assert captured["Authorization"] == "Bearer token123"
    assert captured["e-auth-token"] == "token123"
    # Ensure the caller explicitly sets Cookie so the request works without relying on session defaults
    assert "Cookie" in captured and "rotated-session" in captured["Cookie"]
