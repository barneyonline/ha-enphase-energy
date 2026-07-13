"""Coverage-focused tests for HTTP helpers, authentication, and fetch helpers."""

from __future__ import annotations

import base64
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from yarl import URL

from custom_components.enphase_ev import api


def _build_jwt(exp: int) -> str:
    payload = {"exp": exp}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"hdr.{payload_b64.rstrip('=')}.sig"


def _make_cre(status: int, message: str = "error") -> aiohttp.ClientResponseError:
    req_info = SimpleNamespace(real_url="https://example.test")
    return aiohttp.ClientResponseError(
        request_info=req_info, history=(), status=status, message=message
    )


class FakeResponse:
    """Minimal async response object to exercise request helpers."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        json_body: object = None,
        text_body: str = "",
        raise_text: bool = False,
    ) -> None:
        self.status = status
        self._json_body = json_body
        self._text_body = text_body
        self._raise_text = raise_text
        self.headers = headers or {"Content-Type": "application/json"}
        self.reason = "reason"
        self.request_info = SimpleNamespace(real_url="https://example.test/request")
        self.history = ()

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                self.request_info,
                self.history,
                status=self.status,
                message=self.reason,
                headers=self.headers,
            )

    async def json(self):
        return self._json_body

    async def text(self) -> str:
        if self._raise_text:
            raise RuntimeError("text unavailable")
        return self._text_body


class FakeSession:
    """Stub aiohttp.ClientSession."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []
        self.cookie_jar = aiohttp.CookieJar()

    def request(self, method: str, url: str, allow_redirects: bool = True, **kwargs):
        if not self._responses:
            raise AssertionError("No response configured for request")
        resp = self._responses.pop(0)
        resp.request_info = SimpleNamespace(real_url=url)
        self.calls.append((method, url, kwargs))
        return resp


class StubSession:
    """Lightweight object exposing only cookie_jar for auth helper tests."""

    def __init__(self) -> None:
        self.cookie_jar = aiohttp.CookieJar()


def test_should_limit_enlighten_read_request_handles_bad_string_casts() -> None:
    class BadMethod:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    class BadUrl:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert api._should_limit_enlighten_read_request(BadMethod(), api.BASE_URL) is False
    assert api._should_limit_enlighten_read_request("GET", BadUrl()) is False


@pytest.mark.asyncio
async def test_enlighten_request_timeout_includes_limiter_queue(monkeypatch) -> None:
    limiter = asyncio.Semaphore(0)
    monkeypatch.setattr(api, "_enlighten_read_semaphore", limiter)
    session = FakeSession([FakeResponse(json_body={"ok": True})])

    with pytest.raises(TimeoutError):
        await api._request_json(
            session, "GET", f"{api.BASE_URL}/service/test", timeout=0.01
        )

    assert session.calls == []


@pytest.mark.asyncio
async def test_optional_enlighten_reads_reserve_capacity_for_core(monkeypatch) -> None:
    monkeypatch.setattr(api, "_enlighten_read_semaphore", asyncio.Semaphore(3))
    monkeypatch.setattr(api, "_enlighten_optional_read_semaphore", asyncio.Semaphore(2))
    release = asyncio.Event()
    first_optional_entered = asyncio.Event()
    second_optional_entered = asyncio.Event()
    third_optional_entered = asyncio.Event()
    core_entered = asyncio.Event()

    async def _optional(entered: asyncio.Event) -> None:
        with api.enlighten_optional_read_scope():
            async with api._enlighten_read_request_guard(  # noqa: SLF001
                "GET", f"{api.BASE_URL}/service/optional"
            ):
                entered.set()
                await release.wait()

    async def _core() -> None:
        async with api._enlighten_read_request_guard(  # noqa: SLF001
            "GET", f"{api.BASE_URL}/service/core"
        ):
            core_entered.set()
            await release.wait()

    first = asyncio.create_task(_optional(first_optional_entered))
    await asyncio.wait_for(first_optional_entered.wait(), timeout=1)
    second = asyncio.create_task(_optional(second_optional_entered))
    await asyncio.wait_for(second_optional_entered.wait(), timeout=1)
    third = asyncio.create_task(_optional(third_optional_entered))
    core = asyncio.create_task(_core())
    await asyncio.wait_for(core_entered.wait(), timeout=1)

    assert third_optional_entered.is_set() is False

    release.set()
    await asyncio.gather(first, second, third, core)
    assert third_optional_entered.is_set() is True


@pytest.mark.asyncio
async def test_optional_read_reauth_does_not_deadlock_on_held_limiter(
    monkeypatch,
) -> None:
    monkeypatch.setattr(api, "_enlighten_read_semaphore", asyncio.Semaphore(2))
    monkeypatch.setattr(api, "_enlighten_optional_read_semaphore", asyncio.Semaphore(1))
    session = FakeSession(
        [
            FakeResponse(status=401),
            FakeResponse(json_body={"refreshed": True}),
            FakeResponse(json_body={"ok": True}),
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", None, None, timeout=1)

    async def _reauth() -> bool:
        payload = await api._request_json(
            session,
            "GET",
            f"{api.BASE_URL}/login/refresh",
            timeout=0.2,
        )
        return payload == {"refreshed": True}

    client.set_reauth_callback(_reauth)
    with api.enlighten_optional_read_scope():
        payload = await asyncio.wait_for(
            client._json("GET", f"{api.BASE_URL}/service/optional"), timeout=0.5
        )

    assert payload == {"ok": True}
    assert [call[1] for call in session.calls] == [
        f"{api.BASE_URL}/service/optional",
        f"{api.BASE_URL}/login/refresh",
        f"{api.BASE_URL}/service/optional",
    ]


def test_cookie_header_from_map_empty() -> None:
    assert api._cookie_header_from_map(None) == ""
    assert api._cookie_header_from_map({}) == ""


def test_seed_cookie_jar_handles_missing_or_error() -> None:
    class NoJarSession:
        cookie_jar = None

    class DummySession:
        cookie_jar = object()

    class BadJar:
        def update_cookies(self, *args, **kwargs) -> None:
            raise RuntimeError("boom")

    class BadSession:
        cookie_jar = BadJar()

    api._seed_cookie_jar(NoJarSession(), {"a": "b"})
    api._seed_cookie_jar(DummySession(), {})
    api._seed_cookie_jar(BadSession(), {"a": "b"})


def test_extract_login_session_non_dict() -> None:
    assert api._extract_login_session(["session"]) == (None, None)


def test_too_many_active_sessions_detection() -> None:
    assert api._is_too_many_active_sessions_response(
        {"message": "Too many active sessions.."}
    )
    assert api._is_too_many_active_sessions_response(
        [{"error": "Too many current active sessions"}]
    )
    assert api._is_too_many_active_sessions_response(
        '{"error":{"message":"Too many active sessions.."}}'
    )
    assert not api._is_too_many_active_sessions_response("[not-json")
    assert not api._is_too_many_active_sessions_response({"message": "invalid login"})


@pytest.mark.asyncio
async def test_request_json_success_builds_kwargs() -> None:
    session = FakeSession([FakeResponse(json_body={"ok": True})])
    payload = await api._request_json(
        session,
        "GET",
        "https://example.test",
        timeout=5,
        headers={"X-Test": "1"},
        data={"form": "value"},
        json_data={"payload": "value"},
    )
    assert payload == {"ok": True}
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://example.test"
    assert kwargs["headers"]["X-Test"] == "1"
    assert kwargs["data"] == {"form": "value"}
    assert kwargs["json"] == {"payload": "value"}


@pytest.mark.asyncio
async def test_request_json_raises_on_server_error() -> None:
    session = FakeSession([FakeResponse(status=503)])
    with pytest.raises(api.EnlightenAuthUnavailable):
        await api._request_json(session, "GET", "https://example.test", timeout=5)


@pytest.mark.asyncio
async def test_request_json_raises_too_many_sessions_from_error_body() -> None:
    session = FakeSession(
        [
            FakeResponse(
                status=400,
                headers={"Content-Type": "application/json"},
                text_body='{"message":"Too many active sessions.."}',
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthTooManySessions):
        await api._request_json(session, "POST", api.LOGIN_URL, timeout=5)


@pytest.mark.asyncio
async def test_request_json_error_body_text_unavailable_raises_response_error() -> None:
    session = FakeSession(
        [
            FakeResponse(
                status=400,
                headers={"Content-Type": "application/json"},
                raise_text=True,
            )
        ]
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await api._request_json(session, "POST", api.LOGIN_URL, timeout=5)


@pytest.mark.asyncio
async def test_request_json_raises_too_many_sessions_from_success_payload() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "application/json"},
                json_body={"message": "Too many active sessions.."},
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthTooManySessions):
        await api._request_json(session, "POST", api.LOGIN_URL, timeout=5)


@pytest.mark.asyncio
async def test_request_json_rejects_non_json_content() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/plain"},
                json_body={"ignored": True},
                text_body="Unexpected content" * 10,
            )
        ]
    )
    with pytest.raises(api.EnlightenAuthUnavailable) as err:
        await api._request_json(session, "GET", "https://example.test", timeout=5)
    assert "text/plain" in str(err.value)


@pytest.mark.asyncio
async def test_request_json_raises_too_many_sessions_from_non_json_body() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/plain"},
                text_body="Too many active sessions..",
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthTooManySessions):
        await api._request_json(session, "POST", api.LOGIN_URL, timeout=5)


@pytest.mark.asyncio
async def test_request_mfa_json_allows_text_json() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/plain"},
                text_body='{"success": true}',
            )
        ]
    )
    payload = await api._request_mfa_json(
        session, "GET", "https://example.test", timeout=5
    )
    assert payload == {"success": True}


@pytest.mark.asyncio
async def test_request_mfa_json_raises_too_many_sessions_from_json_payload() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "application/json"},
                json_body={"message": "Too many active sessions.."},
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthTooManySessions):
        await api._request_mfa_json(session, "POST", api.MFA_VALIDATE_URL, timeout=5)


@pytest.mark.asyncio
async def test_request_mfa_json_raises_too_many_sessions_from_text_payload() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/plain"},
                text_body='{"message":"Too many active sessions.."}',
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthTooManySessions):
        await api._request_mfa_json(session, "POST", api.MFA_VALIDATE_URL, timeout=5)


@pytest.mark.asyncio
async def test_request_mfa_json_raises_too_many_sessions_from_error_body() -> None:
    session = FakeSession(
        [
            FakeResponse(
                status=400,
                headers={"Content-Type": "application/json"},
                text_body='{"message":"Too many active sessions.."}',
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthTooManySessions):
        await api._request_mfa_json(session, "POST", api.MFA_VALIDATE_URL, timeout=5)


@pytest.mark.asyncio
async def test_request_mfa_json_error_body_text_unavailable_raises_response_error() -> (
    None
):
    session = FakeSession(
        [
            FakeResponse(
                status=400,
                headers={"Content-Type": "application/json"},
                raise_text=True,
            )
        ]
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await api._request_mfa_json(session, "POST", api.MFA_VALIDATE_URL, timeout=5)


@pytest.mark.asyncio
async def test_request_mfa_json_raises_too_many_sessions_from_escaped_text_json() -> (
    None
):
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/plain"},
                text_body='{"message":"Too many active sess\\u0069ons.."}',
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthTooManySessions):
        await api._request_mfa_json(session, "POST", api.MFA_VALIDATE_URL, timeout=5)


@pytest.mark.asyncio
async def test_request_mfa_json_raises_too_many_sessions_from_json_string() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/plain"},
                text_body='"Too many active sess\\u0069ons.."',
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthTooManySessions):
        await api._request_mfa_json(session, "POST", api.MFA_VALIDATE_URL, timeout=5)


@pytest.mark.asyncio
async def test_request_mfa_json_allows_empty_body() -> None:
    session = FakeSession([FakeResponse(status=204)])
    payload = await api._request_mfa_json(
        session, "GET", "https://example.test", timeout=5
    )
    assert payload == {}


@pytest.mark.asyncio
async def test_request_mfa_json_rejects_non_json_text() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/plain"},
                text_body="Not JSON",
            )
        ]
    )
    with pytest.raises(api.EnlightenAuthUnavailable):
        await api._request_mfa_json(session, "GET", "https://example.test", timeout=5)


@pytest.mark.asyncio
async def test_request_mfa_json_builds_kwargs() -> None:
    session = FakeSession([FakeResponse(json_body={"ok": True})])
    payload = await api._request_mfa_json(
        session,
        "POST",
        "https://example.test",
        timeout=5,
        headers={"X-Test": "1"},
        data={"form": "value"},
    )
    assert payload == {"ok": True}
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == "https://example.test"
    assert kwargs["headers"]["X-Test"] == "1"
    assert kwargs["data"] == {"form": "value"}


@pytest.mark.asyncio
async def test_request_mfa_json_raises_on_server_error() -> None:
    session = FakeSession([FakeResponse(status=503)])
    with pytest.raises(api.EnlightenAuthUnavailable):
        await api._request_mfa_json(session, "GET", "https://example.test", timeout=5)


@pytest.mark.asyncio
async def test_request_mfa_json_empty_text_body_returns_empty() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/plain"},
                text_body="   ",
            )
        ]
    )
    payload = await api._request_mfa_json(
        session, "GET", "https://example.test", timeout=5
    )
    assert payload == {}


def test_mfa_headers_adds_xsrf_and_cookie() -> None:
    headers = api._mfa_headers({"XSRF-TOKEN": "token123", "a": "b"})
    assert headers["X-CSRF-Token"] == "token123"
    assert "Cookie" in headers


def test_login_form_headers_match_browser_flow() -> None:
    assert api._login_form_headers() == {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": api.BASE_URL,
        "Referer": f"{api.BASE_URL}/",
        "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
    }


def test_login_headers_match_xhr_flow() -> None:
    assert api._login_headers() == {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": f"{api.BASE_URL}/",
        "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
    }


def test_extract_login_session_from_cookies_uses_cookie_and_jwt_fallback() -> None:
    payload = {"data": {"session_id": "sid-cookie"}}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    manager_token = f"hdr.{payload_b64.rstrip('=')}.sig"

    assert api._extract_login_session_from_cookies(
        {
            "_enlighten_4_session": "sid123",
            "enlighten_manager_token_production": "jwt123",
        }
    ) == ("sid123", "jwt123")
    assert api._extract_login_session_from_cookies(
        {"enlighten_manager_token_production": manager_token}
    ) == ("sid-cookie", manager_token)
    assert api._extract_login_session_from_cookies({}) == (None, None)


@pytest.mark.asyncio
async def test_submit_login_form_uses_browser_flow_and_reads_cookies() -> None:
    payload = {"data": {"session_id": "sid-cookie"}}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    manager_token = f"hdr.{payload_b64.rstrip('=')}.sig"
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/html"},
                text_body="<html></html>",
            )
        ]
    )
    session.cookie_jar.update_cookies(
        {
            "_enlighten_4_session": "sid123",
            "enlighten_manager_token_production": manager_token,
        },
        response_url=URL(api.BASE_URL),
    )

    session_id, token = await api._submit_login_form(
        session, "user@example.com", "secret", timeout=5
    )

    assert (session_id, token) == ("sid123", manager_token)
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url == api.LOGIN_FORM_URL
    assert kwargs["headers"] == api._login_form_headers()
    assert kwargs["data"] == {
        "user[email]": "user@example.com",
        "user[password]": "secret",
    }


@pytest.mark.asyncio
async def test_submit_login_form_raises_unavailable_on_server_error() -> None:
    session = FakeSession(
        [
            FakeResponse(
                status=503,
                headers={"Content-Type": "text/html"},
                text_body="<html></html>",
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthUnavailable):
        await api._submit_login_form(session, "user@example.com", "secret", timeout=5)


@pytest.mark.asyncio
async def test_submit_login_form_raises_too_many_sessions() -> None:
    session = FakeSession(
        [
            FakeResponse(
                status=400,
                headers={"Content-Type": "application/json"},
                text_body='{"message":"Too many active sessions.."}',
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthTooManySessions):
        await api._submit_login_form(session, "user@example.com", "secret", timeout=5)


@pytest.mark.asyncio
async def test_submit_login_form_raises_too_many_sessions_from_success_body() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/html"},
                text_body='{"message":"Too many active sessions.."}',
            )
        ]
    )

    with pytest.raises(api.EnlightenAuthTooManySessions):
        await api._submit_login_form(session, "user@example.com", "secret", timeout=5)


@pytest.mark.asyncio
async def test_submit_login_form_ignores_unreadable_success_body() -> None:
    session = FakeSession(
        [
            FakeResponse(
                headers={"Content-Type": "text/html"},
                raise_text=True,
            )
        ]
    )

    assert await api._submit_login_form(
        session, "user@example.com", "secret", timeout=5
    ) == (None, None)


@pytest.mark.asyncio
async def test_async_authenticate_success_with_jwt_fallback(monkeypatch) -> None:
    login_headers: list[dict[str, str]] = []
    site_headers: list[dict[str, str]] = []

    async def fake_request_json(
        session: StubSession,
        method: str,
        url: str,
        *,
        timeout: int,
        headers: dict[str, str] | None = None,
        data=None,
        json_data=None,
    ):
        if url == api.LOGIN_URL:
            login_headers.append(headers or {})
            session.cookie_jar.update_cookies(
                {
                    "XSRF-TOKEN": "xsrf123",
                    "enlighten_session": "cookie123",
                },
                response_url=URL(api.BASE_URL),
            )
            return {"session_id": "sid123"}
        if url == api.SELF_TOKEN_URL:
            token = _build_jwt(1_700_000_001)
            return {"token": token}
        if url == api.SITE_SEARCH_URL:
            site_headers.append(headers or {})
            return {"sites": [{"id": "9001", "title": "Garage"}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    session = StubSession()
    tokens, sites = await api.async_authenticate(session, "user@example.com", "secret")

    assert tokens.access_token is not None
    assert tokens.token_expires_at == 1_700_000_001
    assert tokens.cookie and "enlighten_session" in tokens.cookie
    assert sites and sites[0].site_id == "9001"
    assert login_headers == [
        {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": f"{api.BASE_URL}/",
            "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
        }
    ]
    assert any("Authorization" in hdr for hdr in site_headers)
    assert all(hdr.get("Accept") == "*/*" for hdr in site_headers if hdr)
    assert all(hdr.get("X-CSRF-Token") == "xsrf123" for hdr in site_headers if hdr)


@pytest.mark.asyncio
async def test_async_authenticate_falls_back_to_form_login_on_406(monkeypatch) -> None:
    login_headers: list[dict[str, str]] = []
    fallback_calls: list[tuple[str, str, int]] = []
    site_headers: list[dict[str, str]] = []

    async def fake_request_json(
        session: StubSession,
        method: str,
        url: str,
        *,
        timeout: int,
        headers: dict[str, str] | None = None,
        data=None,
        json_data=None,
    ):
        if url == api.LOGIN_URL:
            login_headers.append(headers or {})
            raise _make_cre(406, "Not Acceptable")
        if url == api.SELF_TOKEN_URL:
            return {"token": "token123", "expires_at": 1_700_000_000}
        if url == api.SITE_SEARCH_URL:
            site_headers.append(headers or {})
            return {"sites": [{"id": 7812456, "title": "Garage"}]}
        raise AssertionError(f"Unexpected URL: {url}")

    async def fake_submit_login_form(
        session: StubSession,
        email: str,
        password: str,
        *,
        timeout: int,
    ) -> tuple[str | None, str | None]:
        fallback_calls.append((email, password, timeout))
        session.cookie_jar.update_cookies(
            {
                "_enlighten_4_session": "sid123",
                "XSRF-TOKEN": "xsrf123",
            },
            response_url=URL(api.BASE_URL),
        )
        return ("sid123", None)

    monkeypatch.setattr(api, "_request_json", fake_request_json)
    monkeypatch.setattr(api, "_submit_login_form", fake_submit_login_form)

    session = StubSession()
    tokens, sites = await api.async_authenticate(session, "user@example.com", "secret")

    assert tokens.session_id == "sid123"
    assert tokens.access_token == "token123"
    assert sites and sites[0].site_id == "7812456"
    assert login_headers == [api._login_headers()]
    assert fallback_calls == [("user@example.com", "secret", api.DEFAULT_AUTH_TIMEOUT)]
    assert site_headers and site_headers[0]["Accept"] == "*/*"
    assert site_headers[0]["X-CSRF-Token"] == "xsrf123"


@pytest.mark.asyncio
async def test_async_authenticate_form_login_401_maps_to_invalid_credentials(
    monkeypatch,
) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(406, "Not Acceptable")

    async def fake_submit_login_form(*args, **kwargs):
        raise _make_cre(401)

    monkeypatch.setattr(api, "_request_json", fake_request_json)
    monkeypatch.setattr(api, "_submit_login_form", fake_submit_login_form)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_form_login_re_raises_other_response_errors(
    monkeypatch,
) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(406, "Not Acceptable")

    async def fake_submit_login_form(*args, **kwargs):
        raise _make_cre(500)

    monkeypatch.setattr(api, "_request_json", fake_request_json)
    monkeypatch.setattr(api, "_submit_login_form", fake_submit_login_form)

    with pytest.raises(aiohttp.ClientResponseError):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_form_login_client_error_maps_to_unavailable(
    monkeypatch,
) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(406, "Not Acceptable")

    async def fake_submit_login_form(*args, **kwargs):
        raise aiohttp.ClientConnectionError()

    monkeypatch.setattr(api, "_request_json", fake_request_json)
    monkeypatch.setattr(api, "_submit_login_form", fake_submit_login_form)

    with pytest.raises(api.EnlightenAuthUnavailable):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_form_login_manager_token_without_session_fails(
    monkeypatch,
) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(406, "Not Acceptable")

    async def fake_submit_login_form(*args, **kwargs):
        return (None, "jwt")

    monkeypatch.setattr(api, "_request_json", fake_request_json)
    monkeypatch.setattr(api, "_submit_login_form", fake_submit_login_form)

    with pytest.raises(api.EnlightenAuthInvalidCredentials, match="Missing session"):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_form_login_without_auth_state_fails(
    monkeypatch,
) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(406, "Not Acceptable")

    async def fake_submit_login_form(*args, **kwargs):
        return (None, None)

    monkeypatch.setattr(api, "_request_json", fake_request_json)
    monkeypatch.setattr(api, "_submit_login_form", fake_submit_login_form)

    with pytest.raises(api.EnlightenAuthInvalidCredentials, match="Unexpected login"):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_invalid_login_credentials(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(401)

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "wrong")


@pytest.mark.asyncio
async def test_async_authenticate_client_error(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise aiohttp.ClientConnectionError()

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthUnavailable):
        await api.async_authenticate(StubSession(), "user@example.com", "oops")


@pytest.mark.asyncio
async def test_async_authenticate_re_raises_other_login_errors(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(500)

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(aiohttp.ClientResponseError):
        await api.async_authenticate(StubSession(), "user@example.com", "oops")


@pytest.mark.asyncio
async def test_async_authenticate_requires_mfa(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies(
                {
                    "login_otp_nonce": "nonce123",
                    "_enlighten_4_session": "preauth",
                },
                response_url=URL(api.BASE_URL),
            )
            return {"success": True, "isBlocked": False}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthMFARequired) as err:
        await api.async_authenticate(StubSession(), "user@example.com", "secret")
    assert err.value.tokens is not None
    assert err.value.tokens.raw_cookies


@pytest.mark.asyncio
async def test_async_authenticate_requires_mfa_flag(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            return {"requires_mfa": True}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthMFARequired):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_blocked_account(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            return {"isBlocked": True}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_manager_token_missing_session(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            return {"manager_token": "jwt"}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_success_without_nonce(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            return {"success": True}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_unexpected_response(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            return {"foo": "bar"}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_validate_login_otp_success(monkeypatch) -> None:
    async def fake_request_mfa_json(session, method, url, **kwargs):
        if url != api.MFA_VALIDATE_URL:
            raise AssertionError(f"Unexpected URL: {url}")
        session.cookie_jar.update_cookies(
            {"_enlighten_4_session": "auth", "XSRF-TOKEN": "xsrf123"},
            response_url=URL(api.BASE_URL),
        )
        return {
            "message": "success",
            "session_id": "sid123",
            "manager_token": "jwt",
        }

    async def fake_request_json(session, method, url, **kwargs):
        if url == api.SELF_TOKEN_URL:
            return {"token": "token123"}
        if url == api.SITE_SEARCH_URL:
            return {"sites": [{"id": 1}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_mfa_json)
    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_validate_login_otp(
        StubSession(),
        "user@example.com",
        "123456",
        {"login_otp_nonce": "nonce123"},
    )

    assert tokens.access_token == "token123"
    assert sites and sites[0].site_id == "1"


@pytest.mark.asyncio
async def test_async_validate_login_otp_invalid(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"isValid": False, "isBlocked": False}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidOTP):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "000000",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_blocked(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"isValid": False, "isBlocked": True}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthOTPBlocked):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "000000",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_missing_credentials() -> None:
    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_validate_login_otp(
            StubSession(),
            " ",
            "123456",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_invalid_credentials_error(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(401)

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "123456",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_bad_request_maps_invalid(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(400)

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidOTP):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "123456",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_rate_limited_maps_blocked(
    monkeypatch,
) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(429)

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthOTPBlocked):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "123456",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_success_without_session_falls_back(
    monkeypatch,
) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"message": "success"}

    tokens = api.AuthTokens(cookie="jar=1")
    sites = [api.SiteInfo(site_id="1", name="Garage")]

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)
    monkeypatch.setattr(
        api, "_build_tokens_and_sites", AsyncMock(return_value=(tokens, sites))
    )

    out_tokens, out_sites = await api.async_validate_login_otp(
        StubSession(),
        "user@example.com",
        "123456",
        {"login_otp_nonce": "nonce123"},
    )

    assert out_tokens == tokens
    assert out_sites == sites


@pytest.mark.asyncio
async def test_async_validate_login_otp_recovery_failure(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"message": "success"}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)
    monkeypatch.setattr(
        api,
        "_build_tokens_and_sites",
        AsyncMock(side_effect=api.EnlightenAuthInvalidCredentials()),
    )

    with pytest.raises(api.EnlightenAuthInvalidOTP):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "123456",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_re_raises_other_errors(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(500)

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(aiohttp.ClientResponseError):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "123456",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_client_error(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise aiohttp.ClientConnectionError()

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthUnavailable):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "123456",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_missing_session_with_manager(
    monkeypatch,
) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"manager_token": "jwt"}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "123456",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_validate_login_otp_missing_session(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"message": "ok"}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidOTP):
        await api.async_validate_login_otp(
            StubSession(),
            "user@example.com",
            "123456",
            {"login_otp_nonce": "nonce123"},
        )


@pytest.mark.asyncio
async def test_async_resend_login_otp_rotates_cookie(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url != api.MFA_RESEND_URL:
            raise AssertionError(f"Unexpected URL: {url}")
        session.cookie_jar.update_cookies(
            {"login_otp_nonce": "nonce456"},
            response_url=URL(api.BASE_URL),
        )
        return {"success": True, "isBlocked": False}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    tokens = await api.async_resend_login_otp(
        StubSession(), {"login_otp_nonce": "nonce123"}
    )

    assert tokens.raw_cookies
    assert tokens.raw_cookies.get("login_otp_nonce") == "nonce456"


@pytest.mark.asyncio
async def test_async_resend_login_otp_blocked(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"success": False, "isBlocked": True}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthOTPBlocked):
        await api.async_resend_login_otp(StubSession(), {"login_otp_nonce": "nonce123"})


@pytest.mark.asyncio
async def test_async_resend_login_otp_invalid_response(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"success": False, "isBlocked": False}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_resend_login_otp(StubSession(), {"login_otp_nonce": "nonce123"})


@pytest.mark.asyncio
async def test_async_resend_login_otp_rate_limited(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(429)

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthOTPBlocked):
        await api.async_resend_login_otp(StubSession(), {"login_otp_nonce": "nonce123"})


@pytest.mark.asyncio
async def test_async_resend_login_otp_unexpected_response(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"foo": "bar"}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_resend_login_otp(StubSession(), {"login_otp_nonce": "nonce123"})


@pytest.mark.asyncio
async def test_async_resend_login_otp_reuses_existing_cookie(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {"success": True}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)
    monkeypatch.setattr(
        api, "_serialize_cookie_jar", lambda *_args, **_kwargs: ("", {})
    )

    tokens = await api.async_resend_login_otp(
        StubSession(), {"login_otp_nonce": "nonce123"}
    )

    assert tokens.raw_cookies
    assert tokens.raw_cookies.get("login_otp_nonce") == "nonce123"


@pytest.mark.asyncio
async def test_async_resend_login_otp_empty_response(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        return {}

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    tokens = await api.async_resend_login_otp(
        StubSession(), {"login_otp_nonce": "nonce123"}
    )

    assert tokens.raw_cookies
    assert tokens.raw_cookies.get("login_otp_nonce") == "nonce123"


@pytest.mark.asyncio
async def test_async_resend_login_otp_invalid_credentials_error(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(401)

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_resend_login_otp(StubSession(), {"login_otp_nonce": "nonce123"})


@pytest.mark.asyncio
async def test_async_resend_login_otp_re_raises_other_errors(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise _make_cre(500)

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(aiohttp.ClientResponseError):
        await api.async_resend_login_otp(StubSession(), {"login_otp_nonce": "nonce123"})


@pytest.mark.asyncio
async def test_async_resend_login_otp_client_error(monkeypatch) -> None:
    async def fake_request_json(*args, **kwargs):
        raise aiohttp.ClientOSError()

    monkeypatch.setattr(api, "_request_mfa_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthUnavailable):
        await api.async_resend_login_otp(StubSession(), {"login_otp_nonce": "nonce123"})


@pytest.mark.asyncio
async def test_async_authenticate_token_endpoint_invalid_credentials(
    monkeypatch,
) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {"session_id": "sid123"}
        if url == api.SELF_TOKEN_URL:
            raise _make_cre(403)
        raise AssertionError("Site discovery should not be reached")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_token_endpoint_missing(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {"session_id": "sid123"}
        if url == api.SELF_TOKEN_URL:
            raise _make_cre(404)
        if url == api.SITE_SEARCH_URL:
            return {"sites": [{"id": 1}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(
        StubSession(), "user@example.com", "secret"
    )
    assert tokens.access_token is None
    assert sites and sites[0].site_id == "1"


@pytest.mark.asyncio
async def test_async_authenticate_token_endpoint_generic_error(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {"session_id": "sid123"}
        if url == api.SELF_TOKEN_URL:
            raise _make_cre(500)
        if url == api.SITE_SEARCH_URL:
            return {"sites": [{"id": 2}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(
        StubSession(), "user@example.com", "secret"
    )
    assert tokens.access_token is None
    assert sites and sites[0].site_id == "2"


@pytest.mark.asyncio
async def test_async_authenticate_token_endpoint_unavailable(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {"session_id": "sid123"}
        if url == api.SELF_TOKEN_URL:
            raise api.EnlightenAuthUnavailable("unavailable")
        if url == api.SITE_SEARCH_URL:
            return {"sites": [{"id": "3"}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(
        StubSession(), "user@example.com", "secret"
    )
    assert tokens.access_token is None
    assert sites and sites[0].site_id == "3"


@pytest.mark.asyncio
async def test_async_authenticate_token_endpoint_client_error(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {"session_id": "sid123"}
        if url == api.SELF_TOKEN_URL:
            raise aiohttp.ClientOSError()
        if url == api.SITE_SEARCH_URL:
            return {"sites": [{"id": "4"}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(
        StubSession(), "user@example.com", "secret"
    )
    assert tokens.access_token is None
    assert sites and sites[0].site_id == "4"


@pytest.mark.asyncio
async def test_async_authenticate_site_discovery_invalid_credentials(
    monkeypatch,
) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {}
        raise _make_cre(401)

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    with pytest.raises(api.EnlightenAuthInvalidCredentials):
        await api.async_authenticate(StubSession(), "user@example.com", "secret")


@pytest.mark.asyncio
async def test_async_authenticate_site_discovery_errors_continue(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {}
        if url == api.SITE_SEARCH_URL:
            raise api.EnlightenAuthUnavailable("down")
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(
        StubSession(), "user@example.com", "secret"
    )
    assert tokens.access_token is None
    assert sites == []


@pytest.mark.asyncio
async def test_async_authenticate_site_discovery_handles_client_error(
    monkeypatch,
) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {}
        if url == api.SITE_SEARCH_URL:
            raise _make_cre(404)
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(
        StubSession(), "user@example.com", "secret"
    )
    assert tokens.access_token is None
    assert sites == []


@pytest.mark.asyncio
async def test_async_authenticate_site_discovery_client_error(monkeypatch) -> None:
    async def fake_request_json(session, method, url, **kwargs):
        if url == api.LOGIN_URL:
            session.cookie_jar.update_cookies({}, response_url=URL(api.BASE_URL))
            return {}
        if url == api.SITE_SEARCH_URL:
            raise aiohttp.ClientError()
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(api, "_request_json", fake_request_json)

    tokens, sites = await api.async_authenticate(
        StubSession(), "user@example.com", "secret"
    )
    assert tokens.access_token is None
    assert sites == []


@pytest.mark.asyncio
async def test_async_fetch_chargers_requires_site_id() -> None:
    tokens = api.AuthTokens(cookie="")
    assert await api.async_fetch_chargers(MagicMock(), "", tokens) == []


@pytest.mark.asyncio
async def test_async_fetch_chargers_handles_summary_error(monkeypatch) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.summary_v2 = AsyncMock(side_effect=RuntimeError("boom"))

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)

    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    chargers = await api.async_fetch_chargers(MagicMock(), "site", tokens)
    assert chargers == []


@pytest.mark.asyncio
async def test_async_fetch_chargers_returns_normalized(monkeypatch) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.summary_v2 = AsyncMock(
                return_value={"data": [{"serial": "EV123", "name": "Garage"}]}
            )

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)

    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    chargers = await api.async_fetch_chargers(MagicMock(), "site", tokens)
    assert chargers and chargers[0].serial == "EV123"


@pytest.mark.asyncio
async def test_async_fetch_devices_inventory_requires_site_id() -> None:
    tokens = api.AuthTokens(cookie="")
    assert await api.async_fetch_devices_inventory(MagicMock(), "", tokens) == {}


@pytest.mark.asyncio
async def test_async_fetch_devices_inventory_handles_non_dict_payload(
    monkeypatch,
) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.devices_inventory = AsyncMock(return_value=["bad"])

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_devices_inventory(MagicMock(), "site", tokens)
    assert payload is None


@pytest.mark.asyncio
async def test_async_fetch_devices_inventory_handles_fetch_error(monkeypatch) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.devices_inventory = AsyncMock(side_effect=RuntimeError("boom"))

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_devices_inventory(MagicMock(), "site", tokens)
    assert payload is None


@pytest.mark.asyncio
async def test_async_fetch_devices_inventory_returns_dict_payload(monkeypatch) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.devices_inventory = AsyncMock(return_value={"result": []})

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_devices_inventory(MagicMock(), "site", tokens)
    assert payload == {"result": []}


@pytest.mark.asyncio
async def test_async_fetch_inverters_inventory_requires_site_id() -> None:
    tokens = api.AuthTokens(cookie="")
    assert await api.async_fetch_inverters_inventory(MagicMock(), "", tokens) == {}


@pytest.mark.asyncio
async def test_async_fetch_inverters_inventory_handles_non_dict_payload(
    monkeypatch,
) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.inverters_inventory = AsyncMock(return_value=["bad"])

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_inverters_inventory(MagicMock(), "site", tokens)
    assert payload is None


@pytest.mark.asyncio
async def test_async_fetch_inverters_inventory_handles_fetch_error(monkeypatch) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.inverters_inventory = AsyncMock(side_effect=RuntimeError("boom"))

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_inverters_inventory(MagicMock(), "site", tokens)
    assert payload is None


@pytest.mark.asyncio
async def test_async_fetch_inverters_inventory_returns_dict_payload(
    monkeypatch,
) -> None:
    fetch_mock = AsyncMock(return_value={"inverters": [{"serial_number": "INV-1"}]})

    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.inverters_inventory = fetch_mock

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_inverters_inventory(MagicMock(), "site", tokens)
    assert payload == {"inverters": [{"serial_number": "INV-1"}]}
    fetch_mock.assert_awaited_once_with(limit=1000, offset=0, search="")


@pytest.mark.asyncio
async def test_async_fetch_inverters_inventory_paginates(monkeypatch) -> None:
    fetch_mock = AsyncMock(
        side_effect=[
            {
                "total": 2,
                "inverters": [{"serial_number": "INV-1"}],
            },
            {
                "total": 2,
                "inverters": [{"serial_number": "INV-2"}],
            },
        ]
    )

    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.inverters_inventory = fetch_mock

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_inverters_inventory(MagicMock(), "site", tokens)
    assert payload == {
        "total": 2,
        "inverters": [
            {"serial_number": "INV-1"},
            {"serial_number": "INV-2"},
        ],
    }
    assert [call.kwargs for call in fetch_mock.await_args_list] == [
        {"limit": 1000, "offset": 0, "search": ""},
        {"limit": 1000, "offset": 1, "search": ""},
    ]


@pytest.mark.asyncio
async def test_async_fetch_inverters_inventory_paginates_nested_result_payload(
    monkeypatch,
) -> None:
    fetch_mock = AsyncMock(
        side_effect=[
            {
                "total": 2,
                "result": {"inverters": [{"serial_number": "INV-1"}]},
            },
            {
                "total": 2,
                "result": {"inverters": []},
            },
        ]
    )

    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.inverters_inventory = fetch_mock

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_inverters_inventory(MagicMock(), "site", tokens)
    assert payload == {
        "total": 2,
        "result": {"inverters": [{"serial_number": "INV-1"}]},
    }


@pytest.mark.asyncio
async def test_async_fetch_inverters_inventory_legacy_kwargs_fallback_failure(
    monkeypatch,
) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.inverters_inventory = AsyncMock(side_effect=self._fetch)

        async def _fetch(self, *args, **kwargs):
            if kwargs:
                raise TypeError("legacy signature")
            raise RuntimeError("boom")

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_inverters_inventory(MagicMock(), "site", tokens)
    assert payload is None


@pytest.mark.asyncio
async def test_async_fetch_inverters_inventory_stops_when_legacy_signature_rejects_next_page(
    monkeypatch,
) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.inverters_inventory = AsyncMock(side_effect=self._fetch)

        async def _fetch(self, *args, **kwargs):
            if kwargs.get("offset") == 0:
                return {
                    "total": 2,
                    "inverters": [{"serial_number": "INV-1"}],
                }
            raise TypeError("legacy signature")

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_inverters_inventory(MagicMock(), "site", tokens)
    assert payload == {
        "total": 2,
        "inverters": [{"serial_number": "INV-1"}],
    }


@pytest.mark.asyncio
async def test_async_fetch_inverters_inventory_handles_assembly_error(
    monkeypatch,
) -> None:
    class ExplodingDict(dict):
        def get(self, key, default=None):
            if key == "inverters":
                raise RuntimeError("explode")
            return super().get(key, default)

    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.inverters_inventory = AsyncMock(
                return_value=ExplodingDict({"total": 1, "inverters": []})
            )

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_inverters_inventory(MagicMock(), "site", tokens)
    assert payload is None


@pytest.mark.asyncio
async def test_async_fetch_inverters_inventory_handles_missing_inverter_shapes(
    monkeypatch,
) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.inverters_inventory = AsyncMock(
                return_value={"total": 0, "result": []}
            )

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_inverters_inventory(MagicMock(), "site", tokens)
    assert payload == {"total": 0, "result": []}


@pytest.mark.asyncio
async def test_async_fetch_hems_devices_requires_site_id() -> None:
    tokens = api.AuthTokens(cookie="")
    assert await api.async_fetch_hems_devices(MagicMock(), "", tokens) == {}


@pytest.mark.asyncio
async def test_async_fetch_hems_devices_handles_non_dict_payload(monkeypatch) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.hems_devices = AsyncMock(return_value=["bad"])

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_hems_devices(MagicMock(), "site", tokens)
    assert payload is None


@pytest.mark.asyncio
async def test_async_fetch_hems_devices_handles_fetch_error(monkeypatch) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.hems_devices = AsyncMock(side_effect=RuntimeError("boom"))

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_hems_devices(MagicMock(), "site", tokens)
    assert payload is None


@pytest.mark.asyncio
async def test_async_fetch_hems_devices_returns_dict_payload(monkeypatch) -> None:
    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.hems_devices = AsyncMock(return_value={"data": {"hems-devices": {}}})

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    payload = await api.async_fetch_hems_devices(MagicMock(), "site", tokens)
    assert payload == {"data": {"hems-devices": {}}}


@pytest.mark.asyncio
async def test_async_fetch_hems_devices_passes_refresh_flag(monkeypatch) -> None:
    fetch_mock = AsyncMock(return_value={"data": {}})

    class StubClient:
        def __init__(self, *args, **kwargs) -> None:
            self.hems_devices = fetch_mock

    monkeypatch.setattr(api, "EnphaseEVClient", StubClient)
    tokens = api.AuthTokens(cookie="cook", access_token="tok")
    await api.async_fetch_hems_devices(
        MagicMock(),
        "site",
        tokens,
        refresh_data=True,
    )

    fetch_mock.assert_awaited_once_with(refresh_data=True)
