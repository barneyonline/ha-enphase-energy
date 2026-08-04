"""Comprehensive tests for EnphaseEVClient behavior."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import datetime
import hashlib
from http.cookies import SimpleCookie
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from multidict import CIMultiDict
from yarl import URL

from custom_components.enphase_ev import api
from custom_components.enphase_ev.const import (
    AUTH_APP_SETTING,
    AUTH_RFID_SETTING,
    DEFAULT_CHARGE_LEVEL_SETTING,
    GREEN_BATTERY_SETTING,
    PHASE_SWITCH_CONFIG_SETTING,
)
from custom_components.enphase_ev.request_metrics import request_metrics_scope

TEST_EVSE_SERIAL = "EVSE-SERIAL-0001"


def _make_cre(status: int, message: str = "error") -> aiohttp.ClientResponseError:
    req_info = SimpleNamespace(real_url="https://example.test/path")
    return aiohttp.ClientResponseError(
        request_info=req_info, history=(), status=status, message=message
    )


class _FakeResponse:
    def __init__(self, *, status: int, json_body: object, text_body: str = "") -> None:
        self.status = status
        self._json_body = json_body
        self._text_body = text_body
        self.request_info = SimpleNamespace(real_url="https://example.test/path")
        self.url = URL("https://example.test/path")
        self.history: tuple = ()
        self.reason = "reason"
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, object] = {}

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def json(self):
        if isinstance(self._json_body, Exception):
            raise self._json_body
        return self._json_body

    async def text(self) -> str:
        if isinstance(self._text_body, Exception):
            raise self._text_body
        return self._text_body


class _FakeSession:
    """Session that returns pre-seeded responses."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []
        self.cookie_jar = aiohttp.CookieJar()

    def request(self, method: str, url: str, **kwargs):
        if not self._responses:
            raise AssertionError("No response prepared")
        resp = self._responses.pop(0)
        self.calls.append((method, url, kwargs))
        return resp


class _DefaultSession:
    """Session stub that safely absorbs unexpected request calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.cookie_jar = SimpleNamespace(filter_cookies=lambda _url: {})

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return _FakeResponse(status=200, json_body={}, text_body="")


class _BadCookie:
    def split(self, *_args, **_kwargs):
        raise RuntimeError("cannot split")


class _FakeUrlLibResponse:
    def __init__(
        self,
        *,
        status: int,
        body: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body.encode()
        self.headers = headers or {}
        self.reason = "reason"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _make_client(
    session: _FakeSession | MagicMock | None = None,
) -> api.EnphaseEVClient:
    session = session or _DefaultSession()
    return api.EnphaseEVClient(session, "SITE", "EAUTH", "COOKIE")


def _make_token(payload: dict) -> str:
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    payload_b64 = payload_b64.rstrip("=")
    return f"header.{payload_b64}.sig"


def test_request_count_reset_and_snapshot() -> None:
    client = _make_client()

    client._request_count = 3  # noqa: SLF001
    assert client.request_count == 3

    client.reset_request_count()

    assert client.request_count == 0


def test_safe_response_error_message_handles_header_failures() -> None:
    class BadHeaders:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("header unavailable")

    message = api._safe_response_error_message(  # noqa: SLF001
        status=502,
        reason="bad gateway",
        headers=BadHeaders(),
        body_text="",
    )

    assert "status=502" in message
    assert "reason=bad gateway" in message
    assert "content_type" not in message


def test_scheduler_error_code_ignores_unexpected_payload_shapes() -> None:
    assert api._scheduler_error_context_from_text(None) == (None, None)  # noqa: SLF001
    assert api._scheduler_error_context_from_text(
        json.dumps(["bad"])
    ) == (  # noqa: SLF001
        None,
        None,
    )
    assert api._scheduler_error_context_from_text(
        json.dumps({"error": "bad"})
    ) == (  # noqa: SLF001
        None,
        None,
    )

    err = _make_cre(400, json.dumps({"error": "bad"}))
    assert api._scheduler_error_code(err) is None  # noqa: SLF001


def _make_optional_payload_error(endpoint: str) -> api.InvalidPayloadError:
    return api.InvalidPayloadError(
        "Invalid JSON response",
        status=200,
        content_type="text/html",
        endpoint=endpoint,
    )


def test_update_credentials_manages_headers() -> None:
    client = _make_client()
    client.update_credentials(
        eauth="TOKEN",
        cookie="a=1; XSRF-TOKEN=xsrf; enlighten_manager_token_production=bearer",
    )
    assert client._h["Cookie"].startswith("a=1")
    assert client._h["e-auth-token"] == "TOKEN"
    assert client._h["X-CSRF-Token"] == "xsrf"

    client.update_credentials(cookie="")
    assert "Cookie" not in client._h
    assert "X-CSRF-Token" not in client._h

    client._cookie = _BadCookie()
    client.update_credentials()
    assert "X-CSRF-Token" not in client._h


def test_battery_config_cookie_handles_bad_string_and_empty_cookie() -> None:
    client = _make_client()

    class _BadStringCookie:
        def __bool__(self) -> bool:
            return True

        def __str__(self) -> str:
            raise RuntimeError("boom")

    client._cookie = _BadStringCookie()  # noqa: SLF001

    assert client._battery_config_cookie() is None  # noqa: SLF001

    client.update_credentials(cookie="")
    assert client._battery_config_cookie() is None  # noqa: SLF001


def test_battery_config_cookie_header_xsrf_token_handles_quotes_and_decode_fallback(
    monkeypatch,
) -> None:
    client = _make_client()
    client.update_credentials(cookie='session=1; BP-XSRF-Token="quoted%20token"; a=1')

    assert (
        client._battery_config_cookie_header_xsrf_token() == "quoted token"
    )  # noqa: SLF001

    client.update_credentials(cookie='session=1; BP-XSRF-Token=""')
    assert client._battery_config_cookie_header_xsrf_token() is None  # noqa: SLF001

    client.update_credentials(cookie='session=1; BP-XSRF-Token="opaque"')
    monkeypatch.setattr(
        api, "unquote", lambda value: (_ for _ in ()).throw(ValueError(value))
    )
    assert client._battery_config_cookie_header_xsrf_token() == "opaque"  # noqa: SLF001


def test_battery_config_mixed_auth_headers_without_tokens_or_xsrf() -> None:
    client = api.EnphaseEVClient(_DefaultSession(), "SITE", None, None)

    headers = client._battery_config_mixed_auth_headers()  # noqa: SLF001

    assert headers["Authorization"] is None
    assert headers["e-auth-token"] is None
    assert headers["requestid"] is None
    assert headers["X-XSRF-Token"] is None
    assert headers["X-CSRF-Token"] is None
    assert headers["Cookie"] is None


def test_battery_config_mixed_auth_headers_uses_token_when_eauth_missing() -> None:
    token = _make_token({"user_id": "88"})
    client = api.EnphaseEVClient(
        _DefaultSession(),
        "SITE",
        None,
        f"session=1; enlighten_manager_token_production={token}",
    )

    headers = client._battery_config_mixed_auth_headers()  # noqa: SLF001

    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["e-auth-token"] == token
    assert headers["Username"] == "88"


def test_battery_config_cookie_merges_session_jar_cookies() -> None:
    session = _DefaultSession()
    session.cookie_jar = SimpleNamespace(
        filter_cookies=lambda _url: SimpleCookie("battery_profile_session=live-cookie")
    )
    client = api.EnphaseEVClient(
        session,
        "SITE",
        None,
        "session=1; locale=en-AU; BP-XSRF-Token=stale-token",
    )

    cookie_header = client._battery_config_cookie(include_xsrf=True)  # noqa: SLF001

    assert cookie_header is not None
    assert "session=1" in cookie_header
    assert "locale=en-AU" in cookie_header
    assert "battery_profile_session=live-cookie" in cookie_header
    assert "BP-XSRF-Token=stale-token" in cookie_header


def test_update_credentials_handles_xsrf_extractor_exception(monkeypatch) -> None:
    client = _make_client()
    client._h["X-CSRF-Token"] = "stale"

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_xsrf_token", _boom)
    client.update_credentials(cookie="a=1")
    assert "X-CSRF-Token" not in client._h


def test_site_web_referer_uses_app_version_cookie_when_available() -> None:
    client = _make_client()
    client.update_credentials(cookie="appVersion=3.4.0; a=1")

    assert client._site_web_referer("history") == (
        f"{api.BASE_URL}/web/SITE/history/graph/years?v=3.4.0"
    )
    assert client._site_web_referer("layout") == (
        f"{api.BASE_URL}/web/SITE/layout/graph/years?v=3.4.0"
    )
    assert client._site_web_graph_referer("today", graph_range="hours") == (
        f"{api.BASE_URL}/web/SITE/today/graph/hours?v=3.4.0"
    )


def test_root_and_today_header_helpers_use_browser_profiles() -> None:
    client = _make_client()
    client.update_credentials(cookie="appVersion=3.4.0; a=1")

    assert client._root_xhr_headers() == {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{api.BASE_URL}/",
        "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
        "Cookie": "appVersion=3.4.0; a=1",
        "e-auth-token": "EAUTH",
    }
    assert client._today_headers() == {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{api.BASE_URL}/web/SITE/today/graph/hours?v=3.4.0",
        "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
        "Cookie": "appVersion=3.4.0; a=1",
        "e-auth-token": "EAUTH",
    }
    assert client._today_json_headers() == {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{api.BASE_URL}/web/SITE/today/graph/hours?v=3.4.0",
        "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
        "Cookie": "appVersion=3.4.0; a=1",
        "e-auth-token": "EAUTH",
    }


def test_extract_xsrf_token_branches(monkeypatch) -> None:
    class BadTokenValue:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert (
        api._extract_xsrf_token(
            {"XSRF-TOKEN": BadTokenValue(), "BP-XSRF-Token": "bp%3Dtoken"}
        )
        == "bp=token"
    )
    assert api._extract_xsrf_token({"XSRF-TOKEN": '""', "BP-XSRF-Token": "bp"}) == "bp"

    monkeypatch.setattr(
        api,
        "unquote",
        lambda _value: (_ for _ in ()).throw(ValueError("decode-fail")),
    )
    assert api._extract_xsrf_token({"XSRF-TOKEN": "raw-token"}) == "raw-token"


def test_extract_xsrf_token_handles_bad_and_empty_cookie_values() -> None:
    class BadTokenValue:
        def __str__(self) -> str:
            raise ValueError("bad")

    assert api._extract_xsrf_token({"XSRF-TOKEN": BadTokenValue()}) is None
    assert api._extract_xsrf_token({"XSRF-TOKEN": '""'}) is None


def test_coerce_cookie_map_handles_defensive_branches() -> None:
    class _ItemsFail:
        def items(self):
            raise RuntimeError("boom")

    class _BadName:
        def __str__(self) -> str:
            raise ValueError("bad-name")

    class _BadValue:
        def __str__(self) -> str:
            raise ValueError("bad-value")

    class _CookieMap:
        def items(self):
            return [
                (_BadName(), "skip"),
                ("", "skip"),
                ("bad", _BadValue()),
                ("good", SimpleNamespace(value="value")),
            ]

    assert api._coerce_cookie_map(SimpleNamespace(items="not-callable")) == {}
    assert api._coerce_cookie_map(_ItemsFail()) == {}
    assert api._coerce_cookie_map(_CookieMap()) == {"good": "value"}


def test_cookie_map_from_header_handles_defensive_branches() -> None:
    class _BadCookieHeader:
        def __str__(self) -> str:
            raise ValueError("bad-cookie-header")

    assert api._cookie_map_from_header(_BadCookieHeader()) == {}
    assert api._cookie_map_from_header("") == {}
    assert api._cookie_map_from_header("session=1; ; invalid; other=2; blank=") == {
        "session": "1",
        "other": "2",
        "blank": "",
    }


def test_is_enphase_login_wall_handles_unprintable_payload() -> None:
    class _BadPayload:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert (
        api._is_enphase_login_wall(endpoint="/service/test", payload=_BadPayload())
        is False
    )


@pytest.mark.asyncio
async def test_text_response_returns_redirect_metadata() -> None:
    response = _FakeResponse(status=302, json_body={}, text_body="")
    response.headers = {"Location": "/systems/SITE/devices?status=active"}
    session = _FakeSession([response])
    client = _make_client(session)

    result = await client._text_response(  # noqa: SLF001
        "GET",
        "https://example.test/path",
        expected_statuses=(302,),
        allow_redirects=False,
    )

    assert result.status == 302
    assert result.location == "/systems/SITE/devices?status=active"
    assert result.url == "https://example.test/path"


@pytest.mark.asyncio
@pytest.mark.parametrize("json_body", [None, ValueError("empty response")])
async def test_json_accepts_empty_success_when_requested(json_body: object) -> None:
    response = _FakeResponse(
        status=200,
        json_body=json_body,
        text_body="",
    )
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    client = _make_client(_FakeSession([response]))

    result = await client._json(  # noqa: SLF001
        "PUT",
        "https://example.test/service/activation/envoys",
        allow_empty_success=True,
    )

    assert result == {}


@pytest.mark.asyncio
async def test_devices_tree_login_wall_raises_unauthorized() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                json_body=ValueError("invalid-json"),
                text_body=(
                    '<!DOCTYPE html><html lang="en"><head>'
                    '<meta http-equiv="X-UA-Compatible" content="IE=Edge,chrome=IE8">'
                    "<script>window.OptanonWrapper = function () {};</script>"
                    "</head></html>"
                ),
            )
        ]
    )
    session._responses[0].headers["Content-Type"] = "application/json; charset=utf-8"
    client = _make_client(session)

    with pytest.raises(api.EnphaseLoginWallUnauthorized):
        await client.devices_tree()


@pytest.mark.asyncio
async def test_ac_battery_client_methods_build_expected_requests() -> None:
    sleep_response = _FakeResponse(status=302, json_body={}, text_body="")
    sleep_response.headers = {"Location": "/systems/SITE/devices?status=active"}
    wake_response = _FakeResponse(status=302, json_body={}, text_body="")
    wake_response.headers = {"Location": "/systems/SITE/devices?status=active"}
    session = _FakeSession(
        [
            _FakeResponse(status=200, json_body={}, text_body="<html>devices</html>"),
            _FakeResponse(status=200, json_body={}, text_body="<html>detail</html>"),
            _FakeResponse(status=200, json_body={}, text_body="<html>events</html>"),
            _FakeResponse(status=200, json_body={}, text_body="<div>telemetry</div>"),
            sleep_response,
            wake_response,
        ]
    )
    client = _make_client(session)

    html = await client.ac_battery_devices_page()
    detail = await client.ac_battery_detail_page("BAT-1")
    events = await client.ac_battery_events_page("BAT-1")
    telemetry = await client.ac_battery_show_stat_data("BAT-1")
    sleep = await client.set_ac_battery_sleep("BAT-1", 25)
    wake = await client.set_ac_battery_wake("BAT-1")

    assert html == "<html>devices</html>"
    assert detail == "<html>detail</html>"
    assert events == "<html>events</html>"
    assert telemetry == "<div>telemetry</div>"
    assert sleep.location == "/systems/SITE/devices?status=active"
    assert wake.location == "/systems/SITE/devices?status=active"

    assert session.calls[0][1].endswith("/systems/SITE/devices?status=active")
    assert session.calls[1][1].endswith("/systems/SITE/ac_batteries/BAT-1")
    assert session.calls[2][1].endswith("/systems/SITE/ac_batteries/BAT-1/events")
    assert session.calls[3][1].endswith(
        "/systems/SITE/ac_batteries/BAT-1/show_stat_data"
    )
    assert "sleep_min_soc=25" in session.calls[4][1]
    assert session.calls[5][1].endswith("/systems/SITE/ac_batteries/BAT-1/wake")


@pytest.mark.asyncio
async def test_async_fetch_battery_site_settings_defensive_branches(
    monkeypatch,
) -> None:
    assert (
        await api.async_fetch_battery_site_settings(
            MagicMock(), "", api.AuthTokens("cookie", "sid", "token", 1)
        )
        == {}
    )

    client = AsyncMock()
    client.battery_site_settings = AsyncMock(return_value="bad")
    monkeypatch.setattr(api, "EnphaseEVClient", MagicMock(return_value=client))

    result = await api.async_fetch_battery_site_settings(
        MagicMock(), "SITE", api.AuthTokens("cookie", "sid", "token", 1)
    )

    assert result is None

    client.battery_site_settings = AsyncMock(return_value={"data": {"hasAcb": True}})
    assert await api.async_fetch_battery_site_settings(
        MagicMock(), "SITE", api.AuthTokens("cookie", "sid", "token", 1)
    ) == {"data": {"hasAcb": True}}


@pytest.mark.asyncio
async def test_text_response_retries_unauthorized_with_header_callback(
    monkeypatch,
) -> None:
    class BadURL:
        def __str__(self) -> str:
            return f"{api.BASE_URL}/path"

    first = _FakeResponse(status=401, json_body={}, text_body="")
    second = _FakeResponse(status=200, json_body={}, text_body="ok")
    session = _FakeSession([first, second])
    client = _make_client(session)
    client._reauth_cb = AsyncMock(return_value=True)  # noqa: SLF001
    ticks = iter((0.0, 0.1, 1.0, 1.2, 2.0, 2.3, 3.0, 3.4, 4.0, 4.5))
    monkeypatch.setattr(api, "monotonic", lambda: next(ticks))

    with request_metrics_scope("text_retry") as metrics:
        result = await client._text_response(  # noqa: SLF001
            "GET",
            BadURL(),
            expected_statuses=(200,),
            headers=lambda: {"Authorization": None, "X-Test": "1"},
        )

    assert result.text == "ok"
    assert session.calls[0][2]["headers"]["X-Test"] == "1"
    assert "Authorization" not in session.calls[0][2]["headers"]
    client._reauth_cb.assert_awaited_once()  # noqa: SLF001
    assert client.request_count == 2
    assert metrics.attempts == 2
    assert metrics.queue_s == pytest.approx(0.4)
    assert metrics.network_s == pytest.approx(0.6)
    assert metrics.parsing_s == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_text_response_expected_status_login_wall_raises_unauthorized() -> None:
    response = _FakeResponse(
        status=200,
        json_body={},
        text_body=(
            '<!DOCTYPE html><html lang="en"><head>'
            '<meta http-equiv="X-UA-Compatible" content="IE=Edge,chrome=IE8">'
            '<script>var otLang = "en"; window.OptanonWrapper = function () {};</script>'
            "</head></html>"
        ),
    )
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    session = _FakeSession([response])
    client = _make_client(session)

    with pytest.raises(api.EnphaseLoginWallUnauthorized) as exc_info:
        await client._text_response(  # noqa: SLF001
            "GET",
            "https://example.test/service/test",
            expected_statuses=(200,),
        )

    assert exc_info.value.endpoint == "/service/test"
    assert client.last_unauthorized_request == "GET /service/test"


@pytest.mark.asyncio
async def test_text_response_raises_client_error_with_structured_body_summary() -> None:
    response = _FakeResponse(
        status=500,
        json_body={},
        text_body="secret cloud failure for site SITE",
    )
    session = _FakeSession([response])
    client = _make_client(session)

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client._text_response("GET", "https://example.test/path")  # noqa: SLF001

    assert "body_length=34" in err.value.message
    assert "body_sha256=" not in err.value.message
    assert "secret cloud failure" not in err.value.message
    assert "SITE" not in err.value.message


@pytest.mark.asyncio
async def test_text_response_handles_body_read_failure() -> None:
    response = _FakeResponse(status=500, json_body={}, text_body=RuntimeError("boom"))
    session = _FakeSession([response])
    client = _make_client(session)

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client._text_response("GET", "https://example.test/path")  # noqa: SLF001

    assert err.value.message == (
        "HTTP error from Enphase endpoint (status=500, reason=reason)"
    )


@pytest.mark.asyncio
async def test_text_response_raises_unauthorized_without_successful_reauth() -> None:
    response = _FakeResponse(status=401, json_body={}, text_body="")
    session = _FakeSession([response])
    client = _make_client(session)
    client._reauth_cb = AsyncMock(return_value=False)  # noqa: SLF001

    with pytest.raises(api.Unauthorized):
        await client._text_response("GET", "https://example.test/path")  # noqa: SLF001


@pytest.mark.asyncio
async def test_text_response_success_login_wall_raises_unauthorized() -> None:
    response = _FakeResponse(
        status=200,
        json_body={},
        text_body=(
            '<!DOCTYPE html><html lang="en"><head>'
            '<meta http-equiv="X-UA-Compatible" content="IE=Edge,chrome=IE8">'
            "<script>window.OptanonWrapper = function () {};</script>"
            "</head></html>"
        ),
    )
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    session = _FakeSession([response])
    client = _make_client(session)

    with pytest.raises(api.EnphaseLoginWallUnauthorized) as exc_info:
        await client._text_response(  # noqa: SLF001
            "GET", "https://example.test/service/test"
        )

    assert exc_info.value.endpoint == "/service/test"
    assert client.last_unauthorized_request == "GET /service/test"


def test_cookie_names_from_header_returns_sorted_names() -> None:
    assert api._cookie_names_from_header("z=1; a=2; invalid; c=3") == [
        "a",
        "c",
        "z",
    ]


def test_request_failure_debug_family_matches_curated_endpoint_families() -> None:
    assert (
        api._request_failure_debug_family(
            "PUT",
            "/service/batteryConfig/api/v1/batterySettings/SITE",
        )
        == "BatteryConfig write"
    )
    assert (
        api._request_failure_debug_family(
            "PATCH",
            "/service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/"
            "SITE/SN/schedules",
        )
        == "EVSE control write"
    )
    assert (
        api._request_failure_debug_family(
            "POST",
            "/pv/settings/grid_state.json",
        )
        == "Grid control toggle"
    )
    assert api._request_failure_debug_family("GET", "/systems/SITE/summary") is None


def test_request_failure_debug_family_handles_bad_inputs() -> None:
    class _BadText:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert (
        api._request_failure_debug_family(_BadText(), "/systems/SITE/summary") is None
    )
    assert api._request_failure_debug_family("GET", _BadText()) is None


def test_xsrf_token_handles_empty_and_decode_fallback(monkeypatch) -> None:
    client = _make_client()
    client.update_credentials(cookie='XSRF-TOKEN=""; BP-XSRF-Token=bp%3Dtoken')
    assert client._xsrf_token() == "bp=token"

    monkeypatch.setattr(
        api,
        "unquote",
        lambda _value: (_ for _ in ()).throw(ValueError("decode-fail")),
    )
    client.update_credentials(cookie="XSRF-TOKEN=raw-token")
    assert client._xsrf_token() == "raw-token"


def test_xsrf_helpers_accept_lower_case_cookie_names() -> None:
    client = _make_client()
    client.update_credentials(cookie="bp-xsrf-token=bp%3Dtoken; other=1")

    assert client._xsrf_token() == "bp=token"


def test_system_dashboard_should_fallback_rejects_unexpected_errors() -> None:
    client = _make_client()
    assert (
        client._system_dashboard_is_optional_error(RuntimeError("boom")) is False
    )  # noqa: SLF001


def test_battery_config_auth_helpers_cover_token_and_cookie_fallback() -> None:
    token = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="")
    client._bp_xsrf_token = "dynamic-token"  # noqa: SLF001

    headers = client._battery_config_headers(include_xsrf=True)  # noqa: SLF001
    auth_token, user_id = client._battery_config_auth_context()  # noqa: SLF001

    assert auth_token == token
    assert user_id == "77"
    assert client._xsrf_token() == "dynamic-token"  # noqa: SLF001
    assert headers["e-auth-token"] == token
    assert headers["X-XSRF-Token"] == "dynamic-token"
    assert headers["Authorization"] is None
    assert headers["X-CSRF-Token"] is None
    assert headers["Cookie"] is None
    assert headers["X-Requested-With"] is None
    assert "requestid" in headers


def test_authorization_bearer_token_helper() -> None:
    assert api._authorization_bearer_token({}) is None
    assert api._authorization_bearer_token({"Authorization": "Basic abc"}) is None
    assert (
        api._authorization_bearer_token({"Authorization": "Bearer secret-token"})
        == "secret-token"
    )


def test_battery_config_headers_preserve_original_eauth_and_replace_stale_xsrf() -> (
    None
):
    bearer = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth="session-token",
        cookie=(
            "session=1; BP-XSRF-Token=stale-token; other=1; "
            f"enlighten_manager_token_production={bearer}"
        ),
    )
    client._bp_xsrf_token = "fresh-token"  # noqa: SLF001

    headers = client._battery_config_headers(include_xsrf=True)  # noqa: SLF001

    assert headers["e-auth-token"] == bearer
    assert headers["X-XSRF-Token"] == "fresh-token"
    assert headers["Authorization"] is None
    assert headers["X-CSRF-Token"] is None
    assert headers["Cookie"] is None
    assert headers["Username"] == "77"


def test_battery_config_header_debug_flags_cover_auth_modes() -> None:
    client = _make_client()

    assert client._battery_config_auth_source_label(None) == "none"  # noqa: SLF001
    assert (
        client._battery_config_auth_source_label("other") == "unknown"
    )  # noqa: SLF001

    client.update_credentials(
        eauth="shared-token",
        cookie="session=1; enlighten_manager_token_production=shared-token",
    )
    shared_flags = client._battery_config_header_debug_flags(  # noqa: SLF001
        {
            "Authorization": "Bearer shared-token",
            "e-auth-token": "shared-token",
        }
    )
    assert shared_flags["auth_mode"] == "dual_match"
    assert shared_flags["auth_source"] == "shared"

    client.update_credentials(
        eauth="access-token",
        cookie="session=1; enlighten_manager_token_production=manager-token",
    )
    mismatch_flags = client._battery_config_header_debug_flags(  # noqa: SLF001
        {
            "Authorization": "Bearer manager-token",
            "e-auth-token": "access-token",
        }
    )
    assert mismatch_flags["auth_mode"] == "dual_mismatch"
    assert mismatch_flags["auth_source"] == "mixed"

    client.update_credentials(eauth="access-only", cookie="session=1")
    assert (
        client._battery_config_auth_source_label("access-only") == "access_token"
    )  # noqa: SLF001
    access_only_flags = client._battery_config_header_debug_flags(  # noqa: SLF001
        {"e-auth-token": "access-only"}
    )
    assert access_only_flags["auth_mode"] == "eauth_only"
    assert access_only_flags["auth_source"] == "access_token"

    client.update_credentials(
        eauth="",
        cookie="session=1; enlighten_manager_token_production=manager-token",
    )
    bearer_only_flags = client._battery_config_header_debug_flags(  # noqa: SLF001
        {"Authorization": "Bearer manager-token"}
    )
    assert bearer_only_flags["auth_mode"] == "authorization_only"
    assert bearer_only_flags["auth_source"] == "manager_cookie"


def test_battery_config_single_auth_token_prefers_manager_cookie() -> None:
    bearer = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth="access-token",
        cookie=f"session=1; enlighten_manager_token_production={bearer}",
    )

    assert client._battery_config_single_auth_token() == bearer  # noqa: SLF001


def test_battery_config_user_id_for_token_prefers_override_then_context() -> None:
    bearer = _make_token({"user_id": "77"})
    override = _make_token({"user_id": "88"})
    client = _make_client()
    client.update_credentials(
        eauth="access-token",
        cookie=f"session=1; enlighten_manager_token_production={bearer}",
    )

    assert client._battery_config_user_id_for_token(override) == "88"  # noqa: SLF001
    assert client._battery_config_user_id_for_token(None) == "77"  # noqa: SLF001


def test_battery_config_headers_use_bearer_cookie_for_eauth_when_missing() -> None:
    bearer = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth="",
        cookie=f"session=1; enlighten_manager_token_production={bearer}",
    )

    headers = client._battery_config_headers()  # noqa: SLF001

    assert headers["Authorization"] is None
    assert headers["e-auth-token"] == bearer
    assert headers["X-CSRF-Token"] is None
    assert headers["Username"] == "77"


def test_battery_config_headers_match_official_web_shape() -> None:
    bearer = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth="access-token",
        cookie=(
            "session=1; XSRF-TOKEN=raw-token; "
            f"enlighten_manager_token_production={bearer}"
        ),
    )

    headers = client._battery_config_headers(include_xsrf=True)  # noqa: SLF001

    assert headers["Accept"] == "application/json, text/plain, */*"
    assert headers["Origin"] == "https://battery-profile-ui.enphaseenergy.com"
    assert headers["Referer"] == "https://battery-profile-ui.enphaseenergy.com/"
    assert headers["User-Agent"] == api._BATTERY_CONFIG_BROWSER_USER_AGENT
    assert headers["X-Requested-With"] is None
    assert headers["X-XSRF-Token"] == "raw-token"
    assert headers["X-CSRF-Token"] is None
    assert headers["Authorization"] is None
    assert headers["e-auth-token"] == bearer
    assert headers["Cookie"] is None
    assert headers["Username"] == "77"
    assert "requestid" in headers


def test_battery_config_headers_can_build_lean_fallback_shape() -> None:
    bearer = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth="access-token",
        cookie=(
            "session=1; XSRF-TOKEN=raw-token; "
            f"enlighten_manager_token_production={bearer}"
        ),
    )

    headers = client._battery_config_headers(  # noqa: SLF001
        include_xsrf=True,
        variant=api._BATTERY_CONFIG_VARIANT_LEAN,
    )

    assert headers["Authorization"] is None
    assert headers["e-auth-token"] is None
    assert headers["requestid"] is None
    assert headers["Username"] == "77"
    assert headers["X-XSRF-Token"] == "raw-token"
    assert headers["X-CSRF-Token"] is None
    assert headers["Cookie"] is None


def test_battery_config_headers_can_build_session_cookie_shape() -> None:
    bearer = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth="access-token",
        cookie=(
            "_enlighten_4_session=fresh-session; BP-XSRF-Token=raw-token; "
            f"enlighten_manager_token_production={bearer}"
        ),
    )

    headers = client._battery_config_headers(  # noqa: SLF001
        include_xsrf=True,
        variant=api._BATTERY_CONFIG_VARIANT_SESSION_COOKIE,
    )

    assert headers["Authorization"] is None
    assert headers["e-auth-token"] is None
    assert headers["requestid"]
    assert headers["Username"] == "77"
    assert headers["X-XSRF-Token"] == "raw-token"
    assert headers["X-CSRF-Token"] is None
    assert "_enlighten_4_session=fresh-session" in headers["Cookie"]
    assert "BP-XSRF-Token=raw-token" in headers["Cookie"]


def test_battery_config_session_cookie_read_preserves_existing_xsrf_cookie() -> None:
    bearer = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth="access-token",
        cookie=(
            "_enlighten_4_session=fresh-session; BP-XSRF-Token=read-token; "
            f"enlighten_manager_token_production={bearer}"
        ),
    )

    headers = client._battery_config_headers(  # noqa: SLF001
        variant=api._BATTERY_CONFIG_VARIANT_SESSION_COOKIE,
    )

    assert "BP-XSRF-Token=read-token" in headers["Cookie"]
    assert "X-XSRF-Token" not in headers


def test_battery_config_headers_drop_cookie_when_none_available() -> None:
    client = _make_client()
    client.update_credentials(cookie="session=1")
    client._cookie = ""  # noqa: SLF001

    headers = client._battery_config_headers()  # noqa: SLF001

    assert headers["Cookie"] is None


def test_battery_config_headers_clear_auth_and_username_without_tokens() -> None:
    client = _make_client()
    client.update_credentials(eauth="", cookie="")
    client._h["Username"] = "stale-user"  # noqa: SLF001

    headers = client._battery_config_headers()  # noqa: SLF001

    assert headers["Authorization"] is None
    assert headers["e-auth-token"] is None
    assert "Username" not in headers


def test_battery_config_variant_order_skips_session_shape_without_cookie() -> None:
    client = _make_client()
    client.update_credentials(cookie="")
    client._battery_config_variant_cache[  # noqa: SLF001
        client._battery_config_variant_cache_key("profile")  # noqa: SLF001
    ] = api._BATTERY_CONFIG_VARIANT_SESSION_COOKIE

    assert client._battery_config_variant_order("profile") == [  # noqa: SLF001
        api._BATTERY_CONFIG_VARIANT_PRIMARY,
        api._BATTERY_CONFIG_VARIANT_LEAN,
    ]


def test_merge_request_headers_returns_base_for_non_dict_overrides() -> None:
    client = _make_client()

    assert client._merge_request_headers(
        {"Accept": "application/json"}, None
    ) == {  # noqa: SLF001
        "Accept": "application/json"
    }
    assert client._merge_request_headers(  # noqa: SLF001
        {"Accept": "application/json"},
        "invalid",
    ) == {"Accept": "application/json"}


def test_system_dashboard_query_type_helper_branches() -> None:
    class _BadText:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert api._system_dashboard_query_type(None) is None
    assert api._system_dashboard_query_type(_BadText()) is None
    assert api._system_dashboard_query_type(" - ") is None
    assert api._system_dashboard_query_type("System Controller") is None
    assert api._system_dashboard_query_type("meter") == "meters"
    assert api._system_dashboard_query_type("encharge") == "encharges"


def test_request_label_formats_path_and_query() -> None:
    assert (
        api._request_label("get", "https://example.test/path?foo=1")
        == "GET /path?foo=1"
    )
    label = api._request_label(
        "get",
        "https://example.test/path?next=private:cursor:795:-1&foo=1",
    )
    assert label == "GET /path?next=[redacted]&foo=1"
    assert "private:cursor:795:-1" not in label


def test_request_label_handles_bad_method_and_raw_url_fallback(monkeypatch) -> None:
    class _BadMethod:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        api,
        "URL",
        lambda _value: (_ for _ in ()).throw(ValueError("bad url")),
    )

    assert api._request_label(_BadMethod(), "not a url") == "REQUEST not a url"


def test_request_label_handles_url_and_raw_text_failures(monkeypatch) -> None:
    class _BadUrl:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        api,
        "URL",
        lambda _value: (_ for _ in ()).throw(ValueError("bad url")),
    )

    assert api._request_label("post", _BadUrl()) == "POST"


def test_request_label_returns_method_when_url_has_no_path(monkeypatch) -> None:
    class _UrlNoPath:
        def __init__(self, _value) -> None:
            self.path = ""
            self.query_string = ""

    monkeypatch.setattr(
        api,
        "URL",
        _UrlNoPath,
    )

    assert api._request_label("patch", "https://example.test") == "PATCH"


def test_bearer_extraction_prefers_cookie() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=jwt-token; other=value"
    )
    assert client._bearer() == "jwt-token"

    client._cookie = _BadCookie()
    assert client._bearer() is None


def test_control_headers_fallbacks() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=jwt-token; other=value",
        eauth="EAUTH",
    )
    assert client._control_headers() == {"Authorization": "Bearer jwt-token"}

    client.update_credentials(cookie="")
    assert client._control_headers() == {"Authorization": "Bearer EAUTH"}

    client.update_credentials(eauth="")
    assert client._control_headers() == {}


def test_public_scheduler_and_header_helpers() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=jwt-token; other=value",
        eauth="EAUTH",
    )
    assert client.scheduler_bearer() == "jwt-token"
    assert client.has_scheduler_bearer() is True
    assert client.base_header_names() == sorted(client._h.keys())
    assert client.control_headers() == {"Authorization": "Bearer jwt-token"}


def test_redact_headers_masks_sensitive_fields() -> None:
    headers = {
        "Cookie": "secret",
        "Authorization": "Bearer secret",
        "X-Test": "value",
        "e-auth-token": "token",
        "X-CSRF-Token": "csrf",
        "X-XSRF-Token": "xsrf",
        "Username": "123456",
        "Referer": "https://enlighten.enphaseenergy.com/pv/systems/SITE/summary",
    }
    redacted = _make_client(MagicMock())._redact_headers(headers)
    assert redacted["Cookie"] == "[redacted]"
    assert redacted["Authorization"] == "[redacted]"
    assert redacted["e-auth-token"] == "[redacted]"
    assert redacted["X-CSRF-Token"] == "[redacted]"
    assert redacted["X-XSRF-Token"] == "[redacted]"
    assert redacted["Username"] == "[redacted]"
    assert redacted["X-Test"] == "value"
    assert redacted["Referer"].endswith("/pv/systems/[site]/summary")


def test_truncate_debug_identifier_branches() -> None:
    client = _make_client()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert client._truncate_debug_identifier(None) is None  # noqa: SLF001
    assert client._truncate_debug_identifier(BadStr()) is None  # noqa: SLF001
    assert client._truncate_debug_identifier("") is None  # noqa: SLF001
    assert client._truncate_debug_identifier("ab") == "[redacted]"  # noqa: SLF001
    assert client._truncate_debug_identifier("abcde") == "a...e"  # noqa: SLF001
    assert (
        client._truncate_debug_identifier("ABCDEFGHIJ") == "ABCD...GHIJ"
    )  # noqa: SLF001


def test_debug_request_context_redacts_site_and_truncates_identifiers() -> None:
    client = _make_client()

    context = client._debug_request_context(  # noqa: SLF001
        "GET",
        (
            "https://example.test/app-api/SITE/devices.json"
            "?device-uid=DEVICE-UID-123456789"
            "&start_date=2026-03-13"
            "&userId=person@example.com"
            "&siteId=SITE"
            "&serialNumber=SERIAL-123456"
        ),
        requested_device_uid="DEVICE-UID-123456789",
        site_date="2026-03-13",
    )

    assert context == {
        "request": (
            "GET /app-api/[site]/devices.json"
            "?device-uid=DEVI...6789"
            "&start_date=2026-03-13"
            "&userId=[redacted]"
            "&siteId=[redacted]"
            "&serialNumber=[redacted]"
        ),
        "query_keys": [
            "device-uid",
            "start_date",
            "userId",
            "siteId",
            "serialNumber",
        ],
        "has_device_uid": True,
        "date_key": "start_date",
        "normalized_site_date": "2026-03-13",
        "requested_device_uid": "DEVI...6789",
    }


def test_redact_debug_text_removes_site_and_truncates_device_uid() -> None:
    client = _make_client()

    redacted = client._redact_debug_text(  # noqa: SLF001
        'Failure for site SITE device DEVICE-UID-123456789: {"reason":"bad"}',
        device_uid="DEVICE-UID-123456789",
    )

    assert redacted == 'Failure for site [site] device DEVI...6789: {"reason":"bad"}'


def test_redact_debug_text_defensive_branches_and_kv_redaction() -> None:
    client = _make_client()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    client._site = BadStr()  # noqa: SLF001

    assert client._redact_debug_text(BadStr()) == ""  # noqa: SLF001
    assert client._redact_debug_text("") == ""  # noqa: SLF001

    redacted = client._redact_debug_text(  # noqa: SLF001
        "serial=SERIAL-123456 deviceUid=DEVICE-UID-123456789 "
        "email=person@example.com note=" + ("x" * 300),
        device_uid=BadStr(),
    )

    assert "SERIAL-123456" not in redacted
    assert "person@example.com" not in redacted
    assert "DEVI...6789" in redacted
    assert "[redacted]" in redacted
    assert redacted.endswith("...")


def test_debug_query_key_kind_and_value_branches() -> None:
    client = _make_client()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert client._debug_query_key_kind(BadStr()) == "text"  # noqa: SLF001
    assert client._debug_query_key_kind("") == "text"  # noqa: SLF001
    assert client._debug_query_key_kind("device_uid") == "truncate"  # noqa: SLF001
    assert client._debug_query_key_kind("siteId") == "redact"  # noqa: SLF001
    assert client._debug_query_value("siteId", "SITE") == "[redacted]"  # noqa: SLF001
    assert (  # noqa: SLF001
        client._debug_query_value("device_uid", "DEVICE-UID-123456789") == "DEVI...6789"
    )
    assert client._debug_query_value("note", "") == "[redacted]"  # noqa: SLF001


def test_debug_error_message_sanitizes_structured_identifiers() -> None:
    client = _make_client()

    message = client._debug_error_message(  # noqa: SLF001
        json.dumps(
            {
                "reason": "Invalid request for SITE",
                "device_uid": "DEVICE-UID-123456789",
                "siteId": "SITE",
                "serialNumber": "SERIAL-123456",
                "email": "person@example.com",
                "nested": {
                    "hems-device-id": "HEMS-DEVICE-123456",
                    "status": "bad",
                },
            }
        ),
        device_uid="DEVICE-UID-123456789",
    )

    assert "DEVICE-UID-123456789" not in message
    assert "SERIAL-123456" not in message
    assert "person@example.com" not in message
    assert '"device_uid": "DEVI...6789"' in message
    assert '"siteId": "[redacted]"' in message
    assert '"serialNumber": "[redacted]"' in message
    assert '"hems-device-id": "[redacted]"' in message
    assert '"email": "[redacted]"' in message


def test_debug_sanitize_payload_handles_sequences_bad_keys_and_empty_values() -> None:
    client = _make_client()

    class BadKey:
        def __str__(self) -> str:
            raise ValueError("boom")

    sanitized = client._debug_sanitize_payload(  # noqa: SLF001
        {
            BadKey(): "value",
            "items": ["", None],
            "tuple_items": ("alpha", "beta"),
            "device_uid": "DEVICE-UID-123456789",
        },
        device_uid="DEVICE-UID-123456789",
    )

    assert sanitized["[invalid]"] == "[redacted]"
    assert sanitized["items"] == ["[redacted]", None]
    assert sanitized["tuple_items"] == ["alpha", "beta"]
    assert sanitized["device_uid"] == "DEVI...6789"
    assert (
        client._debug_sanitize_payload("", key="note") == "[redacted]"
    )  # noqa: SLF001
    assert client._debug_sanitize_payload(None, key="note") is None  # noqa: SLF001


def test_debug_error_message_defensive_and_dump_failure_paths(monkeypatch) -> None:
    client = _make_client()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    original_dumps = api.json.dumps

    def _raise_dumps(*_args, **_kwargs):
        raise ValueError("dump-fail")

    monkeypatch.setattr(api.json, "dumps", _raise_dumps)

    structured = client._debug_error_message(  # noqa: SLF001
        {"device_uid": "DEVICE-UID-123456789"},
        device_uid="DEVICE-UID-123456789",
    )
    parsed = client._debug_error_message(  # noqa: SLF001
        original_dumps({"device_uid": "DEVICE-UID-123456789"}),
        device_uid="DEVICE-UID-123456789",
    )

    assert "DEVI...6789" in structured
    assert "DEVI...6789" in parsed
    assert client._debug_error_message(BadStr()) == ""  # noqa: SLF001
    assert client._debug_error_message("") == ""  # noqa: SLF001


def test_debug_request_context_defensive_branches() -> None:
    client = _make_client()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert client._debug_request_context(  # noqa: SLF001
        BadStr(), BadStr(), requested_device_uid="DEVICE-UID-123456789"
    ) == {"request": "REQUEST", "requested_device_uid": "DEVI...6789"}

    client._site = BadStr()  # noqa: SLF001
    context = client._debug_request_context(  # noqa: SLF001
        "GET",
        "https://example.test/app-api/SITE/devices.json?foo=bar",
    )
    assert context == {
        "request": "GET /app-api/SITE/devices.json?foo=bar",
        "query_keys": ["foo"],
    }


def test_evse_timeseries_unavailable_helper_branches() -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert (
        api.is_evse_timeseries_unavailable_error(
            "Service unavailable",
            503,
            "https://x/service/timeseries/evse/timeseries/daily_energy",
        )
        is True
    )
    assert api.is_evse_timeseries_unavailable_error(
        "EVSE timeseries unavailable",
        None,
        None,
    )
    assert api.is_evse_timeseries_unavailable_error(
        "daily_energy service unavailable",
        None,
        None,
    )
    assert api.is_evse_timeseries_unavailable_error(
        "lifetime_energy service unavailable",
        None,
        None,
    )
    assert api.is_evse_timeseries_unavailable_error(BadStr(), None, BadStr()) is False


def test_evse_timeseries_normalizer_helpers(monkeypatch) -> None:
    class BadSerial:
        def __str__(self) -> str:
            raise ValueError("bad-serial")

    class BadUnit:
        def __str__(self) -> str:
            raise ValueError("bad-unit")

    class BadFloat(float):
        def __float__(self):
            raise ValueError("bad-float")

    client = _make_client()

    assert client._normalize_evse_timeseries_serial(BadSerial()) is None
    now_dt = datetime.datetime(2026, 3, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)
    assert client._parse_evse_timeseries_date_key(now_dt) == "2026-03-11"
    assert client._parse_evse_timeseries_date_key(now_dt.date()) == "2026-03-11"
    assert client._parse_evse_timeseries_date_key(1_700_000_000_000) == "2023-11-14"
    assert client._parse_evse_timeseries_date_key(BadFloat(1.0)) is None
    assert client._parse_evse_timeseries_date_key([]) is None
    assert client._parse_evse_timeseries_date_key("") is None
    assert client._parse_evse_timeseries_date_key("2026-03-11 not-iso") == "2026-03-11"
    assert client._parse_evse_timeseries_date_key("2026/03/11 broken") is None
    assert client._parse_evse_timeseries_date_key("bad") is None

    assert client._coerce_evse_timeseries_energy("bad") is None
    assert client._coerce_evse_timeseries_energy(
        "1000", unit_hint=BadUnit()
    ) == pytest.approx(1000.0)
    assert client._coerce_evse_timeseries_energy(
        "1000", unit_hint="Wh"
    ) == pytest.approx(1.0)
    assert client._normalize_evse_timeseries_metadata([]) == {}


def test_evse_timeseries_payload_normalizer_branches(monkeypatch) -> None:
    client = _make_client()

    mapping_days, current_value = client._daily_values_from_mapping(
        {"skip": "value", "2026-03-11": "bad", "energy_wh": 500}
    )
    assert mapping_days == {}
    assert current_value == pytest.approx(0.5)

    original_parser = client._parse_evse_timeseries_date_key
    monkeypatch.setattr(
        api.EnphaseEVClient,
        "_parse_evse_timeseries_date_key",
        staticmethod(
            lambda value: "bad-date" if value == "force-bad" else original_parser(value)
        ),
    )
    values, current = client._daily_values_from_sequence(
        [
            {"energy_kwh": "bad"},
            {"energy_kwh": 1.25},
            "bad",
            2.5,
        ],
        start_date_value="force-bad",
    )
    assert values == {}
    assert current == pytest.approx(2.5)

    values, current = client._daily_values_from_sequence(
        [1.0, 2.0],
        start_date_value="2026-03-10",
    )
    assert values["2026-03-10"] == pytest.approx(1.0)
    assert values["2026-03-11"] == pytest.approx(2.0)
    assert current is None

    daily_entry = client._normalize_evse_daily_entry(
        "SERIAL-1",
        {"serial": "SERIAL-2", "data": {"2026-03-11": 3.0}},
    )
    assert daily_entry["serial"] == "SERIAL-2"
    assert daily_entry["energy_kwh"] == pytest.approx(3.0)
    assert client._normalize_evse_daily_entry("SERIAL-1", [4.0])[
        "current_value_kwh"
    ] == pytest.approx(4.0)
    assert client._normalize_evse_daily_entry("SERIAL-1", "bad") is None

    lifetime_entry = client._normalize_evse_lifetime_entry(
        "SERIAL-1",
        {"serial_number": "SERIAL-2", "values": [1.5]},
    )
    assert lifetime_entry["serial"] == "SERIAL-2"
    assert lifetime_entry["energy_kwh"] == pytest.approx(1.5)
    assert client._normalize_evse_lifetime_entry("SERIAL-1", 9.5)[
        "energy_kwh"
    ] == pytest.approx(9.5)
    assert client._normalize_evse_lifetime_entry("SERIAL-1", {"data": {}}) is None

    payload = client._normalize_evse_timeseries_payload(
        {
            "data": {
                "results": [
                    "skip",
                    {"energy_kwh": 1.0},
                    {"serial": "SERIAL-3", "energy_kwh": 5.0},
                ]
            }
        },
        daily=False,
    )
    assert payload["SERIAL-3"]["serial"] == "SERIAL-3"
    assert payload["SERIAL-3"]["energy_kwh"] == pytest.approx(5.0)

    class BadKey:
        def __str__(self) -> str:
            raise ValueError("bad-key")

    payload = client._normalize_evse_timeseries_payload(
        {BadKey(): {"energy_kwh": 1.0}, "SERIAL-4": {}},
        daily=False,
    )
    assert payload == {}
    assert client._normalize_evse_timeseries_payload("bad", daily=False) is None


def test_invalid_payload_error_defaults_summary_when_blank() -> None:
    err = api.InvalidPayloadError("   ")
    assert err.summary == "Invalid JSON response"
    assert str(err) == "Invalid JSON response"


@pytest.mark.asyncio
async def test_json_merges_headers_and_returns_payload(monkeypatch) -> None:
    session = _FakeSession([_FakeResponse(status=200, json_body={"ok": True})])
    client = api.EnphaseEVClient(session, "SITE", None, "COOKIE")
    ticks = iter((10.0, 10.25, 20.0, 20.5, 30.0, 30.75))
    monkeypatch.setattr(api, "monotonic", lambda: next(ticks))

    with request_metrics_scope("json") as metrics:
        payload = await client._json(
            "GET",
            f"{api.BASE_URL}/service/test",
            headers={"Extra": "1"},
            params={"q": "1"},
        )

    assert payload == {"ok": True}
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert kwargs["headers"]["Extra"] == "1"
    assert kwargs["headers"]["Cookie"] == "COOKIE"
    assert metrics.attempts == 1
    assert metrics.queue_s == pytest.approx(0.25)
    assert metrics.network_s == pytest.approx(0.5)
    assert metrics.parsing_s == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_json_queue_timeout_records_wait_without_attempt(monkeypatch) -> None:
    session = _FakeSession([_FakeResponse(status=200, json_body={"ok": True})])
    client = api.EnphaseEVClient(session, "SITE", None, None, timeout=0.01)
    monkeypatch.setattr(api, "_enlighten_read_semaphore", asyncio.Semaphore(0))
    ticks = iter((10.0, 10.5))
    monkeypatch.setattr(api, "monotonic", lambda: next(ticks))

    with request_metrics_scope("queue_timeout") as metrics:
        with pytest.raises(TimeoutError):
            await client._json("GET", f"{api.BASE_URL}/service/test")

    assert metrics.attempts == 0
    assert metrics.queue_s == pytest.approx(0.5)
    assert metrics.network_s == 0
    assert metrics.parsing_s == 0


@pytest.mark.asyncio
async def test_json_network_failure_records_header_wait(monkeypatch) -> None:
    class _EnterFailureResponse(_FakeResponse):
        async def __aenter__(self):
            raise RuntimeError("connection failed")

    session = _FakeSession([_EnterFailureResponse(status=200, json_body={"ok": True})])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    monkeypatch.setattr(api, "_enlighten_read_semaphore", asyncio.Semaphore(2))
    ticks = iter((0.0, 0.1, 1.0, 1.3))
    monkeypatch.setattr(api, "monotonic", lambda: next(ticks))

    with request_metrics_scope("network_failure") as metrics:
        with pytest.raises(RuntimeError, match="connection failed"):
            await client._json("GET", f"{api.BASE_URL}/service/test")

    assert client.request_count == 1
    assert metrics.attempts == 1
    assert metrics.queue_s == pytest.approx(0.1)
    assert metrics.network_s == pytest.approx(0.3)
    assert metrics.parsing_s == 0


@pytest.mark.asyncio
async def test_json_raises_unauthorized() -> None:
    session = _FakeSession([_FakeResponse(status=401, json_body={})])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(api.Unauthorized):
        await client._json("GET", "https://example.test")


@pytest.mark.asyncio
async def test_json_login_wall_raises_login_wall_unauthorized() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                json_body=ValueError("invalid-json"),
                text_body=(
                    '<!DOCTYPE html><html lang="en"><head>'
                    '<meta http-equiv="X-UA-Compatible" content="IE=Edge,chrome=IE8">'
                    '<script>var otLang = "en"; window.OptanonWrapper = function () {};</script>'
                    "</head></html>"
                ),
            )
        ]
    )
    session._responses[0].headers["Content-Type"] = "application/json; charset=utf-8"
    client = api.EnphaseEVClient(session, "SITE", None, None)

    with pytest.raises(api.EnphaseLoginWallUnauthorized) as exc_info:
        await client._json("GET", "https://example.test/service/test")

    assert exc_info.value.endpoint == "/service/test"
    assert client.last_unauthorized_request == "GET /service/test"


@pytest.mark.asyncio
async def test_evse_feature_flags_login_wall_raises_unauthorized() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                json_body=ValueError("invalid-json"),
                text_body=(
                    '<!DOCTYPE html><html lang="en"><head>'
                    '<meta http-equiv="X-UA-Compatible" content="IE=Edge,chrome=IE8">'
                    "<script>window.OptanonWrapper = function () {};</script>"
                    "</head></html>"
                ),
            )
        ]
    )
    session._responses[0].headers["Content-Type"] = "application/json; charset=utf-8"
    client = api.EnphaseEVClient(session, "SITE", None, None)

    with pytest.raises(api.EnphaseLoginWallUnauthorized):
        await client.evse_feature_flags()


@pytest.mark.asyncio
async def test_optional_heat_pump_events_login_wall_raises_auth_failure() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                json_body=ValueError("invalid-json"),
                text_body=(
                    "<!DOCTYPE html><html><head>"
                    "<script>window.OptanonWrapper = function () {};</script>"
                    "</head></html>"
                ),
            )
        ]
    )
    session._responses[0].headers["Content-Type"] = "application/json; charset=utf-8"
    client = api.EnphaseEVClient(session, "SITE", None, None)

    with pytest.raises(api.EnphaseLoginWallUnauthorized):
        await client.heat_pump_events_json("device-1")


@pytest.mark.asyncio
async def test_json_reauth_retry(monkeypatch) -> None:
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(status=200, json_body={"ok": True}),
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", None, None)
    attempts: list[bool] = []

    async def _reauth() -> bool:
        attempts.append(True)
        return True

    client.set_reauth_callback(_reauth)
    payload = await client._json("GET", "https://example.test")
    assert payload == {"ok": True}
    assert len(attempts) == 1
    assert len(session.calls) == 2
    assert client.last_unauthorized_request == "GET /"


@pytest.mark.asyncio
async def test_json_reauth_retry_logs_request_label(caplog) -> None:
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(status=200, json_body={"ok": True}),
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", None, None)

    async def _reauth() -> bool:
        return True

    client.set_reauth_callback(_reauth)
    with caplog.at_level(logging.DEBUG):
        payload = await client._json("GET", "https://example.test/path?foo=1")

    assert payload == {"ok": True}
    assert (
        "Received 401 for GET /path?foo=1; attempting stored-credential refresh"
        in caplog.text
    )
    assert (
        "Stored-credential refresh succeeded for GET /path?foo=1; retrying request"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_json_reauth_retry_rebuilds_callable_headers() -> None:
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(status=200, json_body={"ok": True}),
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", "OLD-EAUTH", "OLD-COOKIE")

    async def _reauth() -> bool:
        client.update_credentials(
            cookie="enlighten_manager_token_production=NEW-BEAR; XSRF-TOKEN=new-xsrf",
            eauth="NEW-EAUTH",
        )
        return True

    client.set_reauth_callback(_reauth)
    payload = await client._json(
        "GET", "https://example.test", headers=client._hems_headers
    )

    assert payload == {"ok": True}
    first_headers = session.calls[0][2]["headers"]
    second_headers = session.calls[1][2]["headers"]
    assert first_headers["Authorization"] == "Bearer OLD-EAUTH"
    assert first_headers["e-auth-token"] == "OLD-EAUTH"
    assert first_headers["Cookie"] == "OLD-COOKIE"
    assert second_headers["Authorization"] == "Bearer NEW-BEAR"
    assert second_headers["e-auth-token"] == "NEW-EAUTH"
    assert second_headers["Cookie"] == (
        "enlighten_manager_token_production=NEW-BEAR; XSRF-TOKEN=new-xsrf"
    )
    assert second_headers["X-CSRF-Token"] == "new-xsrf"


@pytest.mark.asyncio
async def test_json_hems_401_does_not_attempt_password_refresh() -> None:
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", None, None)
    reauth = AsyncMock(return_value=True)
    client.set_reauth_callback(reauth)

    with pytest.raises(api.Unauthorized):
        await client._json(
            "GET",
            "https://hems-integration.enphaseenergy.com/api/v1/hems/SITE/hems-devices",
        )

    reauth.assert_not_awaited()
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_json_hems_reauth_disabled_does_not_affect_other_endpoints() -> None:
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(status=200, json_body={"other": True}),
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", None, None)
    reauth = AsyncMock(return_value=True)
    client.set_reauth_callback(reauth)

    with pytest.raises(api.Unauthorized):
        await client._json(
            "GET",
            "https://hems-integration.enphaseenergy.com/api/v1/hems/SITE/hems-devices",
        )
    payload = await client._json("GET", "https://example.test/other")

    assert payload == {"other": True}
    assert reauth.await_count == 1


@pytest.mark.asyncio
async def test_evse_fw_details_returns_list_payload() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                json_body=[
                    {
                        "serialNumber": TEST_EVSE_SERIAL,
                        "currentFwVersion": "25.37.1.13",
                        "targetFwVersion": "25.37.1.14",
                    },
                    "bad",
                ],
            )
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", "EAUTH", "COOKIE")

    payload = await client.evse_fw_details()
    assert payload == [
        {
            "serialNumber": TEST_EVSE_SERIAL,
            "currentFwVersion": "25.37.1.13",
            "targetFwVersion": "25.37.1.14",
        }
    ]
    assert session.calls[0][0] == "GET"
    assert session.calls[0][1].endswith("/service/evse_management/fwDetails/SITE")


@pytest.mark.asyncio
async def test_evse_fw_details_normalizes_null_payload_to_empty_list() -> None:
    session = _FakeSession([_FakeResponse(status=200, json_body=None)])
    client = api.EnphaseEVClient(session, "SITE", "EAUTH", "COOKIE")

    assert await client.evse_fw_details() == []


@pytest.mark.asyncio
async def test_evse_fw_details_returns_none_on_unauthorized() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=api.Unauthorized())

    assert await client.evse_fw_details() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_evse_fw_details_returns_none_for_optional_http_errors(
    status: int,
) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(status, "missing"))

    assert await client.evse_fw_details() is None


@pytest.mark.asyncio
async def test_evse_fw_details_rejects_non_list_payload() -> None:
    session = _FakeSession(
        [_FakeResponse(status=200, json_body={"serialNumber": "bad"})]
    )
    client = api.EnphaseEVClient(session, "SITE", "EAUTH", "COOKIE")

    with pytest.raises(api.InvalidPayloadError, match="payload must be a list") as err:
        await client.evse_fw_details()

    payload_text = json.dumps(
        {"serialNumber": "bad"},
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
        sort_keys=True,
    )
    assert err.value.failure_kind == "shape"
    assert err.value.endpoint == "/service/evse_management/fwDetails/SITE"
    assert err.value.body_length == len(payload_text)
    assert err.value.body_sha256 == hashlib.sha256(payload_text.encode()).hexdigest()
    assert err.value.body_preview_redacted == '{"serialNumber":"b...d"}'


@pytest.mark.asyncio
async def test_evse_feature_flags_uses_endpoint_and_optional_country() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": {"evse_charging_mode": True}})

    result = await client.evse_feature_flags(country="DE")

    assert result == {"data": {"evse_charging_mode": True}}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/service/evse_management/api/v1/config/feature-flags?site_id=SITE&country=DE",
        headers=client._today_headers(),
    )


@pytest.mark.asyncio
async def test_evse_feature_flags_returns_none_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    assert await client.evse_feature_flags() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [api.Unauthorized(), _make_cre(403), _make_cre(404)])
async def test_evse_feature_flags_optional_errors_return_none(error) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    assert await client.evse_feature_flags() is None


@pytest.mark.asyncio
async def test_evse_feature_flags_reraises_unexpected_http_error() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.evse_feature_flags()


@pytest.mark.asyncio
async def test_json_reauth_failure_falls_back() -> None:
    session = _FakeSession([_FakeResponse(status=401, json_body={})])
    client = api.EnphaseEVClient(session, "SITE", None, None)

    async def _reauth() -> bool:
        return False

    client.set_reauth_callback(_reauth)
    with pytest.raises(api.Unauthorized):
        await client._json("GET", "https://example.test")
    assert client.last_unauthorized_request == "GET /"


@pytest.mark.asyncio
async def test_json_unauthorized_without_reauth_logs_request_label(caplog) -> None:
    session = _FakeSession([_FakeResponse(status=401, json_body={})])
    client = api.EnphaseEVClient(session, "SITE", None, None)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(api.Unauthorized):
            await client._json("POST", "https://example.test/service/status")

    assert (
        "Received 401 for POST /service/status with no stored-credential refresh available"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_json_returns_empty_on_no_content() -> None:
    session = _FakeSession([_FakeResponse(status=204, json_body=None)])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    payload = await client._json("POST", "https://example.test")
    assert payload == {}


@pytest.mark.asyncio
async def test_json_raises_response_error_with_structured_body_summary() -> None:
    long_body = "secret cloud failure for site SITE" * 20
    session = _FakeSession(
        [_FakeResponse(status=400, json_body={}, text_body=long_body)]
    )
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client._json("GET", "https://example.test")
    assert "status=400" in err.value.message
    assert f"body_length={len(long_body)}" in err.value.message
    assert "body_sha256=" not in err.value.message
    assert "secret cloud failure" not in err.value.message
    assert "SITE" not in err.value.message
    assert not hasattr(err.value, "enphase_response_body")


@pytest.mark.asyncio
async def test_json_attaches_scheduler_error_context_without_raw_body() -> None:
    body = json.dumps(
        {
            "error": {
                "displayMessage": "No Schedules enabled for Scheduled Charging",
                "errorMessageCode": "iqevc_sch_10031",
            }
        }
    )
    session = _FakeSession([_FakeResponse(status=400, json_body={}, text_body=body)])
    client = api.EnphaseEVClient(session, "SITE", None, None)

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client._json(
            "PUT",
            "https://example.test/service/evse_scheduler/api/v1/iqevc/"
            "charging-mode/SITE/SN/preference",
        )

    assert err.value.enphase_scheduler_error == {
        "code": "iqevc_sch_10031",
        "display": "No Schedules enabled for Scheduled Charging",
    }
    assert not hasattr(err.value, "enphase_response_body")


@pytest.mark.asyncio
async def test_json_logs_batteryconfig_write_failure_details(caplog) -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=403,
                json_body={},
                text_body="forbidden for site SITE and user 123456",
            )
        ]
    )
    client = api.EnphaseEVClient(
        session,
        "SITE",
        "EAUTH",
        "session=1; bp-xsrf-token=secret-xsrf; other=1",
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(aiohttp.ClientResponseError):
            await client._json(
                "PUT",
                "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/"
                "batterySettings/SITE",
                headers={
                    "Authorization": "Bearer secret-auth",
                    "e-auth-token": "secret-eauth",
                    "Username": "123456",
                    "X-CSRF-Token": "secret-xsrf",
                    "X-XSRF-Token": "secret-xsrf",
                    "Cookie": ("session=1; bp-xsrf-token=secret-xsrf; other=1"),
                },
                params={"userId": "123456", "source": "enho"},
                json={"veryLowSoc": 15},
            )

    assert (
        "BatteryConfig write failed for PUT "
        "/service/batteryConfig/api/v1/batterySettings/[site]: status=403"
        in caplog.text
    )
    assert "payload={'scheduleType': None, 'json_keys': ['veryLowSoc']}" in caplog.text
    assert "cookie_names=['bp-xsrf-token', 'other', 'session']" in caplog.text
    assert "'has_authorization': True" in caplog.text
    assert "'has_e_auth_token': True" in caplog.text
    assert "'has_requestid': False" in caplog.text
    assert "'has_username': True" in caplog.text
    assert "'has_x_csrf_token': True" in caplog.text
    assert "'has_x_xsrf_token': True" in caplog.text
    assert "'auth_mode': 'dual_mismatch'" in caplog.text
    assert "'auth_source': 'mixed'" in caplog.text
    assert "'source': 'enho'" in caplog.text
    assert "'userId': '[redacted]'" in caplog.text
    assert "headers={'Accept': 'application/json, text/plain, */*'" in caplog.text
    assert "'Cookie': '[redacted]'" in caplog.text
    assert "'Authorization': '[redacted]'" in caplog.text
    assert "'e-auth-token': '[redacted]'" in caplog.text
    assert "'X-CSRF-Token': '[redacted]'" in caplog.text
    assert "'X-XSRF-Token': '[redacted]'" in caplog.text
    assert "'Username': '[redacted]'" in caplog.text
    assert "/pv/systems/SITE/summary" not in caplog.text
    assert "/pv/systems/[site]/summary" in caplog.text
    assert "secret-auth" not in caplog.text
    assert "secret-eauth" not in caplog.text
    assert "secret-xsrf" not in caplog.text


@pytest.mark.asyncio
async def test_json_logs_batteryconfig_write_failure_without_params(caplog) -> None:
    session = _FakeSession([_FakeResponse(status=403, json_body={}, text_body="deny")])
    client = _make_client(session)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(aiohttp.ClientResponseError):
            await client._json(
                "POST",
                "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/"
                "battery/sites/SITE/schedules",
            )

    assert "BatteryConfig write failed for POST" in caplog.text
    assert "params=None" in caplog.text


@pytest.mark.asyncio
async def test_json_logs_battery_schedule_write_failure_includes_schedule_type(
    caplog,
) -> None:
    session = _FakeSession([_FakeResponse(status=403, json_body={}, text_body="deny")])
    client = _make_client(session)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(aiohttp.ClientResponseError):
            await client._json(
                "PUT",
                "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/"
                "battery/sites/SITE/schedules/sched-1",
                json={
                    "scheduleType": "DTG",
                    "startTime": "07:15",
                    "endTime": "09:45",
                    "days": [2, 6],
                },
            )

    assert "BatteryConfig write failed for PUT" in caplog.text
    assert (
        "payload={'scheduleType': 'DTG', 'json_keys': ['days', 'endTime', 'scheduleType', 'startTime']}"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_json_logs_batteryconfig_write_failure_with_non_dict_params(
    caplog,
) -> None:
    session = _FakeSession(
        [_FakeResponse(status=403, json_body={}, text_body="forbidden site SITE")]
    )
    client = _make_client(session)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(aiohttp.ClientResponseError):
            await client._json(
                "PUT",
                "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/"
                "profile/SITE",
                params="userId=123456&source=enho",
            )

    assert "BatteryConfig write failed for PUT" in caplog.text
    assert "params=userId=[redacted]" in caplog.text


@pytest.mark.asyncio
async def test_json_logs_evse_control_write_failure_details(caplog) -> None:
    session = _FakeSession([_FakeResponse(status=403, json_body={}, text_body="deny")])
    client = _make_client(session)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(aiohttp.ClientResponseError):
            await client._json(
                "PUT",
                "https://enlighten.enphaseenergy.com/service/evse_controller/api/v1/"
                "SITE/ev_chargers/EV1/ev_charger_config",
                headers={
                    "Authorization": "Bearer secret-auth",
                    "Cookie": "session=1; other=1",
                },
                json=[{"key": "sessionAuthentication", "value": "enabled"}],
            )

    assert "EVSE control write failed for PUT" in caplog.text
    assert (
        "payload={'json_item_count': 1, 'json_keys': ['key', 'value']}" in caplog.text
    )
    assert "'has_authorization': True" in caplog.text
    assert "cookie_names=['other', 'session']" in caplog.text
    assert "secret-auth" not in caplog.text


@pytest.mark.asyncio
async def test_json_logs_grid_control_toggle_failure_details(caplog) -> None:
    session = _FakeSession([_FakeResponse(status=403, json_body={}, text_body="deny")])
    client = _make_client(session)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(aiohttp.ClientResponseError):
            await client._json(
                "POST",
                "https://enlighten.enphaseenergy.com/pv/settings/grid_state.json",
                headers={"Authorization": "Bearer secret-auth"},
                data={"envoy_serial_number": "ENV123", "state": 1},
            )

    assert (
        "Grid control toggle failed for POST /pv/settings/grid_state.json"
        in caplog.text
    )
    assert "payload={'data_keys': ['envoy_serial_number', 'state']}" in caplog.text
    assert "'has_authorization': True" in caplog.text
    assert "secret-auth" not in caplog.text


@pytest.mark.asyncio
async def test_json_handles_text_failure() -> None:
    session = _FakeSession(
        [_FakeResponse(status=422, json_body={}, text_body=RuntimeError("boom"))]
    )
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client._json("GET", "https://example.test")
    assert err.value.message == (
        "HTTP error from Enphase endpoint (status=422, reason=reason)"
    )


@pytest.mark.asyncio
async def test_json_raises_invalid_payload_with_sanitized_summary() -> None:
    response = _FakeResponse(
        status=200,
        json_body=ValueError("decode failed"),
        text_body="<html>gateway failure</html>",
    )
    response.headers = {"Content-Type": "text/html"}
    session = _FakeSession([response])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(api.InvalidPayloadError) as err:
        await client._json("GET", "https://example.test")
    assert err.value.status == 200
    assert err.value.content_type == "text/html"
    assert err.value.failure_kind == "json_decode"
    assert err.value.decode_error == "ValueError"
    assert err.value.body_length == len("<html>gateway failure</html>")
    assert (
        err.value.body_sha256
        == hashlib.sha256(b"<html>gateway failure</html>").hexdigest()
    )
    assert err.value.body_preview_redacted == "<html>gateway failure</html>"
    assert "Invalid JSON response" in err.value.summary
    assert "content_type=text/html" in err.value.summary
    assert "endpoint=/" in err.value.summary
    assert "decode_error=ValueError" in err.value.summary
    assert "gateway failure" not in err.value.summary


@pytest.mark.asyncio
async def test_json_invalid_payload_uses_default_summary_when_headers_are_unavailable() -> (
    None
):
    class _BadHeaders:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("broken headers")

    response = _FakeResponse(
        status=200,
        json_body=ValueError("decode failed"),
        text_body=RuntimeError("text unavailable"),
    )
    response.headers = _BadHeaders()
    session = _FakeSession([response])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(api.InvalidPayloadError) as err:
        await client._json("GET", "https://example.test")
    assert err.value.status == 200
    assert err.value.content_type is None
    assert "Invalid JSON response" in err.value.summary
    assert "decode_error=ValueError" in err.value.summary


@pytest.mark.asyncio
async def test_json_invalid_payload_sanitizes_long_summary() -> None:
    response = _FakeResponse(
        status=200,
        json_body=ValueError("decode failed"),
        text_body="",
    )
    response.headers = {"Content-Type": "x" * 600}
    session = _FakeSession([response])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(api.InvalidPayloadError) as err:
        await client._json("GET", "https://example.test")
    assert len(err.value.summary) == 257
    assert err.value.summary.endswith("…")


@pytest.mark.asyncio
async def test_json_invalid_payload_logs_redacted_evse_status_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = _FakeResponse(
        status=200,
        json_body=json.JSONDecodeError("Expecting value", "", 0),
        text_body=(
            '{"site":"SITE","email":"user@example.com","serial":"SERIAL-12345678"}'
        ),
    )
    response.headers = {"Content-Type": "application/json; charset=utf-8"}
    session = _FakeSession([response])
    client = api.EnphaseEVClient(session, "SITE", None, None)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(api.InvalidPayloadError) as err:
            await client._json(
                "GET",
                (
                    "https://example.test/service/evse_controller/"
                    "SITE/ev_chargers/status"
                ),
            )

    assert err.value.failure_kind == "json_decode"
    assert err.value.decode_error == "JSONDecodeError"
    assert err.value.body_length == len(
        '{"site":"SITE","email":"user@example.com","serial":"SERIAL-12345678"}'
    )
    assert (
        err.value.body_sha256
        == hashlib.sha256(
            b'{"site":"SITE","email":"user@example.com","serial":"SERIAL-12345678"}'
        ).hexdigest()
    )
    assert (
        err.value.body_preview_redacted
        == '{"site":"[site]","email":"[redacted]","serial":"SERI...5678"}'
    )
    assert (
        "Invalid payload for site [site] endpoint /service/evse_controller/[site]/ev_chargers/status"
        in caplog.text
    )
    assert "application/json; charset=utf-8" in caplog.text
    assert "failure_kind=json_decode" in caplog.text
    assert '"site":"[site]"' in caplog.text
    assert '"email":"[redacted]"' in caplog.text
    assert '"serial":"SERI...5678"' in caplog.text
    assert "user@example.com" not in caplog.text
    assert "SERIAL-12345678" not in caplog.text


@pytest.mark.asyncio
async def test_json_invalid_payload_logs_once_and_recovery_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response1 = _FakeResponse(
        status=200,
        json_body=json.JSONDecodeError("Expecting value", "", 0),
        text_body='{"site":"SITE"}',
    )
    response1.headers = {"Content-Type": "application/json"}
    response2 = _FakeResponse(
        status=200,
        json_body=json.JSONDecodeError("Expecting value", "", 0),
        text_body='{"site":"SITE"}',
    )
    response2.headers = {"Content-Type": "application/json"}
    response3 = _FakeResponse(status=200, json_body={"ok": True})
    session = _FakeSession([response1, response2, response3])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    url = "https://example.test/service/evse_controller/SITE/ev_chargers/status"

    with caplog.at_level(logging.INFO):
        with pytest.raises(api.InvalidPayloadError):
            await client._json("GET", url)
        with pytest.raises(api.InvalidPayloadError):
            await client._json("GET", url)
        assert await client._json("GET", url) == {"ok": True}

    warning_messages = [
        rec.message for rec in caplog.records if rec.levelno == logging.WARNING
    ]
    info_messages = [
        rec.message for rec in caplog.records if rec.levelno == logging.INFO
    ]
    assert len(warning_messages) == 1
    assert (
        "Invalid payload for site [site] endpoint /service/evse_controller/[site]/ev_chargers/status"
        in warning_messages[0]
    )
    assert info_messages == [
        "Payload recovered for site [site] endpoint /service/evse_controller/[site]/ev_chargers/status"
    ]


@pytest.mark.asyncio
async def test_json_invalid_payload_logs_once_until_recovery_even_when_signature_changes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response1 = _FakeResponse(
        status=200,
        json_body=json.JSONDecodeError("Expecting value", "", 0),
        text_body='{"site":"SITE","serial":"SERIAL-1111"}',
    )
    response1.headers = {"Content-Type": "application/json"}
    response2 = _FakeResponse(
        status=200,
        json_body=json.JSONDecodeError("Expecting value", "", 0),
        text_body='{"site":"SITE","serial":"SERIAL-2222"}',
    )
    response2.headers = {"Content-Type": "application/json"}
    session = _FakeSession([response1, response2])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    url = "https://example.test/service/evse_controller/SITE/ev_chargers/status"

    with caplog.at_level(logging.WARNING):
        with pytest.raises(api.InvalidPayloadError):
            await client._json("GET", url)
        with pytest.raises(api.InvalidPayloadError):
            await client._json("GET", url)

    warning_messages = [
        rec.message for rec in caplog.records if rec.levelno == logging.WARNING
    ]
    assert len(warning_messages) == 1


@pytest.mark.asyncio
async def test_json_invalid_payload_handles_unparseable_url() -> None:
    response = _FakeResponse(
        status=200,
        json_body=ValueError("decode failed"),
        text_body="",
    )
    response.headers = {"Content-Type": "application/json"}
    session = _FakeSession([response])
    client = api.EnphaseEVClient(session, "SITE", None, None)
    with pytest.raises(api.InvalidPayloadError) as err:
        await client._json("GET", object())
    assert "endpoint=" not in err.value.summary
    assert "decode_error=ValueError" in err.value.summary


@pytest.mark.asyncio
async def test_devices_inventory_uses_devices_json_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"result": []})

    result = await client.devices_inventory()

    assert result == {"result": []}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/devices.json",
        headers=client._history_headers(),
    )


@pytest.mark.asyncio
async def test_devices_inventory_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["not", "a", "dict"])

    result = await client.devices_inventory()

    assert result == {}


@pytest.mark.asyncio
async def test_phase_map_multiple_envoy_uses_app_api_endpoint() -> None:
    client = _make_client()
    payload = {"GW-2": {"isDefaultGateway": True, "totalPhase": 3}}
    client._json = AsyncMock(return_value=payload)

    result = await client.phase_map_multiple_envoy()

    assert result == payload
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/phase_map_multiple_envoy",
        headers=client._history_headers(),
    )


@pytest.mark.asyncio
async def test_phase_map_multiple_envoy_rejects_non_mapping_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=[])

    assert await client.phase_map_multiple_envoy() is None


@pytest.mark.asyncio
async def test_phase_map_multiple_envoy_wraps_optional_html_payload() -> None:
    client = _make_client()
    error = api.InvalidPayloadError(
        "HTML response",
        status=200,
        content_type="text/html",
        endpoint="/app-api/SITE/phase_map_multiple_envoy",
        failure_kind="html",
        body_preview_redacted="<html></html>",
    )
    client._json = AsyncMock(side_effect=error)

    with pytest.raises(api.OptionalEndpointUnavailable):
        await client.phase_map_multiple_envoy()


@pytest.mark.asyncio
async def test_phase_map_multiple_envoy_reraises_json_payload_error() -> None:
    client = _make_client()
    error = api.InvalidPayloadError(
        "JSON response failed",
        status=500,
        content_type="application/json",
        endpoint="/app-api/SITE/phase_map_multiple_envoy",
        failure_kind="decode",
    )
    client._json = AsyncMock(side_effect=error)

    with pytest.raises(api.InvalidPayloadError):
        await client.phase_map_multiple_envoy()


@pytest.mark.asyncio
async def test_devices_tree_uses_system_dashboard_endpoint_and_headers() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"devices": []})

    result = await client.devices_tree()

    assert result == {"devices": []}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/SITE/devices-tree",
        headers=client._system_dashboard_headers(),
    )


@pytest.mark.asyncio
async def test_devices_tree_returns_none_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    assert await client.devices_tree() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [api.Unauthorized(), _make_cre(403), _make_cre(404)])
async def test_devices_tree_optional_errors_return_none(error) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[error, error])

    assert await client.devices_tree() is None


@pytest.mark.asyncio
async def test_devices_tree_falls_back_to_legacy_route() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(404), {"devices": []}])

    result = await client.devices_tree()

    assert result == {"devices": []}
    assert client._json.await_args_list[0].args[1] == (
        f"{api.BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/SITE/devices-tree"
    )
    assert client._json.await_args_list[1].args[1] == (
        f"{api.BASE_URL}/pv/systems/SITE/system_dashboard/devices-tree"
    )


@pytest.mark.asyncio
async def test_devices_tree_non_json_payload_returns_none(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=text/html, endpoint=/service/system_dashboard/api_internal/dashboard/sites/SITE/devices-tree, decode_error=ContentTypeError)",
        status=200,
        content_type="text/html",
        endpoint="/service/system_dashboard/api_internal/dashboard/sites/SITE/devices-tree",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=[err, err]))

    assert await client.devices_tree() is None


@pytest.mark.asyncio
async def test_devices_tree_json_invalid_payload_reraises(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=application/json, endpoint=/service/system_dashboard/api_internal/dashboard/sites/SITE/devices-tree, decode_error=ValueError)",
        status=200,
        content_type="application/json",
        endpoint="/service/system_dashboard/api_internal/dashboard/sites/SITE/devices-tree",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(api.InvalidPayloadError):
        await client.devices_tree()


@pytest.mark.asyncio
async def test_devices_tree_reraises_unexpected_http_error() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.devices_tree()


@pytest.mark.asyncio
async def test_devices_details_uses_system_dashboard_endpoint_and_headers() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"details": []})

    result = await client.devices_details("meter")

    assert result == {"details": []}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/SITE/devices_details?type=meters",
        headers=client._system_dashboard_headers(),
    )


@pytest.mark.asyncio
async def test_devices_details_returns_none_when_type_invalid_or_payload_bad() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    assert await client.devices_details("") is None
    assert await client.devices_details("unsupported") is None
    assert await client.devices_details("encharge") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [api.Unauthorized(), _make_cre(401), _make_cre(404)])
async def test_devices_details_optional_errors_return_none(error) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[error, error])

    assert await client.devices_details("envoy") is None


@pytest.mark.asyncio
async def test_devices_details_falls_back_to_legacy_route_and_query_mapping() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(404), {"details": []}])

    result = await client.devices_details("microinverter")

    assert result == {"details": []}
    assert client._json.await_args_list[0].args[1] == (
        f"{api.BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/SITE/devices_details?type=inverters"
    )
    assert client._json.await_args_list[1].args[1] == (
        f"{api.BASE_URL}/pv/systems/SITE/system_dashboard/devices_details?type=inverters"
    )


@pytest.mark.asyncio
async def test_devices_details_non_json_payload_returns_none(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=text/html, endpoint=/service/system_dashboard/api_internal/dashboard/sites/SITE/devices_details, decode_error=ContentTypeError)",
        status=200,
        content_type="text/html",
        endpoint="/service/system_dashboard/api_internal/dashboard/sites/SITE/devices_details",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=[err, err]))

    assert await client.devices_details("envoy") is None


@pytest.mark.asyncio
async def test_devices_details_json_invalid_payload_reraises(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=application/json, endpoint=/service/system_dashboard/api_internal/dashboard/sites/SITE/devices_details, decode_error=ValueError)",
        status=200,
        content_type="application/json",
        endpoint="/service/system_dashboard/api_internal/dashboard/sites/SITE/devices_details",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(api.InvalidPayloadError):
        await client.devices_details("envoy")


@pytest.mark.asyncio
async def test_devices_details_reraises_unexpected_http_error() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.devices_details("envoy")


@pytest.mark.asyncio
async def test_system_dashboard_microinverter_parameter_endpoints() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            {"parameters": [{"id": "power"}]},
            {"data": [{"serial_number": "INV-A"}]},
            {"columns": [{"attribute_name": "value_1"}]},
            {"intervals": []},
        ]
    )

    assert await client.system_dashboard_master_data() == {
        "parameters": [{"id": "power"}]
    }
    assert await client.system_dashboard_envoy_inverters(" GW-A ") == {
        "data": [{"serial_number": "INV-A"}]
    }
    assert await client.system_dashboard_data_columns("GW-A") == {
        "columns": [{"attribute_name": "value_1"}]
    }
    assert await client.system_dashboard_parameter_view(
        ["INV-A", "INV-B", "INV-A"], "power"
    ) == {"intervals": []}

    urls = [call.args[1] for call in client._json.await_args_list]
    assert urls[0].endswith("/cs/sites/SITE/data/master-data")
    assert URL(urls[1]).query["serial_number"] == "GW-A"
    assert URL(urls[2]).query == {
        "serial_num": "GW-A",
        "type": "device_level",
    }
    parameter_query = URL(urls[3]).query
    assert parameter_query["serial_numbers"] == "INV-A,INV-B"
    assert parameter_query["parameter_id"] == "power"
    assert parameter_query["sort_by_date"] == "desc"
    assert all(
        call.kwargs["headers"] == client._system_dashboard_headers()
        for call in client._json.await_args_list
    )


@pytest.mark.asyncio
async def test_system_dashboard_microinverter_parameter_endpoint_guards() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    assert await client.system_dashboard_envoy_inverters("") is None
    assert await client.system_dashboard_data_columns("") is None
    assert await client.system_dashboard_parameter_view([], "power") is None
    assert await client.system_dashboard_parameter_view(["INV-A"], "") is None
    assert await client.system_dashboard_master_data() is None

    client._json = AsyncMock(side_effect=_make_cre(404))
    assert await client.system_dashboard_master_data() is None
    assert await client.system_dashboard_envoy_inverters("GW-A") is None
    assert await client.system_dashboard_data_columns("GW-A") is None
    assert await client.system_dashboard_parameter_view(["INV-A"], "power") is None

    client._json = AsyncMock(side_effect=_make_cre(500))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.system_dashboard_parameter_view(["INV-A"], "power")


@pytest.mark.asyncio
async def test_grid_control_check_uses_grid_control_check_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"disableGridControl": False})

    result = await client.grid_control_check()

    assert result == {"disableGridControl": False}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/grid_control_check.json",
        headers={
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{api.BASE_URL}/web/SITE/history/graph/years",
            "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
            "Cookie": "COOKIE",
            "e-auth-token": "EAUTH",
        },
    )


@pytest.mark.asyncio
async def test_off_grid_due_to_grid_outage_uses_status_context_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"is_grid_outage": True})

    result = await client.off_grid_due_to_grid_outage()

    assert result == {"is_grid_outage": True}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/off_grid_due_to_grid_outage",
        headers={
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{api.BASE_URL}/web/SITE/history/graph/years",
            "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
            "Cookie": "COOKIE",
            "e-auth-token": "EAUTH",
        },
    )


@pytest.mark.asyncio
async def test_off_grid_due_to_grid_outage_returns_empty_when_payload_not_dict() -> (
    None
):
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.off_grid_due_to_grid_outage()

    assert result == {}


@pytest.mark.asyncio
async def test_request_grid_toggle_otp_uses_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"success": "email sent successfully"})

    result = await client.request_grid_toggle_otp()

    assert result == {"success": "email sent successfully"}
    expected_headers = client._history_headers()
    expected_headers.update(client._control_headers())
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/grid_toggle_otp.json",
        headers=expected_headers,
    )


@pytest.mark.asyncio
async def test_request_grid_toggle_otp_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.request_grid_toggle_otp()

    assert result == {}


@pytest.mark.asyncio
async def test_validate_grid_toggle_otp_success() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"valid": True})

    result = await client.validate_grid_toggle_otp("1234")

    assert result is True
    expected_headers = client._history_form_headers()
    expected_headers.update(client._control_headers())
    client._json.assert_awaited_once_with(
        "POST",
        f"{api.BASE_URL}/app-api/grid_toggle_otp.json",
        data={"otp": "1234", "site_id": "SITE"},
        headers=expected_headers,
    )


@pytest.mark.asyncio
async def test_validate_grid_toggle_otp_returns_false_on_non_dict_or_invalid() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[["bad"], {"valid": False}])

    assert await client.validate_grid_toggle_otp("1111") is False
    assert await client.validate_grid_toggle_otp("1111") is False


@pytest.mark.asyncio
async def test_set_grid_state_uses_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"request_id": "req"})

    result = await client.set_grid_state("122447007044", 1)

    assert result == {"request_id": "req"}
    expected_headers = client._history_form_headers()
    expected_headers.update(client._control_headers())
    client._json.assert_awaited_once_with(
        "POST",
        f"{api.BASE_URL}/pv/settings/grid_state.json",
        data={"envoy_serial_number": "122447007044", "state": 1},
        headers=expected_headers,
    )


@pytest.mark.asyncio
async def test_set_grid_state_returns_empty_on_non_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=None)

    result = await client.set_grid_state("122447007044", 2)

    assert result == {}


@pytest.mark.asyncio
async def test_log_grid_change_uses_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "Grid Change Logged"})

    result = await client.log_grid_change(
        "122447007044",
        "OPER_RELAY_CLOSED",
        "OPER_RELAY_OFFGRID_AC_GRID_PRESENT",
    )

    assert result == {"status": "Grid Change Logged"}
    expected_headers = client._history_form_headers()
    expected_headers.update(client._control_headers())
    client._json.assert_awaited_once_with(
        "POST",
        f"{api.BASE_URL}/pv/settings/log_grid_change.json",
        data={
            "envoy_serial_number": "122447007044",
            "old_state": "OPER_RELAY_CLOSED",
            "new_state": "OPER_RELAY_OFFGRID_AC_GRID_PRESENT",
        },
        headers=expected_headers,
    )


@pytest.mark.asyncio
async def test_log_grid_change_returns_empty_on_non_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value="bad")

    result = await client.log_grid_change("ENV", "OLD", "NEW")

    assert result == {}


@pytest.mark.asyncio
async def test_battery_backup_history_uses_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"histories": []})

    result = await client.battery_backup_history()

    assert result == {"histories": []}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/battery_backup_history.json",
        headers={
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{api.BASE_URL}/web/SITE/history/graph/years",
            "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
            "Cookie": "COOKIE",
            "e-auth-token": "EAUTH",
        },
    )


@pytest.mark.asyncio
async def test_grid_control_check_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.grid_control_check()

    assert result == {}


@pytest.mark.asyncio
async def test_battery_backup_history_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.battery_backup_history()

    assert result == {}


@pytest.mark.asyncio
async def test_battery_status_uses_battery_status_json_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"current_charge": "48%"})

    result = await client.battery_status()

    assert result == {"current_charge": "48%"}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/pv/settings/SITE/battery_status.json",
        headers={
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{api.BASE_URL}/web/SITE/history/graph/years",
            "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
            "Cookie": "COOKIE",
            "e-auth-token": "EAUTH",
        },
    )


@pytest.mark.asyncio
async def test_battery_status_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.battery_status()

    assert result == {}


@pytest.mark.asyncio
async def test_dry_contacts_settings_uses_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"contacts": []})

    result = await client.dry_contacts_settings()

    assert result == {"contacts": []}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/pv/settings/SITE/dry_contacts",
        headers=client._history_headers(),
    )


@pytest.mark.asyncio
async def test_dry_contacts_settings_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.dry_contacts_settings()

    assert result == {}


@pytest.mark.asyncio
async def test_inverters_inventory_uses_inverters_json_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"inverters": []})

    result = await client.inverters_inventory(limit=30, offset=0, search="")

    assert result == {"inverters": []}
    awaited = client._json.await_args
    assert awaited.args[0] == "GET"
    assert "/app-api/SITE/inverters.json" in awaited.args[1]
    assert "limit=30" in awaited.args[1]
    assert "offset=0" in awaited.args[1]
    assert awaited.kwargs["headers"] == {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{api.BASE_URL}/web/SITE/history/graph/years",
        "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
        "Cookie": "COOKIE",
        "e-auth-token": "EAUTH",
    }


@pytest.mark.asyncio
async def test_inverters_inventory_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["not", "dict"])

    result = await client.inverters_inventory()

    assert result == {}


@pytest.mark.asyncio
async def test_inverter_status_normalizes_keyed_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "1": {"serialNum": "A", "deviceId": 10},
            "2": "invalid",
            "": {"serialNum": "B"},
        }
    )

    result = await client.inverter_status()

    assert result == {"1": {"serialNum": "A", "deviceId": 10}}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/systems/SITE/inverter_status_x.json",
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{api.BASE_URL}/web/SITE/layout/graph/years",
            "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
            "Cookie": "COOKIE",
            "e-auth-token": "EAUTH",
        },
    )


@pytest.mark.asyncio
async def test_inverter_status_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.inverter_status()

    assert result == {}


@pytest.mark.asyncio
async def test_inverter_production_normalizes_values() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "production": {"a": 100, "b": "200.5", "c": "bad"},
            "start_date": "2022-01-01",
            "end_date": "2026-01-01",
        }
    )

    result = await client.inverter_production(
        start_date="2022-01-01", end_date="2026-01-01"
    )

    assert result["production"] == {"a": 100.0, "b": 200.5}
    assert result["start_date"] == "2022-01-01"
    assert result["end_date"] == "2026-01-01"
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/systems/SITE/inverter_data_x/energy.json?start_date=2022-01-01&end_date=2026-01-01",
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{api.BASE_URL}/web/SITE/layout/graph/years",
            "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
            "Cookie": "COOKIE",
            "e-auth-token": "EAUTH",
        },
    )


@pytest.mark.asyncio
async def test_inverter_production_returns_empty_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    result = await client.inverter_production(
        start_date="2022-01-01", end_date="2026-01-01"
    )

    assert result == {}


@pytest.mark.asyncio
async def test_inverter_production_skips_blank_keys() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "production": {"": 100, "good": 50},
            "start_date": "2022-01-01",
            "end_date": "2026-01-01",
        }
    )

    result = await client.inverter_production(
        start_date="2022-01-01", end_date="2026-01-01"
    )

    assert result["production"] == {"good": 50.0}


@pytest.mark.asyncio
async def test_status_normalizes_charger_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "chargers": [
                    {
                        "sn": "EV123",
                        "name": "Garage",
                        "displayName": "Garage EV",
                        "connected": True,
                        "pluggedIn": False,
                        "charging": True,
                        "faulted": False,
                        "commissioned": 1,
                        "offGrid": "ON_GRID",
                        "evManufacturerName": "Example Motors",
                        "smartEV": {"hasToken": True, "hasEVDetails": False},
                        "sch_d": {"status": 0, "info": [{"type": "CUSTOM"}]},
                        "connectors": [
                            {
                                "pluggedIn": True,
                                "connectorStatusType": "READY",
                                "dlbActive": False,
                            }
                        ],
                        "session_d": {
                            "e_c": 5,
                            "strt_chrg": "1000",
                            "auth_type": "APP",
                        },
                    },
                    {
                        "sn": "EV124",
                        "name": "Driveway",
                        "connected": True,
                        "pluggedIn": False,
                        "charging": False,
                        "faulted": False,
                        "connectors": [],
                        "session_d": {"e_c": 1, "strt_chrg": 2000},
                    },
                ]
            },
            "meta": {"serverTimeStamp": 123456},
        }
    )
    data = await client.status()
    assert data["ts"] == 123456
    assert data["evChargerData"][0]["pluggedIn"] is True
    assert data["evChargerData"][0]["connectors"][0]["dlbActive"] is False
    assert data["evChargerData"][0]["session_d"]["start_time"] == 1
    assert data["evChargerData"][0]["session_d"]["e_c"] == 5
    assert data["evChargerData"][0]["session_d"]["auth_type"] == "APP"
    assert data["evChargerData"][0]["displayName"] == "Garage EV"
    assert data["evChargerData"][0]["evManufacturerName"] == "Example Motors"
    assert data["evChargerData"][0]["smartEV"]["hasToken"] is True
    assert data["evChargerData"][0]["sch_d"]["status"] == 0
    assert data["evChargerData"][1]["connectors"] == []


@pytest.mark.asyncio
async def test_status_normalizes_start_time_variants() -> None:
    client = _make_client()
    huge = 1_700_000_000_123
    client._json = AsyncMock(
        return_value={
            "data": {
                "chargers": [
                    {
                        "sn": "EV1",
                        "name": "One",
                        "connectors": [{}],
                        "session_d": {"start_time": huge},
                    },
                    {
                        "sn": "EV2",
                        "name": "Two",
                        "connectors": [{}],
                        "session_d": {"start_time": str(huge)},
                    },
                ]
            }
        }
    )

    data = await client.status()
    assert data["evChargerData"][0]["session_d"]["start_time"] == huge
    assert data["evChargerData"][1]["session_d"]["start_time"] == str(huge)


@pytest.mark.asyncio
async def test_status_handles_bad_start_time_values() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "chargers": [
                    {
                        "sn": "EV1",
                        "name": "One",
                        "connectors": [{}],
                        "session_d": {"start_time": float("nan")},
                    },
                    {
                        "sn": "EV2",
                        "name": "Two",
                        "connectors": [{}],
                        "session_d": {"start_time": "9" * 5000},
                    },
                ]
            }
        }
    )

    data = await client.status()
    assert str(data["evChargerData"][0]["session_d"]["start_time"]) == "nan"
    assert data["evChargerData"][1]["session_d"]["start_time"] == "9" * 5000


@pytest.mark.asyncio
async def test_status_rejects_non_dict_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    with pytest.raises(api.InvalidPayloadError, match="must be an object") as err:
        await client.status()

    assert err.value.failure_kind == "shape"
    assert err.value.endpoint == "/service/evse_controller/SITE/ev_chargers/status"


@pytest.mark.asyncio
async def test_status_raises_optional_endpoint_unavailable_for_html_json_payload() -> (
    None
):
    client = _make_client()
    client._json = AsyncMock(
        side_effect=api.InvalidPayloadError(
            "Invalid JSON response (status=200, endpoint=/service/evse_controller/SITE/ev_chargers/status)",
            endpoint="/service/evse_controller/SITE/ev_chargers/status",
            status=200,
            content_type="application/json; charset=utf-8",
            failure_kind="json_decode",
            decode_error="JSONDecodeError",
            body_preview_redacted="<!DOCTYPE html><html lang='fr'>",
        )
    )

    with pytest.raises(api.OptionalEndpointUnavailable, match="Invalid JSON response"):
        await client.status()


@pytest.mark.asyncio
async def test_status_reraises_non_optional_invalid_payload() -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, endpoint=/service/evse_controller/SITE/ev_chargers/status)",
        endpoint="/service/evse_controller/SITE/ev_chargers/status",
        status=200,
        content_type="application/json; charset=utf-8",
        failure_kind="json_decode",
        decode_error="JSONDecodeError",
        body_preview_redacted='{"bad":true}',
    )
    client._json = AsyncMock(side_effect=err)

    with pytest.raises(api.InvalidPayloadError) as raised:
        await client.status()

    assert raised.value is err


@pytest.mark.asyncio
async def test_get_schedules_normalizes_payload() -> None:
    client = _make_client()
    payload = {
        "meta": {"serverTimeStamp": "ts"},
        "data": {"config": {"name": "config"}, "slots": [{"id": "slot-1"}]},
    }
    client._json = AsyncMock(return_value=payload)

    data = await client.get_schedules("SN123")

    assert data["meta"] == payload["meta"]
    assert data["config"] == {"name": "config"}
    assert data["slots"] == [{"id": "slot-1"}]

    method, url = client._json.call_args.args[:2]
    headers = client._json.call_args.kwargs["headers"]
    assert method == "GET"
    assert url.endswith("/charging-mode/SCHEDULED_CHARGING/SITE/SN123/schedules")
    assert "Authorization" in headers


@pytest.mark.asyncio
async def test_get_schedules_handles_bad_payloads() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value="bad")
    data = await client.get_schedules("SN123")
    assert data == {"meta": None, "config": None, "slots": []}

    client._json = AsyncMock(
        return_value={"meta": {"serverTimeStamp": "ts"}, "data": "bad"}
    )
    data = await client.get_schedules("SN123")
    assert data["meta"] == {"serverTimeStamp": "ts"}
    assert data["config"] is None
    assert data["slots"] == []


@pytest.mark.asyncio
async def test_patch_schedules_builds_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"ok": True})

    data = await client.patch_schedules(
        "SN123",
        server_timestamp="2025-01-01T00:00:00.000+00:00",
        slots=[{"id": "slot-1"}],
    )

    assert data == {"ok": True}

    method, url = client._json.call_args.args[:2]
    payload = client._json.call_args.kwargs["json"]
    assert method == "PATCH"
    assert url.endswith("/charging-mode/SCHEDULED_CHARGING/SITE/SN123/schedules")
    assert payload["meta"]["serverTimeStamp"] == "2025-01-01T00:00:00.000+00:00"
    assert payload["meta"]["rowCount"] == 1
    assert payload["data"] == [{"id": "slot-1"}]


@pytest.mark.asyncio
async def test_patch_schedule_states_builds_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"ok": True})

    data = await client.patch_schedule_states(
        "SN123",
        slot_states={"slot-1": True, "slot-2": False},
    )

    assert data == {"ok": True}

    method, url = client._json.call_args.args[:2]
    payload = client._json.call_args.kwargs["json"]
    assert method == "PATCH"
    assert url.endswith("/charging-mode/SCHEDULED_CHARGING/SITE/SN123/schedules")
    assert payload == {"slot-1": "ENABLED", "slot-2": "DISABLED"}


@pytest.mark.asyncio
async def test_patch_schedule_builds_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"ok": True})
    slot = {"id": "slot-1", "startTime": "11:00"}

    data = await client.patch_schedule("SN123", "slot-1", slot)

    assert data == {"ok": True}

    method, url = client._json.call_args.args[:2]
    payload = client._json.call_args.kwargs["json"]
    assert method == "PATCH"
    assert url.endswith("/charging-mode/SCHEDULED_CHARGING/SITE/SN123/schedule/slot-1")
    assert payload == slot


@pytest.mark.asyncio
async def test_create_schedule_builds_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"ok": True})
    slot = {"id": "slot-2", "startTime": "03:15"}

    data = await client.create_schedule("SN123", slot)

    assert data == {"ok": True}

    method, url = client._json.call_args.args[:2]
    payload = client._json.call_args.kwargs["json"]
    assert method == "POST"
    assert url.endswith("/charging-mode/SCHEDULED_CHARGING/SITE/SN123/schedule")
    assert payload == slot


@pytest.mark.asyncio
async def test_create_schedule_wraps_scheduler_unavailable(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client, "_json", AsyncMock(side_effect=_make_cre(503, "Service Unavailable"))
    )

    with pytest.raises(api.SchedulerUnavailable):
        await client.create_schedule("SN123", {"id": "slot-2", "startTime": "03:15"})


@pytest.mark.asyncio
async def test_delete_schedule_uses_single_slot_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"ok": True})

    data = await client.delete_schedule("SN123", "slot-2")

    assert data == {"ok": True}

    method, url = client._json.call_args.args[:2]
    assert method == "DELETE"
    assert url.endswith("/charging-mode/SCHEDULED_CHARGING/SITE/SN123/schedule/slot-2")


@pytest.mark.asyncio
async def test_delete_schedule_wraps_scheduler_unavailable(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client, "_json", AsyncMock(side_effect=_make_cre(503, "Service Unavailable"))
    )

    with pytest.raises(api.SchedulerUnavailable):
        await client.delete_schedule("SN123", "slot-2")


@pytest.mark.asyncio
async def test_status_does_not_call_legacy_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"evChargerData": []})
    data = await client.status()
    assert data == {"evChargerData": []}
    assert client._json.call_count == 1


@pytest.mark.asyncio
async def test_status_handles_mapping_failure() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": {"chargers": ["bad-entry"]}})
    data = await client.status()
    assert data == {"data": {"chargers": ["bad-entry"]}}


@pytest.mark.asyncio
async def test_start_charging_success_and_cache() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    out = await client.start_charging("SN", 32, connector_id=1)
    assert out == {"status": "ok"}
    assert client._start_variant_idx == 0


@pytest.mark.asyncio
async def test_start_charging_include_level_strict_requires_payload(
    monkeypatch,
) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_start_charging_candidates",
        lambda *args, **kwargs: [
            ("POST", "https://example/start", {"connectorId": 1}),
            ("POST", "https://example/start_alt", None),
        ],
    )
    with pytest.raises(aiohttp.ClientError):
        await client.start_charging(
            "SN", 32, include_level=True, strict_preference=True
        )


@pytest.mark.asyncio
async def test_start_charging_exclude_level_strict_requires_payload(
    monkeypatch,
) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_start_charging_candidates",
        lambda sn, level, connector_id: [
            ("POST", "https://example/start", {"chargingLevel": level}),
            ("POST", "https://example/start_alt", {"charging_level": level}),
        ],
    )
    with pytest.raises(aiohttp.ClientError):
        await client.start_charging(
            "SN", 32, include_level=False, strict_preference=True
        )


@pytest.mark.asyncio
async def test_start_charging_uses_cached_variant() -> None:
    client = _make_client()
    client._start_variant_idx = 5
    client._json = AsyncMock(return_value={"status": "ok"})
    await client.start_charging("SN", 32, connector_id=2)
    args, kwargs = client._json.await_args
    assert "ev_chargers" in args[1]
    assert kwargs.get("json") is None


@pytest.mark.asyncio
async def test_start_charging_not_ready_on_409() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(409), {"status": "ok"}])
    out = await client.start_charging("SN", 32, connector_id=1)
    assert out == {"status": "not_ready"}
    assert client._start_variant_idx == 0


@pytest.mark.asyncio
async def test_start_charging_raises_503_without_trying_fallback() -> None:
    client = _make_client()
    error = _make_cre(503, '{"errorMessageCode":"iqevc_ms-10007"}')
    client._json = AsyncMock(side_effect=error)

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client.start_charging("SN", 32, connector_id=1)

    assert err.value is error
    assert client._json.await_count == 1
    assert client._start_variant_idx is None


@pytest.mark.asyncio
async def test_start_charging_interprets_errors() -> None:
    body = {
        "error": {
            "errorMessageCode": "iqevc_ms-10012",
            "displayMessage": "Charger already in charging state",
        }
    }
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[_make_cre(400, message=json.dumps(body)), {"status": "ok"}]
    )
    out = await client.start_charging("SN", 32, connector_id=1)
    assert out == {"status": "already_charging"}


@pytest.mark.asyncio
async def test_start_charging_error_code_maps_to_already_charging() -> None:
    message = '{"error":{"errorMessageCode":"iqevc_ms-10012"}}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)])
    out = await client.start_charging("SN", 32)
    assert out == {"status": "already_charging"}


@pytest.mark.asyncio
async def test_start_charging_error_code_maps_to_not_ready() -> None:
    message = '{"error":{"errorMessageCode":"iqevc_ms-10008"}}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)])
    out = await client.start_charging("SN", 32)
    assert out == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_start_charging_display_message_fallback() -> None:
    message = '{"error":{"displayMessage":"\\u004eot plugged into vehicle"}}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)])
    out = await client.start_charging("SN", 32)
    assert out == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_start_charging_plain_not_plugged_message_maps_to_not_ready() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, "Vehicle not plugged in")])

    out = await client.start_charging("SN", 32)

    assert out == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_start_charging_display_message_already_charging() -> None:
    message = '{"error":{"message":"\\u0041lready in charging state"}}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)])
    out = await client.start_charging("SN", 32)
    assert out == {"status": "already_charging"}


@pytest.mark.asyncio
async def test_start_charging_parses_single_quoted_payload() -> None:
    payload = '{"errorMessageCode":"iqevc_ms-10008"}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message=f"'{payload}'")])
    out = await client.start_charging("SN", 32, connector_id=1)
    assert out == {"status": "not_ready"}


@pytest.mark.asyncio
async def test_start_charging_prefers_cached_level_variant(monkeypatch) -> None:
    client = _make_client()

    def _candidates(sn, level, connector_id):
        return [
            ("POST", "https://example/start0", {"connectorId": connector_id}),
            ("POST", "https://example/start1", {"chargingLevel": level}),
            ("POST", "https://example/start2", {"chargingLevel": level}),
        ]

    monkeypatch.setattr(client, "_start_charging_candidates", _candidates)
    client._start_variant_idx_with_level = 2
    client._json = AsyncMock(return_value={"status": "ok"})

    await client.start_charging("SN", 40, include_level=True)

    args, kwargs = client._json.await_args
    assert args[1].endswith("/start2")
    assert kwargs["json"] == {"chargingLevel": 40}
    assert client._start_variant_idx_with_level == 2


@pytest.mark.asyncio
async def test_start_charging_prefers_cached_no_level_variant(monkeypatch) -> None:
    client = _make_client()

    def _candidates(sn, level, connector_id):
        return [
            ("POST", "https://example/start0", {"chargingLevel": level}),
            ("POST", "https://example/start1", None),
            ("POST", "https://example/start2", {"connectorId": connector_id}),
        ]

    monkeypatch.setattr(client, "_start_charging_candidates", _candidates)
    client._start_variant_idx_no_level = 2
    client._json = AsyncMock(return_value={"status": "ok"})

    await client.start_charging("SN", 24, include_level=False)
    args, kwargs = client._json.await_args
    assert args[1].endswith("/start2")
    assert kwargs["json"] == {"connectorId": 1}
    assert client._start_variant_idx_no_level == 2


@pytest.mark.asyncio
async def test_start_charging_falls_back_to_general_cache(monkeypatch) -> None:
    client = _make_client()

    def _candidates(sn, level, connector_id):
        return [
            ("POST", "https://example/start0", {"connectorId": connector_id}),
        ]

    monkeypatch.setattr(client, "_start_charging_candidates", _candidates)
    client._json = AsyncMock(return_value={"status": "ok"})

    await client.start_charging("SN", 24, include_level=True)

    # Only general cache should update because payload lacked chargingLevel.
    assert client._start_variant_idx == 0
    assert client._start_variant_idx_with_level is None


@pytest.mark.asyncio
async def test_start_charging_includes_fallback_variants(monkeypatch) -> None:
    client = _make_client()

    def _no_level_candidates(sn, level, connector_id):
        return [
            ("POST", "https://example/start0", None),
        ]

    monkeypatch.setattr(client, "_start_charging_candidates", _no_level_candidates)
    client._json = AsyncMock(return_value={"status": "ok"})

    await client.start_charging("SN", 16, include_level=True, strict_preference=False)
    # Order was extended with fallback entry so the call succeeds.
    assert client._start_variant_idx == 0


async def test_start_charging_excludes_level_variants_when_requested(
    monkeypatch,
) -> None:
    client = _make_client()

    def _level_only_candidates(sn, level, connector_id):
        return [
            ("POST", "https://example/start0", {"chargingLevel": level}),
        ]

    monkeypatch.setattr(client, "_start_charging_candidates", _level_only_candidates)
    client._json = AsyncMock(return_value={"status": "ok"})

    await client.start_charging("SN", 30, include_level=False, strict_preference=False)
    assert client._start_variant_idx_no_level is None
    assert client._start_variant_idx == 0


@pytest.mark.asyncio
async def test_start_charging_raises_when_order_empty(monkeypatch) -> None:
    class TruthyEmpty(list):
        def __bool__(self):
            return True

    client = _make_client()

    def _candidates(sn, level, connector_id):
        return TruthyEmpty()

    monkeypatch.setattr(client, "_start_charging_candidates", _candidates)
    client._json = AsyncMock(return_value={"status": "ok"})

    with pytest.raises(aiohttp.ClientError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_falls_through_and_raises_generic(monkeypatch) -> None:
    class FakeList(list):
        def __bool__(self):
            return True

    client = _make_client()
    monkeypatch.setattr(
        client, "_start_charging_candidates", lambda *args, **kwargs: []
    )

    orig_list = list

    class PatchedList(FakeList):
        pass

    def _patched_list(*args, **kwargs):
        return PatchedList(orig_list(*args, **kwargs))

    monkeypatch.setattr("builtins.list", _patched_list)

    with pytest.raises(aiohttp.ClientError):
        await client.start_charging("SN", 16)


@pytest.mark.asyncio
async def test_start_charging_whitespace_error_message() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, "  ")] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_none_error_message() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, None)] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_non_dict_error_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, "[1, 2, 3]")] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_retries_all_and_raises() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, "bad")] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_no_candidates_raises_client_error(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client, "_start_charging_candidates", lambda *args, **kwargs: []
    )

    with pytest.raises(aiohttp.ClientError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_unknown_error_returns_none() -> None:
    message = '{"error":{"details":42}}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_start_charging_error_list_candidate() -> None:
    message = '{"error":["unexpected"]}'
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(400, message)] * 8)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.start_charging("SN", 32)


@pytest.mark.asyncio
async def test_stop_charging_success_and_cache() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    out = await client.stop_charging("SN")
    assert out == {"status": "ok"}
    assert client._stop_variant_idx == 0


@pytest.mark.asyncio
async def test_stop_charging_reorders_cached_variant() -> None:
    client = _make_client()
    client._stop_variant_idx = 2
    client._json = AsyncMock(return_value={"status": "ok"})
    await client.stop_charging("SN")
    args, _kwargs = client._json.await_args
    assert "/ev_charger/" in args[1]
    assert client._stop_variant_idx == 2


@pytest.mark.asyncio
async def test_stop_charging_handles_noop_status() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(404), {"status": "ok"}])
    out = await client.stop_charging("SN")
    assert out == {"status": "not_active"}
    assert client._stop_variant_idx is None


@pytest.mark.asyncio
async def test_control_candidates_keep_legacy_singular_fallbacks() -> None:
    client = _make_client()

    start_urls = [
        url for _method, url, _payload in client._start_charging_candidates("SN", 32, 1)
    ]
    stop_urls = [
        url for _method, url, _payload in client._stop_charging_candidates("SN")
    ]

    assert any("/ev_chargers/" in url for url in start_urls)
    assert any("/ev_charger/" in url for url in start_urls)
    assert any("/ev_chargers/" in url for url in stop_urls)
    assert any("/ev_charger/" in url for url in stop_urls)


@pytest.mark.asyncio
async def test_stop_charging_raises_503_without_trying_fallback() -> None:
    client = _make_client()
    error = _make_cre(503, '{"errorMessageCode":"iqevc_ms-10007"}')
    client._json = AsyncMock(side_effect=error)

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client.stop_charging("SN")

    assert err.value is error
    assert client._json.await_count == 1
    assert client._stop_variant_idx is None


@pytest.mark.asyncio
async def test_stop_charging_routing_404_is_not_noop(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_stop_charging_candidates",
        lambda _sn: [
            ("POST", "https://example.test/ev_charger/SN/stop_charging", None)
        ],
    )
    error = _make_cre(404, '{"type":"about:blank","detail":"No static resource"}')
    client._json = AsyncMock(side_effect=error)

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client.stop_charging("SN")

    assert err.value is error
    assert client._stop_variant_idx is None


@pytest.mark.asyncio
async def test_stop_charging_about_blank_404_with_spacing_is_not_noop(
    monkeypatch,
) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_stop_charging_candidates",
        lambda _sn: [("POST", "https://example.test/stop_charging", None)],
    )
    error = _make_cre(404, '{ "type": "about:blank", "detail": "No static resource" }')
    client._json = AsyncMock(side_effect=error)

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client.stop_charging("SN")

    assert err.value is error
    assert client._stop_variant_idx is None


@pytest.mark.asyncio
async def test_stop_charging_semantic_404_remains_noop() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            _make_cre(
                404,
                '{"type":"about:blank","title":"Not Found","detail":"charger is not active"}',
            )
        ]
    )

    out = await client.stop_charging("SN")

    assert out == {"status": "not_active"}
    assert client._stop_variant_idx is None


@pytest.mark.asyncio
async def test_stop_charging_routing_404_falls_back_to_legacy_singular() -> None:
    client = _make_client()
    error = _make_cre(404, '{"type":"about:blank","detail":"No static resource"}')
    client._json = AsyncMock(side_effect=[error, _make_cre(405), {"status": "ok"}])

    out = await client.stop_charging("SN")

    assert out == {"status": "ok"}
    assert client._json.await_count == 3
    args, _kwargs = client._json.await_args
    assert "/ev_charger/" in args[1]
    assert client._stop_variant_idx == 2


@pytest.mark.asyncio
async def test_stop_charging_routing_404_from_json_response_uses_derived_flag() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=404,
                json_body={},
                text_body='{ "type": "about:blank", "detail": "No static resource" }',
            ),
            _FakeResponse(status=405, json_body={}, text_body="method not allowed"),
            _FakeResponse(status=200, json_body={"status": "ok"}),
        ]
    )
    client = _make_client(session)

    out = await client.stop_charging("SN")

    assert out == {"status": "ok"}
    assert len(session.calls) == 3
    assert "/ev_charger/" in session.calls[-1][1]
    assert client._stop_variant_idx == 2


@pytest.mark.asyncio
async def test_start_charging_invalid_level_500_from_json_response_sets_flag() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=500,
                json_body={},
                text_body='{"error":{"displayMessage":"Invalid charge level","code":"500"}}',
            ),
        ]
    )
    client = _make_client(session)

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client.start_charging("SN", 32)

    assert getattr(err.value, "enphase_invalid_charge_level", False) is True
    assert len(session.calls) == 1


def test_stop_charging_routing_404_helper_handles_empty_message() -> None:
    assert not api.EnphaseEVClient._is_routing_not_found(None)
    assert not api.EnphaseEVClient._is_routing_not_found("")
    assert not api.EnphaseEVClient._is_invalid_charge_level_error(None)
    assert not api.EnphaseEVClient._is_invalid_charge_level_error("")


@pytest.mark.asyncio
async def test_stop_charging_raises_last_exception() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=[_make_cre(405)] * 3)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.stop_charging("SN")


@pytest.mark.asyncio
async def test_stop_charging_handles_payload_variant(monkeypatch) -> None:
    client = _make_client()
    payload = {"stop": True}
    monkeypatch.setattr(
        client,
        "_stop_charging_candidates",
        lambda _sn: [("POST", "https://example.test/stop", payload)],
    )
    client._json = AsyncMock(return_value={"status": "ok"})

    out = await client.stop_charging("SN")

    assert out == {"status": "ok"}
    assert client._json.await_args.kwargs["json"] == payload


@pytest.mark.asyncio
async def test_stop_charging_no_candidates_raises_client_error(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(client, "_stop_charging_candidates", lambda _sn: [])

    with pytest.raises(aiohttp.ClientError):
        await client.stop_charging("SN")


@pytest.mark.asyncio
async def test_trigger_and_stream_helpers_delegate_to_json() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    await client.trigger_message("SN", "MeterValues")
    await client.start_live_stream()
    await client.stop_live_stream()
    assert client._json.await_count == 3


@pytest.mark.asyncio
async def test_charge_mode_extracts_enabled_mode() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "modes": {
                    "manualCharging": {
                        "enabled": True,
                        "chargingMode": "MANUAL_CHARGING",
                    }
                }
            }
        }
    )
    mode = await client.charge_mode("SN")
    assert mode == "MANUAL_CHARGING"
    args, kwargs = client._json.await_args
    assert "Authorization" in kwargs["headers"]


@pytest.mark.asyncio
async def test_charge_mode_extracts_enabled_smart_mode() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "modes": {
                    "smartCharging": {
                        "enabled": True,
                        "chargingMode": "SMART_CHARGING",
                    },
                    "scheduledCharging": {
                        "enabled": False,
                        "chargingMode": "SCHEDULED_CHARGING",
                    },
                }
            }
        }
    )

    assert await client.charge_mode("SN") == "SMART_CHARGING"


@pytest.mark.asyncio
async def test_charge_mode_handles_unexpected_shape() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": {"modes": "invalid"}})
    assert await client.charge_mode("SN") is None


@pytest.mark.asyncio
async def test_charge_mode_returns_none_when_no_enabled() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "modes": {
                    "manualCharging": {
                        "enabled": False,
                        "chargingMode": "MANUAL_CHARGING",
                    }
                }
            }
        }
    )
    assert await client.charge_mode("SN") is None


@pytest.mark.asyncio
async def test_set_charge_mode_passes_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    out = await client.set_charge_mode(
        "SN", "GREEN_CHARGING", previous_mode="MANUAL_CHARGING"
    )
    assert out == {"status": "ok"}
    args, kwargs = client._json.await_args
    assert kwargs["json"] == {"mode": "GREEN_CHARGING"}


@pytest.mark.asyncio
async def test_set_charge_mode_skips_write_when_already_active() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "modes": {
                    "manualCharging": {
                        "enabled": True,
                        "chargingMode": "MANUAL_CHARGING",
                    }
                }
            }
        }
    )

    out = await client.set_charge_mode("SN", "MANUAL_CHARGING")

    assert out == {"status": "already_set", "mode": "MANUAL_CHARGING"}
    assert client._json.await_args.args[0] == "GET"
    assert client._json.await_count == 1


@pytest.mark.asyncio
async def test_set_charge_mode_accepts_verified_preference_after_400() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            {
                "data": {
                    "modes": {
                        "greenCharging": {
                            "enabled": True,
                            "chargingMode": "GREEN_CHARGING",
                        }
                    }
                }
            },
            _make_cre(400, "HTTP error from Enphase endpoint (status=400)"),
            {
                "data": {
                    "modes": {
                        "manualCharging": {
                            "enabled": True,
                            "chargingMode": "MANUAL_CHARGING",
                        }
                    }
                }
            },
        ]
    )

    out = await client.set_charge_mode("SN", "MANUAL_CHARGING")

    assert out == {
        "status": "accepted",
        "mode": "MANUAL_CHARGING",
        "verified_after_error": True,
    }
    assert client._json.await_args_list[0].args[0] == "GET"
    assert client._json.await_args_list[1].args[0] == "PUT"
    assert client._json.await_args_list[2].args[0] == "GET"


@pytest.mark.asyncio
async def test_set_charge_mode_retries_verified_preference_after_400(
    monkeypatch,
) -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            {
                "data": {
                    "modes": {
                        "greenCharging": {
                            "enabled": True,
                            "chargingMode": "GREEN_CHARGING",
                        }
                    }
                }
            },
            _make_cre(400, "HTTP error from Enphase endpoint (status=400)"),
            {
                "data": {
                    "modes": {
                        "greenCharging": {
                            "enabled": True,
                            "chargingMode": "GREEN_CHARGING",
                        }
                    }
                }
            },
            {
                "data": {
                    "modes": {
                        "manualCharging": {
                            "enabled": True,
                            "chargingMode": "MANUAL_CHARGING",
                        }
                    }
                }
            },
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", sleep)

    out = await client.set_charge_mode("SN", "MANUAL_CHARGING")

    assert out["verified_after_error"] is True
    sleep.assert_awaited_once_with(2)
    assert client._json.await_count == 4


@pytest.mark.asyncio
async def test_set_charge_mode_does_not_verify_schedule_required_error() -> None:
    client = _make_client()
    err = _make_cre(400, "HTTP error from Enphase endpoint (status=400)")
    err.enphase_scheduler_error = {
        "code": "iqevc_sch_10031",
        "display": "No Schedules enabled for Scheduled Charging",
    }
    client._json = AsyncMock(
        side_effect=[
            {
                "data": {
                    "modes": {
                        "manualCharging": {
                            "enabled": True,
                            "chargingMode": "MANUAL_CHARGING",
                        }
                    }
                }
            },
            err,
        ]
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await client.set_charge_mode("SN", "SCHEDULED_CHARGING")

    assert client._json.await_count == 2


@pytest.mark.asyncio
async def test_set_charge_mode_preserves_scheduler_unavailable_error() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(503, "Scheduler MS unavailable"))

    with pytest.raises(api.SchedulerUnavailable):
        await client.set_charge_mode(
            "SN", "MANUAL_CHARGING", previous_mode="GREEN_CHARGING"
        )

    assert client._json.await_count == 1


@pytest.mark.asyncio
async def test_set_charge_mode_reraises_when_400_readback_never_matches(
    monkeypatch,
) -> None:
    client = _make_client()
    err = _make_cre(400, "HTTP error from Enphase endpoint (status=400)")
    green_payload = {
        "data": {
            "modes": {
                "greenCharging": {
                    "enabled": True,
                    "chargingMode": "GREEN_CHARGING",
                }
            }
        }
    }
    client._json = AsyncMock(side_effect=[green_payload, err, *([green_payload] * 6)])
    sleep = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", sleep)

    with pytest.raises(aiohttp.ClientResponseError):
        await client.set_charge_mode("SN", "MANUAL_CHARGING")

    assert client._json.await_count == 8
    assert sleep.await_count == 5


@pytest.mark.asyncio
async def test_set_charge_mode_reraises_when_400_readback_fails() -> None:
    client = _make_client()
    err = _make_cre(400, "HTTP error from Enphase endpoint (status=400)")
    client._json = AsyncMock(side_effect=[err, aiohttp.ClientError("down")])

    with pytest.raises(aiohttp.ClientResponseError):
        await client.set_charge_mode(
            "SN", "MANUAL_CHARGING", previous_mode="GREEN_CHARGING"
        )

    assert client._json.await_count == 2


@pytest.mark.asyncio
async def test_green_charging_settings_filters_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": [
                {"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": True},
                "invalid",
            ]
        }
    )
    settings = await client.green_charging_settings("SN")
    assert settings == [{"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": True}]
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "GREEN_CHARGING" in args[1]
    assert "Authorization" in kwargs["headers"]


@pytest.mark.asyncio
async def test_green_charging_settings_handles_non_dict_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])
    assert await client.green_charging_settings("SN") == []


@pytest.mark.asyncio
async def test_green_charging_settings_handles_non_list_data() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": "nope"})
    assert await client.green_charging_settings("SN") == []


@pytest.mark.asyncio
async def test_set_green_battery_setting_passes_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    out = await client.set_green_battery_setting("SN", enabled=True)
    assert out == {"status": "ok"}
    args, kwargs = client._json.await_args
    assert kwargs["json"] == {
        "chargerSettingList": [
            {
                "chargerSettingName": GREEN_BATTERY_SETTING,
                "enabled": True,
                "value": None,
                "loader": False,
            }
        ]
    }


@pytest.mark.asyncio
async def test_storm_guard_alert_passes_headers() -> None:
    token = _make_token({"user_id": "42"})
    client = _make_client()
    client.update_credentials(
        eauth=token,
        cookie=(
            "enlighten_manager_token_production="
            f"{token}; XSRF-TOKEN=xsrf-token; other=1"
        ),
    )
    client._json = AsyncMock(return_value={"criticalAlertActive": False})
    out = await client.storm_guard_alert()
    assert out == {"criticalAlertActive": False}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "stormGuard" in args[1]
    assert kwargs["headers"]["e-auth-token"] is None
    assert kwargs["headers"]["Cookie"]
    assert kwargs["headers"]["Username"] == "42"
    assert kwargs["headers"]["Origin"] == "https://battery-profile-ui.enphaseenergy.com"
    assert (
        kwargs["headers"]["Referer"] == "https://battery-profile-ui.enphaseenergy.com/"
    )
    assert kwargs["headers"]["Authorization"] is None
    assert kwargs["headers"]["X-CSRF-Token"] is None
    assert kwargs["headers"]["X-Requested-With"] is None
    assert "requestid" in kwargs["headers"]


@pytest.mark.asyncio
async def test_storm_guard_profile_passes_params() -> None:
    token = _make_token({"user_id": "55"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="session=1")
    client._json = AsyncMock(return_value={"data": {}})
    await client.storm_guard_profile(locale="en-US")
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert kwargs["params"]["source"] == "enho"
    assert kwargs["params"]["userId"] == "55"
    assert kwargs["params"]["locale"] == "en-US"


@pytest.mark.asyncio
async def test_battery_site_settings_passes_params_and_headers() -> None:
    token = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth=token,
        cookie=(
            "enlighten_manager_token_production=cookie-bearer;"
            " XSRF-TOKEN=xsrf-token; other=1"
        ),
    )
    client._json = AsyncMock(return_value={"data": {}})

    out = await client.battery_site_settings()

    assert out == {"data": {}}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "siteSettings" in args[1]
    assert kwargs["params"]["userId"] == "77"
    assert kwargs["headers"]["e-auth-token"] is None
    assert kwargs["headers"]["Cookie"]
    assert kwargs["headers"]["requestid"]
    assert kwargs["headers"]["Username"] == "77"
    assert kwargs["headers"]["Origin"] == "https://battery-profile-ui.enphaseenergy.com"
    assert kwargs["headers"]["Authorization"] is None
    assert kwargs["headers"]["X-CSRF-Token"] is None
    assert kwargs["headers"]["X-Requested-With"] is None
    assert "requestid" in kwargs["headers"]


@pytest.mark.asyncio
async def test_site_tariff_billing_details_passes_tariff_headers() -> None:
    token = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth=token,
        cookie="session=1; XSRF-TOKEN=xsrf-token; other=1",
    )
    client._json = AsyncMock(return_value={"billingFrequency": "MONTH"})

    out = await client.site_tariff_billing_details()

    assert out == {"billingFrequency": "MONTH"}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "/service/tariff/tariff-ms/systems/SITE/billing-details" in args[1]
    assert (
        kwargs["headers"]["Accept"] == "application/json, text/javascript, */*; q=0.01"
    )
    assert kwargs["headers"]["Cookie"] == "session=1; XSRF-TOKEN=xsrf-token; other=1"
    assert kwargs["headers"]["e-auth-token"] == token
    assert kwargs["headers"]["Username"] == "77"
    assert kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert kwargs["headers"]["x-xsrf-token"] == "xsrf-token"
    assert "Requestid" in kwargs["headers"]


@pytest.mark.asyncio
async def test_site_tariff_billing_update_posts_payload_with_today() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"billingFrequency": "MONTH"})
    payload = {
        "anyBillPeriodStartDate": "2026-04-01",
        "billingFrequency": "MONTH",
        "billingIntervalValue": 1,
    }

    out = await client.site_tariff_billing_update(payload)

    assert out == {"billingFrequency": "MONTH"}
    args, kwargs = client._json.await_args
    assert args[0] == "POST"
    assert "/service/tariff/tariff-ms/systems/SITE/billing-details" in args[1]
    assert kwargs["json"] == payload
    assert kwargs["params"] == {"date": datetime.date.today().isoformat()}
    assert kwargs["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_site_tariff_billing_update_accepts_request_date_override() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"billingFrequency": "DAY"})

    await client.site_tariff_billing_update(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "DAY",
            "billingIntervalValue": 30,
        },
        request_date=datetime.date(2026, 4, 29),
    )

    _args, kwargs = client._json.await_args
    assert kwargs["params"] == {"date": "2026-04-29"}

    await client.site_tariff_billing_update(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "DAY",
            "billingIntervalValue": 30,
        },
        request_date=datetime.datetime(2026, 4, 30, 12, 0),
    )
    _args, kwargs = client._json.await_args
    assert kwargs["params"] == {"date": "2026-04-30"}

    await client.site_tariff_billing_update(
        {
            "anyBillPeriodStartDate": "2026-04-01",
            "billingFrequency": "DAY",
            "billingIntervalValue": 30,
        },
        request_date="2026-05-01",
    )
    _args, kwargs = client._json.await_args
    assert kwargs["params"] == {"date": "2026-05-01"}


@pytest.mark.asyncio
async def test_site_tariff_passes_include_site_details() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"purchase": {"typeId": "flat"}})

    out = await client.site_tariff()

    assert out == {"purchase": {"typeId": "flat"}}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "/service/tariff/tariff-ms/systems/SITE/tariff" in args[1]
    assert kwargs["params"] == {"include-site-details": "true"}


@pytest.mark.asyncio
async def test_site_tariff_rates_passes_rate_type_and_date() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": {"buyback": []}})

    out = await client.site_tariff_rates(
        rate_type="buyback",
        request_date=datetime.date(2026, 5, 1),
    )

    assert out == {"data": {"buyback": []}}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "/service/tariff/tariff-ms/systems/SITE/tariffs" in args[1]
    assert kwargs["params"] == {
        "rateType": "BUYBACK",
        "date": "2026-05-01",
        "includeUtility": "",
    }
    assert (
        kwargs["headers"]["Accept"] == "application/json, text/javascript, */*; q=0.01"
    )

    await client.site_tariff_rates(
        rate_type="purchase",
        request_date=datetime.datetime(2026, 5, 2, 12, 0),
    )
    _args, kwargs = client._json.await_args
    assert kwargs["params"]["date"] == "2026-05-02"

    await client.site_tariff_rates(rate_type="buyback", request_date="2026-05-03")
    _args, kwargs = client._json.await_args
    assert kwargs["params"]["date"] == "2026-05-03"

    await client.site_tariff_rates(rate_type="buyback")
    _args, kwargs = client._json.await_args
    assert kwargs["params"]["date"] == datetime.date.today().isoformat()


@pytest.mark.asyncio
async def test_site_tariff_bundle_fetches_billing_and_tariff() -> None:
    client = _make_client()
    client.site_tariff_billing_details = AsyncMock(
        return_value={"billingFrequency": "MONTH"}
    )
    client.site_tariff = AsyncMock(return_value={"purchase": {"typeId": "flat"}})

    out = await client.site_tariff_bundle()

    assert out == (
        {"billingFrequency": "MONTH"},
        {"purchase": {"typeId": "flat"}},
    )
    client.site_tariff_billing_details.assert_awaited_once_with()
    client.site_tariff.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_site_tariff_bundle_cancels_sibling_and_unwraps_single_failure() -> None:
    client = _make_client()
    err = RuntimeError("tariff billing failed")
    tariff_cancelled = asyncio.Event()
    client.site_tariff_billing_details = AsyncMock(side_effect=err)

    async def _site_tariff() -> dict:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            tariff_cancelled.set()
            raise
        return {"purchase": {"typeId": "flat"}}

    client.site_tariff = AsyncMock(side_effect=_site_tariff)

    with pytest.raises(RuntimeError) as exc:
        await client.site_tariff_bundle()

    assert exc.value is err
    assert tariff_cancelled.is_set()


@pytest.mark.asyncio
async def test_site_tariff_update_passes_user_id_and_write_headers() -> None:
    token = _make_token({"user_id": "77"})
    client = _make_client()
    client.update_credentials(
        eauth=token,
        cookie="session=1; XSRF-TOKEN=xsrf-token; other=1",
    )
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client.site_tariff_update({"purchase": {"typeId": "flat"}})

    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "/service/tariff/tariff-ms/systems/SITE/tariff" in args[1]
    assert kwargs["params"] == {"user-id": "77"}
    assert kwargs["json"] == {"purchase": {"typeId": "flat"}}
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["e-auth-token"] == token
    assert kwargs["headers"]["Username"] == "77"
    assert kwargs["headers"]["x-xsrf-token"] == "xsrf-token"


@pytest.mark.asyncio
async def test_notify_tariff_change_uses_scheduler_endpoint() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": "Request Accepted"})

    out = await client.notify_tariff_change()

    assert out == {"data": "Request Accepted"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "/service/evse_scheduler/api/v1/siteConfig/SITE/tariff_change" in args[1]
    assert kwargs["json"] is None
    assert kwargs["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_battery_settings_details_passes_params_and_headers() -> None:
    token = _make_token({"user_id": "99"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="session=1")
    client._json = AsyncMock(return_value={"data": {"chargeFromGrid": True}})

    out = await client.battery_settings_details()

    assert out == {"data": {"chargeFromGrid": True}}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "batterySettings" in args[1]
    assert kwargs["params"]["source"] == "enlm"
    assert kwargs["params"]["userId"] == "99"
    assert kwargs["headers"]["Username"] == "99"
    assert kwargs["headers"]["e-auth-token"] is None
    assert kwargs["headers"]["Authorization"] is None
    assert kwargs["headers"]["Cookie"]
    assert kwargs["headers"]["X-CSRF-Token"] is None
    assert kwargs["headers"]["X-Requested-With"] is None
    assert "requestid" in kwargs["headers"]


@pytest.mark.asyncio
async def test_battery_settings_details_wire_headers_match_official_web_shape() -> None:
    response = _FakeResponse(status=200, json_body={"data": {"chargeFromGrid": True}})
    session = _FakeSession([response])
    client = _make_client(session)
    token = _make_token({"user_id": "99"})
    client.update_credentials(
        eauth=token,
        cookie="session=1; XSRF-TOKEN=base-xsrf",
    )

    out = await client.battery_settings_details()

    assert out == {"data": {"chargeFromGrid": True}}
    headers = session.calls[0][2]["headers"]
    assert headers["Accept"] == "application/json, text/plain, */*"
    assert headers["Origin"] == "https://battery-profile-ui.enphaseenergy.com"
    assert headers["Referer"] == "https://battery-profile-ui.enphaseenergy.com/"
    assert headers["Username"] == "99"
    assert "Authorization" not in headers
    assert "e-auth-token" not in headers
    assert headers["Cookie"]
    assert "X-CSRF-Token" not in headers
    assert "X-Requested-With" not in headers
    assert "requestid" in headers


@pytest.mark.asyncio
async def test_set_battery_settings_payload_and_xsrf() -> None:
    token = _make_token({"user_id": "88"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="other=1")
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf=token")  # noqa: SLF001
    client._bp_xsrf_token = "xsrf=token"  # noqa: SLF001
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client.set_battery_settings({"veryLowSoc": 15})

    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "batterySettings" in args[1]
    assert kwargs["params"]["userId"] == "88"
    assert kwargs["headers"]["X-XSRF-Token"] == "xsrf=token"
    assert kwargs["headers"]["e-auth-token"] == token
    assert kwargs["headers"]["Authorization"] is None
    assert "requestid" in kwargs["headers"]
    assert kwargs["headers"]["Cookie"] is None
    assert kwargs["headers"]["X-CSRF-Token"] is None
    assert kwargs["json"] == {"veryLowSoc": 15}


@pytest.mark.asyncio
async def test_set_battery_settings_uses_requested_schedule_type_for_xsrf() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"message": "success"})

    async def _acquire(
        schedule_type: str = "cfg",
        *,
        variant: str = api._BATTERY_CONFIG_VARIANT_PRIMARY,
    ) -> str:
        assert variant == api._BATTERY_CONFIG_VARIANT_PRIMARY
        client._bp_xsrf_token = f"{schedule_type}-token"  # noqa: SLF001
        return client._bp_xsrf_token  # noqa: SLF001

    client._acquire_xsrf_token = AsyncMock(side_effect=_acquire)  # noqa: SLF001

    await client.set_battery_settings(
        {"dtgControl": {"enabled": True}},
        schedule_type="dtg",
    )

    client._acquire_xsrf_token.assert_awaited_once_with(
        "dtg", variant=api._BATTERY_CONFIG_VARIANT_PRIMARY
    )
    assert client._json.await_args.kwargs["headers"]["X-XSRF-Token"] == "dtg-token"
    assert client._json.await_args.kwargs["headers"]["e-auth-token"] == "EAUTH"
    assert client._json.await_args.kwargs["headers"]["Authorization"] is None
    assert client._json.await_args.kwargs["headers"]["Cookie"] is None
    assert client._json.await_args.kwargs["headers"]["X-CSRF-Token"] is None
    assert "requestid" in client._json.await_args.kwargs["headers"]


@pytest.mark.asyncio
async def test_set_battery_settings_retries_403_with_lean_official_web_variant(
    caplog,
) -> None:
    def _get_bootstrap() -> _FakeResponse:
        response = _FakeResponse(status=500, json_body={})
        response.headers = CIMultiDict()
        return response

    validate_primary = _FakeResponse(status=200, json_body={"isValid": True})
    validate_primary.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=cfg-token; Path=/; Secure")]
    )
    initial_write = _FakeResponse(
        status=403,
        json_body={},
        text_body='{"timestamp":"2026-04-11T06:15:59.504+00:00","status":403}',
    )
    validate_fallback = _FakeResponse(status=200, json_body={"isValid": True})
    validate_fallback.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=cfg-token-lean; Path=/; Secure")]
    )
    fallback_write = _FakeResponse(
        status=403,
        json_body={},
        text_body='{"timestamp":"2026-04-11T06:16:00.098+00:00","status":403}',
    )
    validate_cookie = _FakeResponse(status=200, json_body={"isValid": True})
    validate_cookie.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=cfg-token-cookie; Path=/; Secure")]
    )
    cookie_write = _FakeResponse(
        status=403,
        json_body={},
        text_body='{"timestamp":"2026-04-11T06:16:00.412+00:00","status":403}',
    )
    validate_mixed = _FakeResponse(status=200, json_body={"isValid": True})
    validate_mixed.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=cfg-token-mixed; Path=/; Secure")]
    )
    mixed_write = _FakeResponse(
        status=403,
        json_body={},
        text_body='{"timestamp":"2026-04-11T06:16:00.687+00:00","status":403}',
    )
    # Each _acquire_xsrf_token attempt now issues a GET siteSettings bootstrap
    # first, falling back to the legacy POST isValid when the response lacks
    # an ``x-csrf-token`` header. Feed a failing GET ahead of every validate.
    session = _FakeSession(
        [
            _get_bootstrap(),
            validate_primary,
            initial_write,
            _get_bootstrap(),
            validate_fallback,
            fallback_write,
            _get_bootstrap(),
            validate_cookie,
            cookie_write,
            _get_bootstrap(),
            validate_mixed,
            mixed_write,
        ]
    )
    primary_token = _make_token({"user_id": "88"})
    client = _make_client(session)
    client.update_credentials(
        eauth=primary_token,
        cookie="session=1",
    )

    @asynccontextmanager
    async def _request_session(*, cookie_header_only: bool = False):
        yield session

    client._request_session = _request_session  # noqa: SLF001

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(aiohttp.ClientResponseError) as err:
            await client.set_battery_settings({"veryLowSoc": 15})

    assert err.value.status == 403
    assert len(session.calls) == 12
    # Each attempt: GET siteSettings (bootstrap) + POST isValid + PUT write.
    assert [call[0] for call in session.calls] == [
        "GET",
        "POST",
        "PUT",
        "GET",
        "POST",
        "PUT",
        "GET",
        "POST",
        "PUT",
        "GET",
        "POST",
        "PUT",
    ]
    primary_headers = session.calls[2][2]["headers"]
    fallback_headers = session.calls[5][2]["headers"]
    cookie_headers = session.calls[8][2]["headers"]
    mixed_headers = session.calls[11][2]["headers"]
    assert "Authorization" not in primary_headers
    assert primary_headers["e-auth-token"] == primary_token
    assert "requestid" in primary_headers
    assert primary_headers["Username"] == "88"
    assert primary_headers["X-XSRF-Token"] == "cfg-token"
    assert "Cookie" not in primary_headers
    assert "X-CSRF-Token" not in primary_headers
    assert "Authorization" not in fallback_headers
    assert "e-auth-token" not in fallback_headers
    assert "requestid" not in fallback_headers
    assert fallback_headers["Username"] == "88"
    assert fallback_headers["X-XSRF-Token"] == "cfg-token-lean"
    assert "X-CSRF-Token" not in fallback_headers
    assert "Authorization" not in cookie_headers
    assert cookie_headers["e-auth-token"] == primary_token
    assert cookie_headers["Cookie"] == "session=1"
    assert "X-CSRF-Token" not in cookie_headers
    assert "X-XSRF-Token" not in cookie_headers
    assert cookie_headers["X-Requested-With"] == "XMLHttpRequest"
    assert "requestid" not in cookie_headers
    assert mixed_headers["Authorization"] == f"Bearer {primary_token}"
    assert mixed_headers["e-auth-token"] == primary_token
    assert mixed_headers["Cookie"].startswith("session=1;")
    assert "BP-XSRF-Token=cfg-token-mixed" in mixed_headers["Cookie"]
    assert mixed_headers["X-CSRF-Token"] == "cfg-token-mixed"
    assert mixed_headers["X-XSRF-Token"] == "cfg-token-mixed"
    assert mixed_headers["X-Requested-With"] == "XMLHttpRequest"
    assert "requestid" not in mixed_headers
    assert "Retrying BatteryConfig write" in caplog.text
    assert "official_web_lean" in caplog.text
    assert "cookie_eauth_compatible" in caplog.text
    assert "mixed_auth_compatible" in caplog.text
    assert "attempt_id=battery_settings_primary_source" in caplog.text
    assert "attempt_id=battery_settings_lean_source" in caplog.text
    assert "attempt_id=battery_settings_cookie_eauth_source" in caplog.text
    assert "attempt_id=battery_settings_mixed_source" in caplog.text
    assert client._bp_xsrf_token is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_set_battery_profile_uses_canonical_batteryconfig_write_path() -> None:
    token = _make_token({"user_id": "100"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="session=1")
    client._acquire_xsrf_token = AsyncMock(return_value="fresh-xsrf")  # noqa: SLF001
    client._bp_xsrf_token = "fresh-xsrf"  # noqa: SLF001
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client.set_battery_profile(
        profile="self-consumption",
        battery_backup_percentage=10,
        operation_mode_sub_type="prioritize-energy",
        devices=[
            {"uuid": "abc", "deviceType": "iqEvse", "enable": False},
            "skip",
        ],
    )

    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args == (
        "PUT",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/profile/SITE",
    )
    assert kwargs["params"] == {"userId": "100", "source": "enho"}
    assert kwargs["json"] == {
        "profile": "self-consumption",
        "batteryBackupPercentage": 10,
        "operationModeSubType": "prioritize-energy",
        "devices": [{"uuid": "abc", "deviceType": "iqEvse", "enable": False}],
    }
    assert kwargs["headers"]["e-auth-token"] == token
    assert kwargs["headers"]["Username"] == "100"
    assert kwargs["headers"]["X-XSRF-Token"] == "fresh-xsrf"
    assert kwargs["headers"]["requestid"]
    assert kwargs["headers"]["Authorization"] is None
    assert kwargs["headers"]["Cookie"] is None
    assert kwargs["headers"]["X-CSRF-Token"] is None
    assert kwargs["headers"]["X-Requested-With"] is None


def test_battery_config_endpoint_family_identifies_schedule_routes() -> None:
    client = _make_client()

    assert (
        client._battery_config_endpoint_family(  # noqa: SLF001
            "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/"
            "battery/sites/SITE/schedules/sched-1"
        )
        == "schedules"
    )


def test_battery_config_attempt_json_body_returns_list_unchanged() -> None:
    client = _make_client()
    payload = [{"enabled": True}]
    attempt = api._BatteryConfigWriteAttempt(  # noqa: SLF001
        attempt_id="list-payload",
        auth_mode=api._BATTERY_CONFIG_VARIANT_PRIMARY,
    )

    assert (
        client._battery_config_attempt_json_body(payload, "battery_settings", attempt)
        == payload
    )  # noqa: SLF001


def test_remember_battery_config_capabilities_handles_payload_shapes() -> None:
    client = _make_client()

    client._remember_battery_config_capabilities(["bad"])  # noqa: SLF001
    assert client._battery_config_supports_mqtt is None  # noqa: SLF001

    client._remember_battery_config_capabilities({"supportsMqtt": True})  # noqa: SLF001
    assert client._battery_config_supports_mqtt is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_config_request_raises_when_no_variants_are_available() -> None:
    client = _make_client()
    client._battery_config_variant_order = MagicMock(return_value=[])  # noqa: SLF001

    with pytest.raises(aiohttp.ClientError, match="exhausted variants"):
        await client._battery_config_request(  # noqa: SLF001
            "GET",
            "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/profile/SITE",
        )


@pytest.mark.asyncio
async def test_battery_config_request_retries_403_and_caches_variant_when_bootstrapping() -> (
    None
):
    client = _make_client()
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    client._json = AsyncMock(
        side_effect=[_make_cre(403, "Forbidden"), {"message": "success"}]
    )

    out = await client._battery_config_request(  # noqa: SLF001
        "PUT",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/profile/SITE",
        json_body={"profile": "self-consumption"},
        params={"userId": "88", "source": "enho"},
        endpoint_family="profile",
        bootstrap_xsrf=True,
        cache_on_success=True,
    )

    assert out == {"message": "success"}
    assert client._acquire_xsrf_token.await_count == 2  # noqa: SLF001
    assert (
        client._battery_config_cached_variant("profile")  # noqa: SLF001
        == api._BATTERY_CONFIG_VARIANT_LEAN
    )
    assert client._bp_xsrf_token is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_config_request_retries_login_wall_with_token_variant() -> None:
    client = _make_client()
    client.update_credentials(cookie="_enlighten_4_session=fresh-session")
    login_wall = api.EnphaseLoginWallUnauthorized(
        endpoint="/service/batteryConfig/api/v1/siteSettings/SITE",
        request_label="GET /service/batteryConfig/api/v1/siteSettings/[site]",
        status=200,
        content_type="text/html",
        body_preview_redacted="Enlighten Login",
    )
    client._json = AsyncMock(side_effect=[login_wall, {"message": "success"}])

    out = await client._battery_config_request(  # noqa: SLF001
        "GET",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/siteSettings/SITE",
        endpoint_family="profile",
        cache_on_success=True,
    )

    assert out == {"message": "success"}
    first_call, second_call = client._json.await_args_list
    assert first_call.kwargs["headers"]["Cookie"]
    assert first_call.kwargs["headers"]["e-auth-token"] is None
    assert second_call.kwargs["headers"]["Cookie"] is None
    assert second_call.kwargs["headers"]["e-auth-token"] == "EAUTH"
    assert (
        client._battery_config_cached_variant("profile")  # noqa: SLF001
        == api._BATTERY_CONFIG_VARIANT_PRIMARY
    )


@pytest.mark.asyncio
async def test_battery_config_request_reraises_login_wall_after_all_variants() -> None:
    client = _make_client()
    client.update_credentials(cookie="")
    login_wall = api.EnphaseLoginWallUnauthorized(
        endpoint="/service/batteryConfig/api/v1/siteSettings/SITE",
        request_label="GET /service/batteryConfig/api/v1/siteSettings/[site]",
        status=200,
        content_type="text/html",
        body_preview_redacted="Enlighten Login",
    )
    client._json = AsyncMock(side_effect=[login_wall, login_wall])

    with pytest.raises(api.EnphaseLoginWallUnauthorized):
        await client._battery_config_request(  # noqa: SLF001
            "GET",
            "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/siteSettings/SITE",
            endpoint_family="profile",
        )

    assert client._json.await_count == 2


@pytest.mark.asyncio
async def test_battery_config_request_reraises_401() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(401, "Unauthorized"))

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client._battery_config_request(  # noqa: SLF001
            "GET",
            "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/profile/SITE",
            endpoint_family="profile",
        )

    assert err.value.status == 401


@pytest.mark.asyncio
async def test_battery_config_request_reraises_non_403_errors() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500, "boom"))

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client._battery_config_request(  # noqa: SLF001
            "GET",
            "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/profile/SITE",
            endpoint_family="profile",
        )

    assert err.value.status == 500


@pytest.mark.asyncio
async def test_battery_config_write_request_reraises_non_403_errors() -> None:
    client = _make_client()
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    client._json = AsyncMock(side_effect=_make_cre(401, "Unauthorized"))

    with pytest.raises(aiohttp.ClientResponseError):
        await client._battery_config_write_request(  # noqa: SLF001
            "PUT",
            "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/batterySettings/SITE",
            json_body={"veryLowSoc": 15},
            params={"userId": "88", "source": "enho"},
        )

    assert client._json.await_count == 1
    assert client._bp_xsrf_token is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_config_write_request_reraises_403_after_fallback() -> None:
    client = _make_client()
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    client._json = AsyncMock(side_effect=_make_cre(403, "Forbidden"))
    attempts = client._battery_config_write_attempts(  # noqa: SLF001
        "profile",
        write_intent="generic",
        supports_mqtt=None,
        params={"userId": "88"},
        json_body={"profile": "self-consumption"},
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await client._battery_config_write_request(  # noqa: SLF001
            "PUT",
            "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/profile/SITE",
            json_body={"profile": "self-consumption"},
            params={"userId": "88"},
        )

    assert client._json.await_count == len(attempts)
    assert client._acquire_xsrf_token.await_count == len(attempts)
    assert client._acquire_xsrf_token.await_args_list[0].args == ("cfg",)
    assert client._acquire_xsrf_token.await_args_list[0].kwargs == {
        "variant": api._BATTERY_CONFIG_VARIANT_PRIMARY
    }
    assert client._acquire_xsrf_token.await_args_list[-1].args == ("cfg",)


@pytest.mark.asyncio
async def test_battery_config_write_request_caches_successful_fallback_variant() -> (
    None
):
    client = _make_client()
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    client._json = AsyncMock(
        side_effect=[
            _make_cre(403, "Forbidden"),
            {"message": "success"},
            {"message": "success"},
        ]
    )

    first = await client._battery_config_write_request(  # noqa: SLF001
        "PUT",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/batterySettings/SITE",
        json_body={"veryLowSoc": 15},
        params={"userId": "88", "source": "enho"},
    )
    second = await client._battery_config_write_request(  # noqa: SLF001
        "PUT",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/batterySettings/SITE",
        json_body={"veryLowSoc": 20},
        params={"userId": "88", "source": "enho"},
    )

    assert first == {"message": "success"}
    assert second == {"message": "success"}
    first_call = client._json.await_args_list[0]
    fallback_call = client._json.await_args_list[1]
    cached_call = client._json.await_args_list[2]
    assert first_call.kwargs["headers"]["requestid"]
    assert "e-auth-token" in first_call.kwargs["headers"]
    assert fallback_call.kwargs["headers"]["requestid"] is None
    assert fallback_call.kwargs["headers"]["e-auth-token"] is None
    assert cached_call.kwargs["headers"]["requestid"] is None
    assert cached_call.kwargs["headers"]["e-auth-token"] is None
    assert (
        client._battery_config_cached_write_attempt(  # noqa: SLF001
            "battery_settings",
            supports_mqtt=None,
        )
        == "battery_settings_lean"
    )


@pytest.mark.asyncio
async def test_battery_config_write_request_updates_cached_attempt_after_403() -> None:
    client = _make_client()
    client._cache_battery_config_write_attempt(  # noqa: SLF001
        "battery_settings",
        "battery_settings_primary_source",
        supports_mqtt=None,
    )
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    client._json = AsyncMock(
        side_effect=[_make_cre(403, "Forbidden"), {"message": "success"}]
    )

    out = await client._battery_config_write_request(  # noqa: SLF001
        "PUT",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/batterySettings/SITE",
        json_body={"veryLowSoc": 15},
        params={"userId": "88", "source": "enho"},
    )

    assert out == {"message": "success"}
    assert (
        client._battery_config_cached_write_attempt(  # noqa: SLF001
            "battery_settings",
            supports_mqtt=None,
        )
        == "battery_settings_lean"
    )


@pytest.mark.asyncio
async def test_battery_config_write_request_does_not_update_cached_attempt_on_401() -> (
    None
):
    client = _make_client()
    client._cache_battery_config_write_attempt(  # noqa: SLF001
        "battery_settings",
        "battery_settings_primary_source",
        supports_mqtt=None,
    )
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    client._json = AsyncMock(side_effect=_make_cre(401, "Unauthorized"))

    with pytest.raises(aiohttp.ClientResponseError):
        await client._battery_config_write_request(  # noqa: SLF001
            "PUT",
            "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/batterySettings/SITE",
            json_body={"veryLowSoc": 15},
            params={"userId": "88", "source": "enho"},
        )

    assert (
        client._battery_config_cached_write_attempt(  # noqa: SLF001
            "battery_settings",
            supports_mqtt=None,
        )
        == "battery_settings_primary_source"
    )


@pytest.mark.asyncio
async def test_battery_config_write_request_raises_last_error_when_duplicate_attempts_are_skipped() -> (
    None
):
    client = _make_client()
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    duplicate = api._BatteryConfigWriteAttempt(  # noqa: SLF001
        attempt_id="duplicate",
        auth_mode=api._BATTERY_CONFIG_VARIANT_PRIMARY,
    )
    client._battery_config_write_attempts = MagicMock(  # noqa: SLF001
        return_value=[duplicate, duplicate]
    )
    client._json = AsyncMock(side_effect=[_make_cre(403, "Forbidden")])

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client._battery_config_write_request(  # noqa: SLF001
            "PUT",
            "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/batterySettings/SITE",
            json_body={"veryLowSoc": 15},
            params={"userId": "88", "source": "enho"},
            endpoint_family="battery_settings",
            write_intent="battery_settings_update",
        )

    assert err.value.status == 403
    assert client._json.await_count == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_config_write_request_raises_when_no_attempts_are_available() -> (
    None
):
    client = _make_client()
    client._battery_config_write_attempts = MagicMock(return_value=[])  # noqa: SLF001

    with pytest.raises(aiohttp.ClientError, match="exhausted variants"):
        await client._battery_config_write_request(  # noqa: SLF001
            "PUT",
            "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/batterySettings/SITE",
            json_body={"veryLowSoc": 15},
            params={"userId": "88", "source": "enho"},
            endpoint_family="battery_settings",
            write_intent="battery_settings_update",
        )


@pytest.mark.asyncio
async def test_battery_config_write_request_retries_without_source_when_mqtt_supported() -> (
    None
):
    client = _make_client()
    client._battery_config_supports_mqtt = True  # noqa: SLF001
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    client._json = AsyncMock(
        side_effect=[
            _make_cre(403, "Forbidden"),
            _make_cre(403, "Forbidden"),
            {"message": "success"},
        ]
    )

    out = await client.set_battery_settings(
        {
            "acceptedItcDisclaimer": "2026-04-17T09:30:00+00:00",
            "chargeFromGrid": True,
        }
    )

    assert out == {"message": "success"}
    first_call, second_call, third_call = client._json.await_args_list
    assert first_call.kwargs["params"] == {"source": "enho"}
    assert second_call.kwargs["params"] == {"source": "enho"}
    assert third_call.kwargs["params"] == {}
    assert third_call.kwargs["headers"]["requestid"]
    assert third_call.kwargs["headers"]["e-auth-token"] == "EAUTH"
    assert (
        client._battery_config_cached_write_attempt(  # noqa: SLF001
            "battery_settings",
            supports_mqtt=True,
        )
        == "battery_settings_primary_no_source"
    )


@pytest.mark.asyncio
async def test_battery_config_write_request_cache_isolated_by_endpoint_family_and_mqtt() -> (
    None
):
    client = _make_client()
    client._cache_battery_config_write_attempt(  # noqa: SLF001
        "battery_settings",
        "battery_settings_primary_no_source",
        supports_mqtt=True,
    )
    client._cache_battery_config_write_attempt(  # noqa: SLF001
        "profile",
        "profile_mixed_compat",
        supports_mqtt=True,
    )

    assert (
        client._battery_config_cached_write_attempt(  # noqa: SLF001
            "battery_settings",
            supports_mqtt=True,
        )
        == "battery_settings_primary_no_source"
    )
    assert (
        client._battery_config_cached_write_attempt(  # noqa: SLF001
            "battery_settings",
            supports_mqtt=False,
        )
        is None
    )
    assert (
        client._battery_config_cached_write_attempt(  # noqa: SLF001
            "profile",
            supports_mqtt=True,
        )
        == "profile_mixed_compat"
    )


def test_battery_config_write_attempts_prefer_cookie_compat_when_raw_xsrf_available() -> (
    None
):
    token = _make_token({"user_id": "88"})
    client = _make_client()
    client.update_credentials(
        cookie=(
            "session=1; BP-XSRF-Token=raw-xsrf; "
            f"enlighten_manager_token_production={token}"
        )
    )

    attempts = client._battery_config_write_attempts(  # noqa: SLF001
        "battery_settings",
        write_intent="battery_settings_update",
        supports_mqtt=True,
        params={"userId": "88", "source": "enho"},
        json_body={
            "chargeFromGrid": True,
            "acceptedItcDisclaimer": "2026-04-17T09:30:00+00:00",
        },
    )

    assert attempts[0].attempt_id == "battery_settings_cookie_eauth_source"
    assert attempts[1].attempt_id == "battery_settings_cookie_eauth_no_source"


def test_battery_config_schedule_write_attempts_include_cookie_compat() -> None:
    token = _make_token({"user_id": "88"})
    client = _make_client()
    client.update_credentials(
        cookie=(
            "session=1; BP-XSRF-Token=raw-xsrf; "
            f"enlighten_manager_token_production={token}"
        )
    )

    attempts = client._battery_config_write_attempts(  # noqa: SLF001
        "schedules",
        write_intent="generic",
        supports_mqtt=None,
        params=None,
        json_body={
            "timezone": "Australia/Melbourne",
            "startTime": "00:00",
            "endTime": "23:59",
            "scheduleType": "DTG",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "limit": 21,
            "isEnabled": False,
        },
    )

    assert attempts[0].attempt_id == "schedules_cookie_eauth"
    assert [attempt.attempt_id for attempt in attempts] == [
        "schedules_cookie_eauth",
        "schedules_primary",
        "schedules_lean",
        "schedules_mixed",
    ]


def test_battery_config_write_attempts_cover_stateful_profile_and_settings_variants() -> (
    None
):
    token = _make_token({"user_id": "88"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="session=1")
    client._battery_config_write_bases["profile"] = {  # noqa: SLF001
        "profile": "self-consumption",
        "devices": {"iqEvse": {"enabled": True}},
    }
    client._battery_config_write_bases["battery_settings"] = {  # noqa: SLF001
        "chargeFromGrid": False,
        "dtgControl": {"enabled": False},
        "acceptedItcDisclaimer": "2026-04-17T09:30:00+00:00",
    }

    profile_attempts = client._battery_config_write_attempts(  # noqa: SLF001
        "profile",
        write_intent="profile_update",
        supports_mqtt=True,
        params={"userId": "88", "source": "enho"},
        json_body={"profile": "full_backup", "devices": {"iqEvse": {"enabled": False}}},
    )
    assert [attempt.attempt_id for attempt in profile_attempts] == [
        "profile_primary",
        "profile_primary_no_devices",
        "profile_primary_no_source",
        "profile_stateful_primary",
        "profile_stateful_primary_no_source",
        "profile_cookie_eauth_compat",
        "profile_stateful_cookie_eauth_compat",
        "profile_mixed_compat",
        "profile_stateful_mixed_compat",
    ]

    settings_attempts = client._battery_config_write_attempts(  # noqa: SLF001
        "battery_settings",
        write_intent="battery_settings_update",
        supports_mqtt=True,
        params={"userId": "88", "source": "enho"},
        json_body={
            "chargeFromGrid": True,
            "acceptedItcDisclaimer": "2026-04-17T09:30:00+00:00",
        },
    )
    assert [attempt.attempt_id for attempt in settings_attempts] == [
        "battery_settings_primary_source",
        "battery_settings_lean_source",
        "battery_settings_primary_no_source",
        "battery_settings_lean_no_source",
        "battery_settings_stateful_primary_source",
        "battery_settings_stateful_lean_source",
        "battery_settings_stateful_primary_no_source",
        "battery_settings_stateful_lean_no_source",
        "battery_settings_cookie_eauth_source",
        "battery_settings_cookie_eauth_no_source",
        "battery_settings_stateful_cookie_eauth_source",
        "battery_settings_stateful_cookie_eauth_no_source",
        "battery_settings_mixed_source",
        "battery_settings_mixed_no_source",
        "battery_settings_stateful_mixed_source",
        "battery_settings_stateful_mixed_no_source",
        "battery_settings_disclaimer_true",
        "battery_settings_stateful_disclaimer_true",
    ]

    disclaimer_attempts = client._battery_config_write_attempts(  # noqa: SLF001
        "battery_settings",
        write_intent="battery_settings_disclaimer_accept",
        supports_mqtt=None,
        params=None,
        json_body={"disclaimer-type": "itc"},
    )
    assert [attempt.attempt_id for attempt in disclaimer_attempts] == [
        "battery_settings_disclaimer_primary",
        "battery_settings_disclaimer_lean",
        "battery_settings_disclaimer_cookie_eauth",
        "battery_settings_disclaimer_mixed",
    ]


def test_battery_config_write_payload_helpers_cover_merge_and_base_fallbacks() -> None:
    client = _make_client()

    assert client._battery_config_payload_data(None) is None  # noqa: SLF001
    assert client._battery_config_payload_data({"a": 1}) == {"a": 1}  # noqa: SLF001

    client._remember_battery_config_write_base(
        "other", {"data": {"a": 1}}
    )  # noqa: SLF001
    client._remember_battery_config_write_base("profile", "bad")  # noqa: SLF001
    assert client._battery_config_write_bases == {}  # noqa: SLF001

    client._remember_battery_config_write_base(  # noqa: SLF001
        "battery_settings",
        {
            "data": {
                "dtgControl": {"enabled": False, "startTime": "00:00"},
                "powerMatchControl": {
                    "show": True,
                    "enabled": False,
                    "locked": False,
                    "futureField": {"value": 1},
                },
                "devices": {"iqEvse": {"enabled": True}},
                "ignored": 1,
            }
        },
    )
    assert client._battery_config_write_bases["battery_settings"] == {  # noqa: SLF001
        "dtgControl": {"enabled": False, "startTime": "00:00"},
        "powerMatchControl": {
            "show": True,
            "enabled": False,
            "locked": False,
            "futureField": {"value": 1},
        },
        "devices": {"iqEvse": {"enabled": True}},
    }

    assert (
        client._battery_config_merged_write_payload("battery_settings", None) is None
    )  # noqa: SLF001
    assert client._battery_config_merged_write_payload(
        "missing", {"chargeFromGrid": True}
    ) == {  # noqa: SLF001
        "chargeFromGrid": True
    }
    assert client._battery_config_merged_write_payload(  # noqa: SLF001
        "battery_settings",
        {
            "dtgControl": {"enabled": True},
            "powerMatchControl": {"enabled": True},
            "chargeFromGrid": True,
        },
    ) == {
        "dtgControl": {"enabled": True, "startTime": "00:00"},
        "powerMatchControl": {
            "show": True,
            "enabled": True,
            "locked": False,
            "futureField": {"value": 1},
        },
        "devices": {"iqEvse": {"enabled": True}},
        "chargeFromGrid": True,
    }

    attempt = api._BatteryConfigWriteAttempt(  # noqa: SLF001
        attempt_id="stateful",
        auth_mode=api._BATTERY_CONFIG_VARIANT_PRIMARY,
        merged_payload=True,
        preserve_base_devices=True,
    )
    assert client._battery_config_attempt_json_body(  # noqa: SLF001
        {
            "dtgControl": {"enabled": True},
            "devices": {"iqEvse": {"enabled": False}},
        },
        "battery_settings",
        attempt,
    ) == {
        "dtgControl": {"enabled": True, "startTime": "00:00"},
        "powerMatchControl": {
            "show": True,
            "enabled": False,
            "locked": False,
            "futureField": {"value": 1},
        },
        "devices": {"iqEvse": {"enabled": True}},
    }
    assert (  # noqa: SLF001
        client._battery_config_attempt_json_body(None, "battery_settings", attempt)
        is None
    )
    lean_attempt = api._BatteryConfigWriteAttempt(  # noqa: SLF001
        attempt_id="lean",
        auth_mode=api._BATTERY_CONFIG_VARIANT_PRIMARY,
    )
    assert (  # noqa: SLF001
        client._battery_config_attempt_json_body(None, "battery_settings", lean_attempt)
        is None
    )


def test_battery_config_write_payload_helpers_preserve_inputs() -> None:
    client = _make_client()
    read_payload = {
        "data": {
            "dtgControl": {
                "enabled": False,
                "startTime": "00:00",
                "schedule": {"weekday": True},
            },
            "devices": {"iqEvse": {"enabled": True}},
        }
    }
    client._remember_battery_config_write_base(  # noqa: SLF001
        "battery_settings", read_payload
    )
    write_payload = {
        "dtgControl": {"schedule": {"weekday": False}},
        "devices": {"iqEvse": {"enabled": False}},
    }

    merged = client._battery_config_merged_write_payload(  # noqa: SLF001
        "battery_settings", write_payload
    )

    write_payload["dtgControl"]["schedule"]["weekday"] = "mutated"
    assert merged == {
        "dtgControl": {
            "enabled": False,
            "startTime": "00:00",
            "schedule": {"weekday": False},
        },
        "devices": {"iqEvse": {"enabled": False}},
    }
    assert client._battery_config_write_bases["battery_settings"] == {  # noqa: SLF001
        "dtgControl": {
            "enabled": False,
            "startTime": "00:00",
            "schedule": {"weekday": True},
        },
        "devices": {"iqEvse": {"enabled": True}},
    }

    attempt = api._BatteryConfigWriteAttempt(  # noqa: SLF001
        attempt_id="lean",
        auth_mode=api._BATTERY_CONFIG_VARIANT_PRIMARY,
        strip_devices=True,
        disclaimer_bool_true=True,
    )
    request_payload = {
        "acceptedItcDisclaimer": "true",
        "devices": {"iqEvse": {"enabled": False}},
        "dtgControl": {"enabled": True},
    }
    adjusted = client._battery_config_attempt_json_body(  # noqa: SLF001
        request_payload, "battery_settings", attempt
    )

    assert adjusted == {
        "acceptedItcDisclaimer": True,
        "dtgControl": {"enabled": True},
    }
    assert request_payload == {
        "acceptedItcDisclaimer": "true",
        "devices": {"iqEvse": {"enabled": False}},
        "dtgControl": {"enabled": True},
    }


@pytest.mark.asyncio
async def test_battery_config_write_request_uses_cookie_header_only_for_cookie_attempt() -> (
    None
):
    token = _make_token({"user_id": "88"})
    client = _make_client()
    client.update_credentials(
        cookie=(
            "session=1; BP-XSRF-Token=raw-xsrf; "
            f"enlighten_manager_token_production={token}"
        )
    )
    client._battery_config_write_attempts = MagicMock(  # noqa: SLF001
        return_value=[
            api._BatteryConfigWriteAttempt(  # noqa: SLF001
                attempt_id="cookie-only",
                auth_mode=api._BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                prefer_existing_xsrf=True,
            )
        ]
    )
    client._acquire_xsrf_token = AsyncMock(return_value="fresh-xsrf")  # noqa: SLF001
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client._battery_config_write_request(  # noqa: SLF001
        "PUT",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/batterySettings/SITE",
        json_body={"chargeFromGrid": True},
        params={"userId": "88", "source": "enho"},
        endpoint_family="battery_settings",
        write_intent="battery_settings_update",
    )

    assert out == {"message": "success"}
    client._acquire_xsrf_token.assert_not_awaited()  # noqa: SLF001
    assert client._json.await_args.kwargs["use_cookie_header_only"] is True


@pytest.mark.asyncio
async def test_request_session_reuses_injected_cookie_header_session() -> None:
    shared_session = _DefaultSession()
    stateless_session = _DefaultSession()
    stateless_session.cookie_jar = aiohttp.DummyCookieJar()
    client = api.EnphaseEVClient(
        shared_session,
        "SITE",
        "EAUTH",
        "COOKIE",
        cookie_header_session=stateless_session,
    )

    async with client._request_session(
        cookie_header_only=False
    ) as shared:  # noqa: SLF001
        assert shared is shared_session

    async with client._request_session(
        cookie_header_only=True
    ) as first:  # noqa: SLF001
        assert first is stateless_session
    async with client._request_session(
        cookie_header_only=True
    ) as second:  # noqa: SLF001
        assert second is first


@pytest.mark.asyncio
async def test_request_session_accepts_stateless_primary_session() -> None:
    stateless_session = _DefaultSession()
    stateless_session.cookie_jar = aiohttp.DummyCookieJar()
    client = api.EnphaseEVClient(stateless_session, "SITE", "EAUTH", "COOKIE")

    async with client._request_session(  # noqa: SLF001
        cookie_header_only=True
    ) as request_session:
        assert request_session is stateless_session


@pytest.mark.asyncio
async def test_cookie_header_only_request_preserves_raw_cookie_header() -> None:
    shared_session = _DefaultSession()
    shared_session.cookie_jar = SimpleNamespace(
        filter_cookies=lambda _url: SimpleCookie("hidden=jar-cookie")
    )
    stateless_session = _FakeSession(
        [
            _FakeResponse(status=200, json_body={"attempt": 1}),
            _FakeResponse(status=200, json_body={"attempt": 2}),
        ]
    )
    stateless_session.cookie_jar = aiohttp.DummyCookieJar()
    client = api.EnphaseEVClient(
        shared_session,
        "SITE",
        None,
        "session=raw; BP-XSRF-Token=raw-token",
        cookie_header_session=stateless_session,
    )

    for expected_attempt in (1, 2):
        payload = await client._json(  # noqa: SLF001
            "PUT",
            "https://example.test/battery-settings",
            headers={"Cookie": "session=raw; BP-XSRF-Token=raw-token"},
            use_cookie_header_only=True,
        )
        assert payload == {"attempt": expected_attempt}

    assert len(stateless_session.calls) == 2
    assert all(
        kwargs["headers"]["Cookie"] == "session=raw; BP-XSRF-Token=raw-token"
        for _method, _url, kwargs in stateless_session.calls
    )
    assert all(
        "hidden=jar-cookie" not in kwargs["headers"]["Cookie"]
        for _method, _url, kwargs in stateless_session.calls
    )


@pytest.mark.asyncio
async def test_cookie_header_only_request_requires_stateless_session() -> None:
    client = _make_client()

    with pytest.raises(
        RuntimeError,
        match="require an injected stateless session",
    ):
        async with client._request_session(cookie_header_only=True):  # noqa: SLF001
            pass


@pytest.mark.asyncio
async def test_async_close_detaches_private_session_once() -> None:
    stateless_session = MagicMock(closed=False)
    client = api.EnphaseEVClient(
        _DefaultSession(),
        "SITE",
        "EAUTH",
        "COOKIE",
        cookie_header_session=stateless_session,
    )

    await client.async_close()
    await client.async_close()

    stateless_session.detach.assert_called_once_with()
    assert client._cookie_header_session is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_requested_validation_payload() -> None:
    token = _make_token({"user_id": "88"})
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=fresh-token; Path=/; Secure")]
    )
    session = _FakeSession([bootstrap, response])
    client = _make_client(session)
    client.update_credentials(
        eauth=token,
        cookie="session=1; BP-XSRF-Token=stale-token; other=1",
    )

    out = await client._acquire_xsrf_token("dtg")  # noqa: SLF001

    assert out == "fresh-token"
    method, url, kwargs = session.calls[1]
    assert method == "POST"
    assert url.endswith(
        "/service/batteryConfig/api/v1/battery/sites/SITE/schedules/isValid"
    )
    assert kwargs["json"] == {"scheduleType": "dtg"}
    assert kwargs["headers"]["Username"] == "88"
    assert kwargs["headers"]["X-XSRF-Token"] == "stale-token"
    assert kwargs["headers"]["e-auth-token"] == token
    assert "Authorization" not in kwargs["headers"]
    assert "requestid" in kwargs["headers"]
    assert "Cookie" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_accept_battery_settings_disclaimer_uses_battery_config_write_request() -> (
    None
):
    client = _make_client()
    client._battery_config_write_request = AsyncMock(
        return_value={"status": "ok"}
    )  # noqa: SLF001

    out = await client.accept_battery_settings_disclaimer("custom")

    assert out == {"status": "ok"}
    client._battery_config_write_request.assert_awaited_once_with(  # noqa: SLF001
        "POST",
        f"{api.BASE_URL}/service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/SITE",
        json_body={"disclaimer-type": "custom"},
        params=None,
        endpoint_family="battery_settings_disclaimer",
        write_intent="battery_settings_disclaimer_accept",
    )


@pytest.mark.asyncio
async def test_validate_battery_schedule_cfg_payload_keeps_force_schedule_opted() -> (
    None
):
    client = _make_client()
    client._bp_xsrf_token = "xsrf=token"  # noqa: SLF001
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf=token")  # noqa: SLF001
    client._json = AsyncMock(return_value={"isValid": True})

    out = await client.validate_battery_schedule("CFG")

    assert out == {"isValid": True}
    client._acquire_xsrf_token.assert_awaited_once_with(  # noqa: SLF001
        "CFG", variant=api._BATTERY_CONFIG_VARIANT_PRIMARY
    )
    assert client._json.await_args.kwargs["headers"]["X-XSRF-Token"] == "xsrf=token"
    assert client._json.await_args.kwargs["json"] == {
        "scheduleType": "cfg",
        "forceScheduleOpted": True,
    }


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_official_web_headers() -> None:
    token = _make_token({"user_id": "88"})
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=fresh-token; Path=/; Secure")]
    )
    session = _FakeSession([bootstrap, response])
    client = _make_client(session)
    client.update_credentials(eauth=token, cookie="session=1")

    await client._acquire_xsrf_token()  # noqa: SLF001

    headers = session.calls[1][2]["headers"]
    assert "Authorization" not in headers
    assert headers["e-auth-token"] == token
    assert headers["Username"] == "88"
    assert headers["Origin"] == "https://battery-profile-ui.enphaseenergy.com"
    assert headers["Referer"] == "https://battery-profile-ui.enphaseenergy.com/"
    assert "Cookie" not in headers
    assert "requestid" in headers


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_getall_fallback_and_handles_bad_cookie() -> None:
    token = _make_token({"user_id": "88"})

    class _BadStringCookie:
        def __bool__(self) -> bool:
            return True

        def __str__(self) -> str:
            raise RuntimeError("boom")

    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict(
        [
            ("Set-Cookie", "other=1; Path=/"),
            ("Set-Cookie", "BP-XSRF-Token=fallback-token; Path=/; Secure"),
        ]
    )
    session = _FakeSession([bootstrap, response])
    client = _make_client(session)
    client.update_credentials(eauth=token, cookie="session=1")
    client._cookie = _BadStringCookie()  # noqa: SLF001

    out = await client._acquire_xsrf_token()  # noqa: SLF001

    assert out == "fallback-token"
    assert "Cookie" not in session.calls[1][2]["headers"]


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_plain_header_get_fallback() -> None:
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = {}
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = {"Set-Cookie": "BP-XSRF-Token=plain-header-token; Path=/;"}
    session = _FakeSession([bootstrap, response])
    client = _make_client(session)

    out = await client._acquire_xsrf_token()  # noqa: SLF001

    assert out == "plain-header-token"


@pytest.mark.asyncio
async def test_acquire_xsrf_token_returns_none_when_cookie_missing() -> None:
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict()
    session = _FakeSession([bootstrap, response])
    client = _make_client(session)

    assert await client._acquire_xsrf_token() is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_acquire_xsrf_token_returns_none_when_request_raises(caplog) -> None:
    class _BoomSession(_DefaultSession):
        def request(self, method: str, url: str, **kwargs):
            raise RuntimeError("boom")

    client = _make_client(_BoomSession())

    with caplog.at_level(logging.WARNING):
        assert await client._acquire_xsrf_token() is None  # noqa: SLF001

    assert "Failed to acquire XSRF token" in caplog.text


@pytest.mark.asyncio
async def test_acquire_xsrf_token_returns_none_for_empty_decoded_cookie() -> None:
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=fresh-token; Path=/; Secure")]
    )
    session = _FakeSession([bootstrap, response])
    client = _make_client(session)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(api, "unquote", lambda _value: "")
        assert await client._acquire_xsrf_token() is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_response_cookies_when_header_missing() -> None:
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict()
    response.cookies = {"bp-xsrf-token": SimpleNamespace(value="fresh-token")}
    session = _FakeSession([bootstrap, response])
    client = _make_client(session)

    assert await client._acquire_xsrf_token() == "fresh-token"  # noqa: SLF001


@pytest.mark.asyncio
async def test_acquire_xsrf_token_falls_back_to_session_cookie_jar() -> None:
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict()
    session = _FakeSession([bootstrap, response])
    request_url = URL(
        f"{api.BASE_URL}/service/batteryConfig/api/v1/battery/sites/SITE/schedules/isValid"
    )
    session.cookie_jar.update_cookies(
        {"bp-xsrf-token": "jar-token"},
        response_url=request_url,
    )
    client = _make_client(session)
    client.update_credentials(
        cookie="session=1; other=1; enlighten_manager_token_production=bearer"
    )

    assert await client._acquire_xsrf_token() == "jar-token"  # noqa: SLF001
    assert (
        client._cookie
        == "session=1; other=1; enlighten_manager_token_production=bearer"
    )  # noqa: SLF001
    assert client._battery_config_headers(include_xsrf=True)["Cookie"] is None


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_get_bootstrap_response_header() -> None:
    """GET siteSettings returns x-csrf-token header — the EMEA web UI flow.

    Regression coverage for BatteryConfig 403 loops on sites where the
    legacy POST ``schedules/isValid`` bootstrap is rejected without ever
    issuing a Set-Cookie. See GitHub issue #460.
    """

    token = _make_token({"user_id": "88"})
    bootstrap = _FakeResponse(status=200, json_body={"settings": {}})
    bootstrap.headers = CIMultiDict([("x-csrf-token", "header-token")])
    session = _FakeSession([bootstrap])
    client = _make_client(session)
    client.update_credentials(eauth=token, cookie="session=1")

    out = await client._acquire_xsrf_token()  # noqa: SLF001

    assert out == "header-token"
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url.endswith("/service/batteryConfig/api/v1/siteSettings/SITE")
    assert kwargs.get("params") == {"userId": "88"}


@pytest.mark.asyncio
async def test_acquire_xsrf_token_get_bootstrap_trims_header_whitespace() -> None:
    token = _make_token({"user_id": "88"})
    bootstrap = _FakeResponse(status=200, json_body={})
    bootstrap.headers = CIMultiDict([("X-CSRF-Token", "  padded-token  ")])
    session = _FakeSession([bootstrap])
    client = _make_client(session)
    client.update_credentials(eauth=token, cookie="session=1")

    assert await client._acquire_xsrf_token() == "padded-token"  # noqa: SLF001


@pytest.mark.asyncio
async def test_acquire_xsrf_token_get_bootstrap_falls_back_to_post_on_failure() -> None:
    """When the GET bootstrap errors, the legacy POST path still runs."""

    token = _make_token({"user_id": "88"})
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    isvalid = _FakeResponse(status=200, json_body={"isValid": True})
    isvalid.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=post-token; Path=/; Secure")]
    )
    session = _FakeSession([bootstrap, isvalid])
    client = _make_client(session)
    client.update_credentials(eauth=token, cookie="session=1")

    out = await client._acquire_xsrf_token()  # noqa: SLF001

    assert out == "post-token"
    assert len(session.calls) == 2
    assert session.calls[0][0] == "GET"
    assert session.calls[1][0] == "POST"


@pytest.mark.asyncio
async def test_acquire_xsrf_token_get_bootstrap_ignores_set_cookie_fallthrough() -> (
    None
):
    """GET bootstrap only honors x-csrf-token header; Set-Cookie is for the POST path.

    This preserves the existing retry semantics in ``_battery_config_write_request``
    which counts each ``_acquire_xsrf_token`` call as one POST validate attempt
    when the GET does not return the header.
    """

    token = _make_token({"user_id": "88"})
    bootstrap = _FakeResponse(status=200, json_body={})
    bootstrap.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=get-cookie-token; Path=/; Secure")]
    )
    isvalid = _FakeResponse(status=200, json_body={"isValid": True})
    isvalid.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=post-cookie-token; Path=/; Secure")]
    )
    session = _FakeSession([bootstrap, isvalid])
    client = _make_client(session)
    client.update_credentials(eauth=token, cookie="session=1")

    out = await client._acquire_xsrf_token()  # noqa: SLF001

    assert out == "post-cookie-token"
    assert len(session.calls) == 2
    assert session.calls[0][0] == "GET"
    assert session.calls[1][0] == "POST"


@pytest.mark.asyncio
async def test_acquire_xsrf_token_get_bootstrap_no_user_id_skips_params() -> None:
    bootstrap = _FakeResponse(status=200, json_body={})
    bootstrap.headers = CIMultiDict([("x-csrf-token", "header-token")])
    session = _FakeSession([bootstrap])
    client = _make_client(session)

    assert await client._acquire_xsrf_token() == "header-token"  # noqa: SLF001
    assert session.calls[0][2].get("params") is None


@pytest.mark.asyncio
async def test_acquire_xsrf_token_post_fallback_reads_response_header() -> None:
    """POST isValid path also accepts x-csrf-token in the response header."""

    token = _make_token({"user_id": "88"})
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    isvalid = _FakeResponse(status=200, json_body={"isValid": True})
    isvalid.headers = CIMultiDict([("x-csrf-token", "post-header-token")])
    session = _FakeSession([bootstrap, isvalid])
    client = _make_client(session)
    client.update_credentials(eauth=token, cookie="session=1")

    out = await client._acquire_xsrf_token()  # noqa: SLF001

    assert out == "post-header-token"
    assert [call[0] for call in session.calls] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_acquire_xsrf_token_get_bootstrap_without_headers_attr() -> None:
    """Defensive: response objects missing a mapping-style ``headers`` attribute."""

    class _HeaderlessResponse:
        def __init__(self) -> None:
            self.status = 200
            self.headers = None
            self.cookies = None

        async def __aenter__(self) -> "_HeaderlessResponse":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def json(self):
            return {}

    isvalid = _FakeResponse(status=200, json_body={"isValid": True})
    isvalid.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=post-token; Path=/; Secure")]
    )

    class _MixedSession:
        def __init__(self) -> None:
            self._responses: list[object] = [_HeaderlessResponse(), isvalid]
            self.calls: list[tuple[str, str, dict]] = []
            self.cookie_jar = aiohttp.CookieJar()

        def request(self, method: str, url: str, **kwargs):
            resp = self._responses.pop(0)
            self.calls.append((method, url, kwargs))
            return resp

    session = _MixedSession()
    client = _make_client(session)

    out = await client._acquire_xsrf_token()  # noqa: SLF001

    assert out == "post-token"


@pytest.mark.asyncio
async def test_acquire_xsrf_token_post_fallback_tolerates_unquote_errors(
    monkeypatch,
) -> None:
    """The POST Set-Cookie parser tolerates ``unquote`` raising."""

    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    isvalid = _FakeResponse(status=200, json_body={"isValid": True})
    isvalid.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=raw-token; Path=/; Secure")]
    )
    session = _FakeSession([bootstrap, isvalid])
    client = _make_client(session)

    def _boom(_value: str) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "unquote", _boom)

    assert await client._acquire_xsrf_token() == "raw-token"  # noqa: SLF001


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_session_cookie_after_bootstrap_403() -> None:
    bootstrap = _FakeResponse(status=403, json_body={"error": "Forbidden"})
    bootstrap.headers = CIMultiDict()
    response = _FakeResponse(status=403, json_body={"error": "Forbidden"})
    response.headers = CIMultiDict()
    session = _FakeSession([bootstrap, response])
    request_url = URL(
        f"{api.BASE_URL}/service/batteryConfig/api/v1/battery/sites/SITE/schedules/isValid"
    )
    session.cookie_jar.update_cookies(
        {"bp-xsrf-token": "jar-token"},
        response_url=request_url,
    )

    client = _make_client(session)
    client.update_credentials(
        cookie="session=1; other=1; enlighten_manager_token_production=bearer"
    )
    client._bp_xsrf_token = "existing-token"  # noqa: SLF001

    assert await client._acquire_xsrf_token() == "jar-token"  # noqa: SLF001
    assert client._bp_xsrf_token == "jar-token"  # noqa: SLF001
    assert client._xsrf_token() == "jar-token"  # noqa: SLF001


@pytest.mark.asyncio
async def test_acquire_xsrf_token_sends_existing_token_on_validation() -> None:
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    response = _FakeResponse(status=200, json_body={"isValid": True})
    response.headers = CIMultiDict(
        [("Set-Cookie", "BP-XSRF-Token=fresh-token; Path=/; Secure")]
    )
    session = _FakeSession([bootstrap, response])
    client = _make_client(session)
    client.update_credentials(
        cookie=(
            "session=1; BP-XSRF-Token=stale-token; other=1; "
            "enlighten_manager_token_production=bearer"
        )
    )
    client._bp_xsrf_token = "existing-token"  # noqa: SLF001

    assert await client._acquire_xsrf_token() == "fresh-token"  # noqa: SLF001
    assert session.calls[1][2]["headers"]["X-XSRF-Token"] == "existing-token"


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_session_cookie_after_validation_403() -> None:
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    validation = _FakeResponse(status=403, json_body={"error": "Forbidden"})
    validation.headers = CIMultiDict()
    session = _FakeSession([bootstrap, validation])
    request_url = URL(
        f"{api.BASE_URL}/service/batteryConfig/api/v1/battery/sites/SITE/schedules/isValid"
    )
    session.cookie_jar.update_cookies(
        {"bp-xsrf-token": "jar-token"},
        response_url=request_url,
    )
    client = _make_client(session)
    client.update_credentials(
        cookie="session=1; enlighten_manager_token_production=bearer"
    )

    assert await client._acquire_xsrf_token() == "jar-token"  # noqa: SLF001
    assert [call[0] for call in session.calls] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_acquire_xsrf_token_bootstraps_from_site_settings_cookie() -> None:
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    validation = _FakeResponse(status=403, json_body={"error": "Forbidden"})
    validation.headers = CIMultiDict()
    site_settings = _FakeResponse(status=200, json_body={"data": {}})
    site_settings.headers = CIMultiDict()
    site_settings.cookies = SimpleCookie("BP-XSRF-Token=site-token")
    session = _FakeSession([bootstrap, validation, site_settings])
    client = _make_client(session)
    client.update_credentials(
        cookie="session=1; enlighten_manager_token_production=bearer"
    )

    assert await client._acquire_xsrf_token() == "site-token"  # noqa: SLF001
    assert client._bp_xsrf_token == "site-token"  # noqa: SLF001
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]
    method, url, kwargs = session.calls[2]
    assert method == "GET"
    assert "/service/batteryConfig/api/v1/siteSettings/SITE" in url
    assert kwargs["params"] == {}
    assert client._xsrf_token() == "site-token"  # noqa: SLF001


@pytest.mark.asyncio
async def test_acquire_xsrf_token_bootstraps_from_site_settings_cookie_jar() -> None:
    class _DeferredJar:
        active = False

        def update_cookies(self, *_args, **_kwargs) -> None:
            return None

        def filter_cookies(self, _url):
            if not self.active:
                return SimpleCookie()
            return SimpleCookie("BP-XSRF-Token=jar-site-token")

    class _JarUpdatingSession(_FakeSession):
        def request(self, method: str, url: str, **kwargs):
            response = super().request(method, url, **kwargs)
            if method == "GET" and "/siteSettings/" in url and len(self.calls) == 3:
                self.cookie_jar.active = True
            return response

    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    validation = _FakeResponse(status=403, json_body={"error": "Forbidden"})
    validation.headers = CIMultiDict()
    site_settings = _FakeResponse(status=200, json_body={"data": {}})
    site_settings.headers = CIMultiDict()
    session = _JarUpdatingSession([bootstrap, validation, site_settings])
    session.cookie_jar = _DeferredJar()
    client = _make_client(session)
    client.update_credentials(
        cookie="session=1; enlighten_manager_token_production=bearer"
    )

    assert await client._acquire_xsrf_token() == "jar-site-token"  # noqa: SLF001
    assert [call[0] for call in session.calls] == ["GET", "POST", "GET"]


@pytest.mark.asyncio
async def test_acquire_xsrf_token_returns_none_when_validation_and_site_settings_403(
    caplog,
) -> None:
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    validation = _FakeResponse(status=403, json_body={"error": "Forbidden"})
    validation.headers = CIMultiDict()
    site_settings = _FakeResponse(status=403, json_body={"error": "Forbidden"})
    site_settings.headers = CIMultiDict()
    session = _FakeSession([bootstrap, validation, site_settings])
    client = _make_client(session)
    client.update_credentials(
        cookie="session=1; enlighten_manager_token_production=bearer"
    )

    with caplog.at_level(logging.DEBUG):
        assert await client._acquire_xsrf_token() is None  # noqa: SLF001

    assert "siteSettings XSRF bootstrap returned 403" in caplog.text
    assert "BatteryConfig bootstrap returned 403" in caplog.text


@pytest.mark.asyncio
async def test_acquire_xsrf_token_uses_validation_error_response_cookie() -> None:
    bootstrap = _FakeResponse(status=500, json_body={})
    bootstrap.headers = CIMultiDict()
    validation = _FakeResponse(status=403, json_body={"error": "Forbidden"})
    validation.headers = CIMultiDict()
    validation.cookies = SimpleCookie("BP-XSRF-Token=error-token")
    session = _FakeSession([bootstrap, validation])
    client = _make_client(session)
    client.update_credentials(
        cookie="session=1; enlighten_manager_token_production=bearer"
    )

    assert await client._acquire_xsrf_token() == "error-token"  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_schedule_crud_methods_build_requests() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"message": "success"})

    async def _acquire(*_args: object, **_kwargs: object) -> str:
        client._bp_xsrf_token = "fresh-token"  # noqa: SLF001
        return "fresh-token"

    client._acquire_xsrf_token = AsyncMock(side_effect=_acquire)  # noqa: SLF001

    await client.create_battery_schedule(
        schedule_type="cfg",
        start_time="22:30:59",
        end_time="06:45:00",
        limit=95,
        days=["1", 7],
        timezone="Europe/Lisbon",
    )
    client._bp_xsrf_token = None  # noqa: SLF001
    await client.delete_battery_schedule("sched-1", schedule_type="rbd")
    await client.validate_battery_schedule("dtg")

    create_call, delete_call, validate_call = client._json.await_args_list
    assert create_call.args == (
        "POST",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/battery/sites/SITE/schedules",
    )
    assert create_call.kwargs["json"] == {
        "timezone": "Europe/Lisbon",
        "startTime": "22:30",
        "endTime": "06:45",
        "limit": 95,
        "scheduleType": "CFG",
        "days": [1, 7],
    }
    assert client._acquire_xsrf_token.await_args_list[0].args == ("cfg",)
    assert client._acquire_xsrf_token.await_args_list[0].kwargs == {
        "variant": api._BATTERY_CONFIG_VARIANT_PRIMARY
    }
    assert delete_call.args == (
        "POST",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/battery/sites/SITE/schedules/sched-1/delete",
    )
    assert delete_call.kwargs["json"] == {}
    assert client._acquire_xsrf_token.await_args_list[1].args == ("rbd",)
    assert client._acquire_xsrf_token.await_args_list[1].kwargs == {
        "variant": api._BATTERY_CONFIG_VARIANT_PRIMARY
    }
    assert validate_call.args == (
        "POST",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/battery/sites/SITE/schedules/isValid",
    )
    assert validate_call.kwargs["json"] == {"scheduleType": "dtg"}
    assert validate_call.kwargs["headers"]["X-XSRF-Token"] == "fresh-token"
    assert client._acquire_xsrf_token.await_args_list[2].args == ("dtg",)
    assert client._acquire_xsrf_token.await_args_list[2].kwargs == {
        "variant": api._BATTERY_CONFIG_VARIANT_PRIMARY
    }
    assert client._acquire_xsrf_token.await_count == 3


@pytest.mark.asyncio
async def test_create_battery_schedule_omits_optional_limit_and_sets_is_enabled() -> (
    None
):
    client = _make_client()
    client._json = AsyncMock(return_value={"message": "success"})

    async def _acquire(*_args: object, **_kwargs: object) -> str:
        client._bp_xsrf_token = "fresh-token"  # noqa: SLF001
        return "fresh-token"

    client._acquire_xsrf_token = AsyncMock(side_effect=_acquire)  # noqa: SLF001

    await client.create_battery_schedule(
        schedule_type="rbd",
        start_time="01:00",
        end_time="16:00",
        limit=None,
        days=[1, 2, 3],
        timezone="Europe/London",
        is_enabled=True,
    )

    call = client._json.await_args
    client._acquire_xsrf_token.assert_awaited_once_with(
        "rbd", variant=api._BATTERY_CONFIG_VARIANT_PRIMARY
    )
    assert call.kwargs["json"] == {
        "timezone": "Europe/London",
        "startTime": "01:00",
        "endTime": "16:00",
        "scheduleType": "RBD",
        "days": [1, 2, 3],
        "isEnabled": True,
    }


@pytest.mark.asyncio
async def test_update_battery_schedule_sets_optional_is_enabled_and_is_deleted() -> (
    None
):
    client = _make_client()
    client._json = AsyncMock(return_value={"message": "success"})

    async def _acquire(*_args: object, **_kwargs: object) -> str:
        client._bp_xsrf_token = "fresh-token"  # noqa: SLF001
        return "fresh-token"

    client._acquire_xsrf_token = AsyncMock(side_effect=_acquire)  # noqa: SLF001

    await client.update_battery_schedule(
        "sched-rbd",
        schedule_type="rbd",
        start_time="01:00",
        end_time="16:00",
        limit=None,
        days=[1, 2, 3],
        timezone="Europe/London",
        is_enabled=False,
        is_deleted=True,
    )

    call = client._json.await_args
    client._acquire_xsrf_token.assert_awaited_once_with(
        "rbd", variant=api._BATTERY_CONFIG_VARIANT_PRIMARY
    )
    assert call.kwargs["json"] == {
        "timezone": "Europe/London",
        "startTime": "01:00",
        "endTime": "16:00",
        "scheduleType": "RBD",
        "days": [1, 2, 3],
        "isEnabled": False,
        "isDeleted": True,
    }


@pytest.mark.asyncio
async def test_update_battery_schedule_builds_request_and_clears_xsrf() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"message": "success"})

    async def _acquire(*_args: object, **_kwargs: object) -> str:
        client._bp_xsrf_token = "fresh-token"  # noqa: SLF001
        return "fresh-token"

    client._acquire_xsrf_token = AsyncMock(side_effect=_acquire)  # noqa: SLF001

    out = await client.update_battery_schedule(
        "sched-1",
        schedule_type="dtg",
        start_time="07:15:59",
        end_time="09:45:00",
        limit="80",
        days=["2", 6],
        timezone="Australia/Melbourne",
    )

    assert out == {"message": "success"}
    client._acquire_xsrf_token.assert_awaited_once_with(
        "dtg", variant=api._BATTERY_CONFIG_VARIANT_PRIMARY
    )
    args, kwargs = client._json.await_args
    assert args == (
        "PUT",
        "https://enlighten.enphaseenergy.com/service/batteryConfig/api/v1/battery/sites/SITE/schedules/sched-1",
    )
    assert kwargs["json"] == {
        "timezone": "Australia/Melbourne",
        "startTime": "07:15",
        "endTime": "09:45",
        "limit": 80,
        "scheduleType": "DTG",
        "days": [2, 6],
    }
    assert kwargs["headers"]["Authorization"] is None
    assert kwargs["headers"]["e-auth-token"] == "EAUTH"
    assert "requestid" in kwargs["headers"]
    assert kwargs["headers"]["X-CSRF-Token"] is None
    assert kwargs["headers"]["X-XSRF-Token"] == "fresh-token"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["Origin"] == "https://battery-profile-ui.enphaseenergy.com"
    assert (
        kwargs["headers"]["Referer"] == "https://battery-profile-ui.enphaseenergy.com/"
    )
    assert kwargs["headers"]["Cookie"] is None
    assert client._bp_xsrf_token is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_set_battery_settings_reacquires_xsrf_for_each_write() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"message": "success"})
    call_number = 0

    async def _acquire(
        schedule_type: str = "cfg",
        *,
        variant: str = api._BATTERY_CONFIG_VARIANT_PRIMARY,
    ) -> str:
        nonlocal call_number
        assert variant == api._BATTERY_CONFIG_VARIANT_PRIMARY
        call_number += 1
        client._bp_xsrf_token = (  # noqa: SLF001
            f"{schedule_type}-fresh-token-{call_number}"
        )
        return client._bp_xsrf_token  # noqa: SLF001

    client._acquire_xsrf_token = AsyncMock(side_effect=_acquire)  # noqa: SLF001

    await client.set_battery_settings({"veryLowSoc": 15})
    await client.set_battery_settings({"veryLowSoc": 20})

    first_call, second_call = client._json.await_args_list
    assert first_call.kwargs["headers"]["X-XSRF-Token"] == "cfg-fresh-token-1"
    assert second_call.kwargs["headers"]["X-XSRF-Token"] == "cfg-fresh-token-2"
    assert "requestid" in first_call.kwargs["headers"]
    assert "requestid" in second_call.kwargs["headers"]
    assert client._acquire_xsrf_token.await_count == 2
    assert client._acquire_xsrf_token.await_args_list[0].args == ("cfg",)
    assert client._acquire_xsrf_token.await_args_list[0].kwargs == {
        "variant": api._BATTERY_CONFIG_VARIANT_PRIMARY
    }
    assert client._acquire_xsrf_token.await_args_list[1].args == ("cfg",)
    assert client._acquire_xsrf_token.await_args_list[1].kwargs == {
        "variant": api._BATTERY_CONFIG_VARIANT_PRIMARY
    }
    assert client._bp_xsrf_token is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_storm_guard_profile_delegates_to_battery_profile_details() -> None:
    client = _make_client()
    client.battery_profile_details = AsyncMock(return_value={"data": {"ok": True}})

    out = await client.storm_guard_profile(locale="en-US")

    assert out == {"data": {"ok": True}}
    client.battery_profile_details.assert_awaited_once_with(locale="en-US")


@pytest.mark.asyncio
async def test_set_battery_profile_reraises_non_403_errors() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500, "boom"))

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client.set_battery_profile(
            profile="self-consumption",
            battery_backup_percentage=20,
        )

    assert err.value.status == 500


@pytest.mark.asyncio
async def test_set_battery_profile_retries_without_devices_then_without_source() -> (
    None
):
    client = _make_client()
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    client._json = AsyncMock(
        side_effect=[
            _make_cre(403, "Forbidden"),
            _make_cre(403, "Forbidden"),
            {"message": "success"},
        ]
    )

    out = await client.set_battery_profile(
        profile="self-consumption",
        battery_backup_percentage=20,
        devices=[{"uuid": "abc", "deviceType": "iqEvse", "enable": False}],
    )

    assert out == {"message": "success"}
    first_call, second_call, third_call = client._json.await_args_list
    assert first_call.kwargs["json"] == {
        "profile": "self-consumption",
        "batteryBackupPercentage": 20,
        "devices": [{"uuid": "abc", "deviceType": "iqEvse", "enable": False}],
    }
    assert second_call.kwargs["json"] == {
        "profile": "self-consumption",
        "batteryBackupPercentage": 20,
    }
    assert second_call.kwargs["params"] == {"source": "enho"}
    assert third_call.kwargs["json"] == {
        "profile": "self-consumption",
        "batteryBackupPercentage": 20,
    }
    assert third_call.kwargs["params"] == {}


@pytest.mark.asyncio
async def test_set_battery_profile_retries_without_devices_after_400() -> None:
    client = _make_client()
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    client._json = AsyncMock(
        side_effect=[
            _make_cre(400, "Bad Request"),
            {"message": "success"},
        ]
    )

    out = await client.set_battery_profile(
        profile="backup_only",
        battery_backup_percentage=100,
        devices=[{"uuid": "abc", "deviceType": "iqEvse", "enable": False}],
    )

    assert out == {"message": "success"}
    first_call, second_call = client._json.await_args_list
    assert first_call.kwargs["json"] == {
        "profile": "backup_only",
        "batteryBackupPercentage": 100,
        "devices": [{"uuid": "abc", "deviceType": "iqEvse", "enable": False}],
    }
    assert second_call.kwargs["json"] == {
        "profile": "backup_only",
        "batteryBackupPercentage": 100,
    }


@pytest.mark.asyncio
async def test_set_battery_settings_retries_disclaimer_as_boolean_true_after_403() -> (
    None
):
    client = _make_client()

    async def _acquire(*_args: object, **_kwargs: object) -> str:
        client._bp_xsrf_token = "xsrf-token"  # noqa: SLF001
        return "xsrf-token"

    client._acquire_xsrf_token = AsyncMock(side_effect=_acquire)  # noqa: SLF001
    client._json = AsyncMock(
        side_effect=[
            _make_cre(403, "Forbidden"),
            _make_cre(403, "Forbidden"),
            _make_cre(403, "Forbidden"),
            _make_cre(403, "Forbidden"),
            {"message": "success"},
        ]
    )

    out = await client.set_battery_settings(
        {
            "chargeFromGrid": True,
            "acceptedItcDisclaimer": "2026-04-17T09:30:00+00:00",
        }
    )

    assert out == {"message": "success"}
    (
        primary_call,
        lean_call,
        cookie_call,
        mixed_call,
        disclaimer_call,
    ) = client._json.await_args_list
    assert primary_call.kwargs["json"]["acceptedItcDisclaimer"] == (
        "2026-04-17T09:30:00+00:00"
    )
    assert lean_call.kwargs["json"]["acceptedItcDisclaimer"] == (
        "2026-04-17T09:30:00+00:00"
    )
    assert cookie_call.kwargs["json"]["acceptedItcDisclaimer"] == (
        "2026-04-17T09:30:00+00:00"
    )
    assert mixed_call.kwargs["json"]["acceptedItcDisclaimer"] == (
        "2026-04-17T09:30:00+00:00"
    )
    assert disclaimer_call.kwargs["json"]["acceptedItcDisclaimer"] is True
    assert disclaimer_call.kwargs["params"] == {"source": "enho"}
    assert disclaimer_call.kwargs["headers"]["Authorization"] == "Bearer EAUTH"
    assert disclaimer_call.kwargs["headers"]["e-auth-token"] == "EAUTH"
    assert disclaimer_call.kwargs["headers"]["X-CSRF-Token"] == "xsrf-token"


@pytest.mark.asyncio
async def test_set_battery_settings_does_not_preflight_disclaimer_accept() -> None:
    client = _make_client()
    client.accept_battery_settings_disclaimer = AsyncMock(  # noqa: SLF001
        return_value={"message": "success"}
    )
    client._battery_config_write_request = AsyncMock(
        return_value={"message": "ok"}
    )  # noqa: SLF001

    out = await client.set_battery_settings(
        {
            "chargeFromGrid": True,
            "acceptedItcDisclaimer": "2026-04-17T09:30:00+00:00",
        }
    )

    assert out == {"message": "ok"}
    client.accept_battery_settings_disclaimer.assert_not_awaited()  # noqa: SLF001
    assert (
        client._battery_config_write_request.await_args.kwargs["json_body"][
            "acceptedItcDisclaimer"
        ]
        == "2026-04-17T09:30:00+00:00"
    )


@pytest.mark.asyncio
async def test_set_battery_profile_reraises_403_without_devices() -> None:
    client = _make_client()
    client._battery_config_write_request = AsyncMock(  # noqa: SLF001
        side_effect=_make_cre(403, "Forbidden")
    )

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client.set_battery_profile(
            profile="self-consumption",
            battery_backup_percentage=20,
        )

    assert err.value.status == 403
    assert client._battery_config_write_request.await_count == 1  # noqa: SLF001


@pytest.mark.asyncio
async def test_set_battery_settings_compat_builds_merged_payload_without_devices() -> (
    None
):
    client = _make_client()
    client._battery_config_merged_write_payload = MagicMock(  # noqa: SLF001
        return_value={"chargeFromGrid": True, "devices": {"iqEvse": {}}}
    )
    client._battery_config_write_request = AsyncMock(  # noqa: SLF001
        return_value={"message": "ok"}
    )

    out = await client.set_battery_settings_compat(
        {"chargeFromGrid": True},
        schedule_type="rbd",
        include_source=False,
        merged_payload=True,
        strip_devices=True,
    )

    assert out == {"message": "ok"}
    client._battery_config_merged_write_payload.assert_called_once_with(
        "battery_settings", {"chargeFromGrid": True}
    )
    assert client._battery_config_write_request.await_args.kwargs["json_body"] == {
        "chargeFromGrid": True
    }
    assert client._battery_config_write_request.await_args.kwargs["params"] == {}
    assert (
        client._battery_config_write_request.await_args.kwargs["strip_devices"] is True
    )


@pytest.mark.asyncio
async def test_battery_config_write_request_strips_devices_from_stateful_attempt() -> (
    None
):
    client = _make_client()
    client._remember_battery_config_write_base(  # noqa: SLF001
        "battery_settings",
        {
            "data": {
                "powerMatchControl": {"show": True, "enabled": False},
                "devices": {"iqEvse": {"enabled": True}},
            }
        },
    )
    client._battery_config_write_attempts = MagicMock(  # noqa: SLF001
        return_value=[
            api._BatteryConfigWriteAttempt(  # noqa: SLF001
                attempt_id="stateful",
                auth_mode=api._BATTERY_CONFIG_VARIANT_PRIMARY,  # noqa: SLF001
                merged_payload=True,
            )
        ]
    )
    client._acquire_xsrf_token = AsyncMock(return_value="token")  # noqa: SLF001
    client._battery_config_attempt_headers = MagicMock(return_value={})  # noqa: SLF001
    client._json = AsyncMock(return_value={"message": "success"})  # noqa: SLF001

    await client._battery_config_write_request(  # noqa: SLF001
        "PUT",
        "https://example.test/batterySettings/SITE",
        json_body={"powerMatchControl": {"enabled": True}},
        endpoint_family="battery_settings",
        write_intent="battery_settings_update",
        strip_devices=True,
    )

    sent = client._json.await_args.kwargs["json"]  # noqa: SLF001
    assert sent == {"powerMatchControl": {"show": True, "enabled": True}}


@pytest.mark.asyncio
async def test_cancel_battery_profile_update_uses_empty_body() -> None:
    token = _make_token({"user_id": "44"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="XSRF-TOKEN=t; other=1")
    client._acquire_xsrf_token = AsyncMock(return_value="t")  # noqa: SLF001
    client._bp_xsrf_token = "t"  # noqa: SLF001
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client.cancel_battery_profile_update()

    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "cancel/profile" in args[1]
    assert kwargs["json"] == {}
    assert kwargs["params"]["userId"] == "44"
    assert kwargs["headers"]["X-XSRF-Token"] == "t"
    assert kwargs["headers"]["X-CSRF-Token"] is None
    assert kwargs["headers"]["e-auth-token"] == token
    assert kwargs["headers"]["Authorization"] is None
    assert "requestid" in kwargs["headers"]


@pytest.mark.asyncio
async def test_set_storm_guard_passes_payload_and_xsrf() -> None:
    token = _make_token({"user_id": "99"})
    client = _make_client()
    client.update_credentials(
        eauth=token,
        cookie="XSRF-TOKEN=xsrf-token; other=1",
    )
    client._acquire_xsrf_token = AsyncMock(return_value="xsrf-token")  # noqa: SLF001
    client._bp_xsrf_token = "xsrf-token"  # noqa: SLF001
    client._json = AsyncMock(return_value={"message": "success"})
    out = await client.set_storm_guard(enabled=True, evse_enabled=False)
    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "stormGuard/toggle" in args[1]
    assert kwargs["json"] == {
        "stormGuardState": "enabled",
        "evseStormEnabled": False,
    }
    assert kwargs["params"]["userId"] == "99"
    assert kwargs["headers"]["X-XSRF-Token"] == "xsrf-token"
    assert kwargs["headers"]["X-CSRF-Token"] is None
    assert kwargs["headers"]["e-auth-token"] == token
    assert kwargs["headers"]["Authorization"] is None
    assert "requestid" in kwargs["headers"]


@pytest.mark.asyncio
async def test_opt_out_storm_alert_passes_payload_and_xsrf() -> None:
    token = _make_token({"user_id": "99"})
    client = _make_client()
    client.update_credentials(
        eauth=token,
        cookie="XSRF-TOKEN=xsrf-token; other=1",
    )
    client._json = AsyncMock(return_value={"message": "success"})

    out = await client.opt_out_storm_alert(alert_id="IDV21037", name="Severe Weather")

    assert out == {"message": "success"}
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "stormGuard/" in args[1]
    assert args[1].endswith("/stormAlert")
    assert kwargs["json"] == {
        "stormAlerts": [
            {"id": "IDV21037", "name": "Severe Weather", "status": "opted-out"}
        ]
    }
    assert kwargs["headers"]["X-XSRF-Token"] == "xsrf-token"
    assert kwargs["headers"]["e-auth-token"] == token
    assert kwargs["headers"]["Cookie"] == "XSRF-TOKEN=xsrf-token; other=1"
    assert kwargs["headers"]["requestid"] is None
    assert kwargs["use_cookie_header_only"] is True
    assert kwargs["debug_battery_attempt_id"] == "storm_alert_cookie_eauth"
    assert kwargs["params"] is None


@pytest.mark.asyncio
async def test_opt_out_storm_alert_handles_missing_xsrf() -> None:
    client = _make_client()
    client.update_credentials(cookie="cookie=1")
    client._acquire_xsrf_token = AsyncMock(return_value=None)  # noqa: SLF001
    client._json = AsyncMock(return_value={"message": "success"})

    await client.opt_out_storm_alert(alert_id="IDV21037", name="Severe Weather")

    _args, kwargs = client._json.await_args
    assert kwargs["headers"].get("X-XSRF-Token") is None
    assert kwargs["headers"]["requestid"]


@pytest.mark.asyncio
async def test_opt_out_storm_alert_retries_403_with_lean_auth() -> None:
    token = _make_token({"user_id": "99"})
    client = _make_client()
    client.update_credentials(eauth=token, cookie="other=1")

    async def _acquire(*_args, **_kwargs):
        client._bp_xsrf_token = "fresh-xsrf"  # noqa: SLF001
        return "fresh-xsrf"

    client._acquire_xsrf_token = AsyncMock(side_effect=_acquire)  # noqa: SLF001
    client._json = AsyncMock(
        side_effect=[_make_cre(403, "Forbidden"), {"message": "success"}]
    )

    out = await client.opt_out_storm_alert(
        alert_id="IDV21037",
        name="Severe Weather",
    )

    assert out == {"message": "success"}
    assert client._json.await_count == 2
    primary, lean = client._json.await_args_list
    assert primary.kwargs["debug_battery_attempt_id"] == "storm_alert_primary"
    assert lean.kwargs["debug_battery_attempt_id"] == "storm_alert_lean"
    assert primary.kwargs["headers"]["X-XSRF-Token"] == "fresh-xsrf"
    assert lean.kwargs["headers"]["X-XSRF-Token"] == "fresh-xsrf"
    assert primary.kwargs["headers"]["e-auth-token"] == token
    assert lean.kwargs["headers"]["e-auth-token"] is None
    assert client._acquire_xsrf_token.await_count == 2  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_config_prefers_cookie_bearer_when_it_has_user_id() -> None:
    eauth_token = _make_token({"user_id": "99"})
    cookie_token = _make_token({"user_id": "123"})
    client = _make_client()
    client.update_credentials(
        eauth=eauth_token,
        cookie=(
            "enlighten_manager_token_production="
            f"{cookie_token}; XSRF-TOKEN=token; other=1"
        ),
    )
    client._acquire_xsrf_token = AsyncMock(return_value="token")  # noqa: SLF001
    client._bp_xsrf_token = "token"  # noqa: SLF001
    client._json = AsyncMock(return_value={"message": "success"})

    await client.set_storm_guard(enabled=True, evse_enabled=True)

    _args, kwargs = client._json.await_args
    assert kwargs["headers"]["Authorization"] is None
    assert kwargs["headers"]["e-auth-token"] == cookie_token
    assert kwargs["headers"]["Username"] == "123"
    assert "requestid" in kwargs["headers"]
    assert kwargs["params"]["userId"] == "123"


@pytest.mark.asyncio
async def test_set_storm_guard_handles_missing_xsrf() -> None:
    client = _make_client()
    client.update_credentials(cookie="cookie=1")
    client._json = AsyncMock(return_value={"message": "success"})

    await client.set_storm_guard(enabled=False, evse_enabled=True)

    _args, kwargs = client._json.await_args
    assert "X-XSRF-Token" not in kwargs["headers"]
    assert "requestid" in kwargs["headers"]


@pytest.mark.asyncio
async def test_set_storm_guard_uses_bp_xsrf_cookie_fallback() -> None:
    client = _make_client()
    client.update_credentials(cookie="BP-XSRF-Token=bp%3Dtoken; other=1")
    client._json = AsyncMock(return_value={"message": "success"})

    await client.set_storm_guard(enabled=False, evse_enabled=True)

    _args, kwargs = client._json.await_args
    assert kwargs["headers"]["X-XSRF-Token"] == "bp=token"
    assert "requestid" in kwargs["headers"]


@pytest.mark.asyncio
async def test_set_storm_guard_handles_bad_cookie() -> None:
    client = _make_client()
    client._cookie = _BadCookie()
    client._json = AsyncMock(return_value={"message": "success"})

    await client.set_storm_guard(enabled=True, evse_enabled=True)

    _args, kwargs = client._json.await_args
    assert "X-XSRF-Token" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_charger_auth_settings_filters_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": [{"key": AUTH_APP_SETTING, "value": "enabled"}, "invalid"]
        }
    )
    settings = await client.charger_auth_settings("SN")
    assert settings == [{"key": AUTH_APP_SETTING, "value": "enabled"}]
    args, kwargs = client._json.await_args
    assert args[0] == "POST"
    assert "ev_charger_config" in args[1]
    assert kwargs["json"] == [
        {"key": AUTH_RFID_SETTING},
        {"key": AUTH_APP_SETTING},
    ]
    assert "Authorization" in kwargs["headers"]


@pytest.mark.asyncio
async def test_charger_auth_settings_retries_without_authorization_on_401() -> None:
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(
                status=200,
                json_body={"data": [{"key": AUTH_APP_SETTING, "value": None}]},
            ),
        ]
    )
    client = _make_client(session)

    settings = await client.charger_auth_settings("SN")

    assert settings == [{"key": AUTH_APP_SETTING, "value": None}]
    assert len(session.calls) == 2
    first_headers = session.calls[0][2]["headers"]
    second_headers = session.calls[1][2]["headers"]
    assert "Authorization" in first_headers
    assert "e-auth-token" in first_headers
    assert first_headers["Accept"] == "application/json, text/javascript, */*; q=0.01"
    assert "Authorization" not in second_headers
    assert "e-auth-token" not in second_headers
    assert second_headers["Accept"] == "application/json, text/javascript, */*; q=0.01"


@pytest.mark.asyncio
async def test_charger_auth_settings_handles_non_dict_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])
    assert await client.charger_auth_settings("SN") == []


@pytest.mark.asyncio
async def test_charger_auth_settings_handles_non_list_data() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": "nope"})
    assert await client.charger_auth_settings("SN") == []


@pytest.mark.asyncio
async def test_charger_config_filters_payload_and_passes_requested_keys() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": [
                {"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": None},
                {"key": PHASE_SWITCH_CONFIG_SETTING, "value": "auto"},
                "invalid",
            ]
        }
    )

    settings = await client.charger_config(
        "SN",
        [DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING],
    )

    assert settings == [
        {"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": None},
        {"key": PHASE_SWITCH_CONFIG_SETTING, "value": "auto"},
    ]
    args, kwargs = client._json.await_args
    assert args[0] == "POST"
    assert "ev_charger_config" in args[1]
    assert kwargs["json"] == [
        {"key": DEFAULT_CHARGE_LEVEL_SETTING},
        {"key": PHASE_SWITCH_CONFIG_SETTING},
    ]
    expected_headers = client._today_json_headers()
    expected_headers.update(client._control_headers())
    assert kwargs["headers"] == expected_headers


@pytest.mark.asyncio
async def test_charger_config_normalizes_requested_keys() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": []})

    class _BadKey:
        def __str__(self) -> str:
            raise ValueError("boom")

    await client.charger_config(
        "SN",
        ["", DEFAULT_CHARGE_LEVEL_SETTING, DEFAULT_CHARGE_LEVEL_SETTING, _BadKey()],
    )

    _args, kwargs = client._json.await_args
    assert kwargs["json"] == [{"key": DEFAULT_CHARGE_LEVEL_SETTING}]


@pytest.mark.asyncio
async def test_charger_config_returns_empty_when_no_valid_keys() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": []})

    class _BadKey:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert await client.charger_config("SN", ["", _BadKey()]) == []
    client._json.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_default_charge_level_uses_charger_config_put() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": [{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": "30", "status": 2}]
        }
    )

    result = await client.set_default_charge_level("SN", 30)

    assert result == {
        "data": [{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": "30", "status": 2}]
    }
    args, kwargs = client._json.await_args
    assert args[0] == "PUT"
    assert "ev_charger_config" in args[1]
    assert kwargs["json"] == [{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": 30}]
    expected_headers = client._today_json_headers()
    expected_headers.update(client._control_headers())
    assert kwargs["headers"] == expected_headers


@pytest.mark.asyncio
async def test_set_default_charge_level_wraps_config_unavailable() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=_make_cre(503, "ev_charger_config service unavailable")
    )

    with pytest.raises(api.ChargerConfigUnavailable):
        await client.set_default_charge_level("SN", 30)
    client._json.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_default_charge_level_reraises_non_config_error() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(400, "bad request"))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.set_default_charge_level("SN", 30)


@pytest.mark.asyncio
async def test_set_app_authentication_passes_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"status": "ok"})
    out = await client.set_app_authentication("SN", enabled=False)
    assert out == {"status": "ok"}
    args, kwargs = client._json.await_args
    assert kwargs["json"] == [{"key": AUTH_APP_SETTING, "value": "disabled"}]
    expected_headers = client._today_json_headers()
    expected_headers.update(client._control_headers())
    assert kwargs["headers"] == expected_headers


@pytest.mark.asyncio
async def test_lifetime_energy_normalization() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                status=200,
                json_body={
                    "data": {
                        "production": [1000, "2000", None, -5],
                        "import": ["", "30"],
                        "grid_home": [15],
                        "generator_grid": ["12.5"],
                        "update_pending": False,
                        "start_date": "2024-01-01",
                        "last_report_date": "1700000000",
                        "evse": "skip",
                        "heatpump": [None, "4.2", "bad"],
                        "water_heater": [0, "15"],
                        "interval_minutes": "15",
                    }
                },
            )
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", None, "COOKIE")
    payload = await client.lifetime_energy()
    assert payload["production"] == [1000.0, 2000.0, None, -5.0]
    assert payload["import"] == [None, 30.0]
    assert payload["grid_home"] == [15.0]
    assert payload["generator_grid"] == [12.5]
    assert payload["update_pending"] is False
    assert payload["start_date"] == "2024-01-01"
    assert payload["last_report_date"] == "1700000000"
    assert payload["evse"] == []
    assert payload["heatpump"] == [None, 4.2, None]
    assert payload["water_heater"] == [0.0, 15.0]
    assert payload["interval_minutes"] == 15.0
    assert session.calls[0][2]["headers"] == {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{api.BASE_URL}/web/SITE/layout/graph/years",
        "User-Agent": api._ENLIGHTEN_BROWSER_USER_AGENT,
        "Cookie": "COOKIE",
    }


@pytest.mark.asyncio
async def test_latest_power_normalization() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "latest_power": {
                "value": 752,
                "units": "W",
                "precision": 0,
                "time": 1_773_207_600,
            }
        }
    )

    payload = await client.latest_power()

    assert payload == {
        "value": 752.0,
        "units": "W",
        "precision": 0,
        "time": 1_773_207_600,
    }
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/get_latest_power",
        headers=client._history_headers(),
    )


@pytest.mark.asyncio
async def test_latest_power_logs_invalid_payload_shape(caplog) -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "latest_power": {
                "units": "W",
                "time": 1_773_207_600,
            }
        }
    )

    with caplog.at_level(logging.DEBUG):
        payload = await client.latest_power()

    assert payload is None
    assert "Invalid latest power payload for site" in caplog.text
    assert "top_level_keys=['latest_power']" in caplog.text
    assert "nested_keys=['time', 'units']" in caplog.text


@pytest.mark.asyncio
async def test_latest_power_logs_invalid_nested_data_payload_shape(caplog) -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "units": "W",
                "time": 1_773_207_600,
            }
        }
    )

    with caplog.at_level(logging.DEBUG):
        payload = await client.latest_power()

    assert payload is None
    assert "Invalid latest power payload for site" in caplog.text
    assert "top_level_keys=['data']" in caplog.text
    assert "nested_keys=['time', 'units']" in caplog.text


def test_normalize_latest_power_payload_rejects_invalid_shapes() -> None:
    client = _make_client()

    assert client._normalize_latest_power_payload("bad") is None  # noqa: SLF001
    assert client._normalize_latest_power_payload({}) is None  # noqa: SLF001
    assert (
        client._normalize_latest_power_payload(  # noqa: SLF001
            {"latest_power": {"units": "W"}}
        )
        is None
    )
    assert (
        client._normalize_latest_power_payload(  # noqa: SLF001
            {"latest_power": {"value": "bad"}}
        )
        is None
    )
    assert (
        client._normalize_latest_power_payload(  # noqa: SLF001
            {"latest_power": {"value": False}}
        )
        is None
    )
    assert client._normalize_latest_power_payload(  # noqa: SLF001
        {"latest_power": {"value": 752, "precision": True, "time": False}}
    ) == {"value": 752.0}

    assert client._normalize_latest_power_payload(  # noqa: SLF001
        {
            "data": {
                "latest_power": {
                    "value": "600.5",
                    "units": "W",
                    "precision": "1",
                    "time": "1773207600000",
                }
            }
        }
    ) == {
        "value": 600.5,
        "units": "W",
        "precision": 1,
        "time": 1_773_207_600,
    }


def test_normalize_latest_power_payload_handles_unstringable_units_and_nan_metadata() -> (
    None
):
    client = _make_client()

    class BadString:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert client._normalize_latest_power_payload(  # noqa: SLF001
        {
            "latest_power": {
                "value": 752,
                "units": BadString(),
                "precision": "nan",
                "time": "nan",
            }
        }
    ) == {"value": 752.0}


@pytest.mark.asyncio
async def test_evse_timeseries_daily_energy_normalization() -> None:
    client = _make_client()
    client.update_credentials(eauth=_make_token({"user_id": "user-123"}))
    client._json = AsyncMock(
        return_value={
            "data": {
                TEST_EVSE_SERIAL: {
                    "days": [
                        {"date": "2026-03-10", "energy_wh": 1200},
                        {"date": "2026-03-11", "energy_kwh": "2.5"},
                    ],
                    "intervalMinutes": "1440",
                    "lastReportDate": "2026-03-11T10:00:00+00:00",
                },
                "EVSE-2": {
                    "2026-03-11": "3.1",
                },
            }
        }
    )

    payload = await client.evse_timeseries_daily_energy(
        start_date=datetime.date(2026, 3, 11)
    )

    assert payload[TEST_EVSE_SERIAL]["day_values_kwh"] == {
        "2026-03-10": pytest.approx(1.2),
        "2026-03-11": pytest.approx(2.5),
    }
    assert payload[TEST_EVSE_SERIAL]["energy_kwh"] == pytest.approx(2.5)
    assert payload[TEST_EVSE_SERIAL]["interval_minutes"] == pytest.approx(1440.0)
    assert payload["EVSE-2"]["energy_kwh"] == pytest.approx(3.1)
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "/service/timeseries/evse/timeseries/daily_energy" in args[1]
    assert "site_id=SITE" in args[1]
    assert "start_date=2026-03-11" in args[1]
    assert kwargs["headers"]["username"] == "user-123"


@pytest.mark.asyncio
async def test_evse_timeseries_lifetime_energy_normalization() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": [
                {
                    "serial": TEST_EVSE_SERIAL,
                    "lifetime_energy_wh": 45600,
                    "interval": "60",
                    "last_report_date": 1_700_000_000,
                },
                {
                    "serial_number": "EVSE-2",
                    "values": [{"value_kwh": "12.4"}],
                },
            ]
        }
    )

    payload = await client.evse_timeseries_lifetime_energy()

    assert payload[TEST_EVSE_SERIAL]["energy_kwh"] == pytest.approx(45.6)
    assert payload[TEST_EVSE_SERIAL]["interval_minutes"] == pytest.approx(60.0)
    assert payload["EVSE-2"]["energy_kwh"] == pytest.approx(12.4)
    args, _kwargs = client._json.await_args
    assert "site_id=SITE" in args[1]


@pytest.mark.asyncio
async def test_evse_timeseries_wraps_unavailable() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(503, "service unavailable"))

    with pytest.raises(api.EVSETimeseriesUnavailable):
        await client.evse_timeseries_daily_energy()

    with pytest.raises(api.EVSETimeseriesUnavailable):
        await client.evse_timeseries_lifetime_energy()


@pytest.mark.asyncio
async def test_evse_timeseries_methods_handle_username_and_reraise() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=None)

    assert await client.evse_timeseries_lifetime_energy(username="user-1") is None
    args, _kwargs = client._json.await_args
    assert "username=user-1" in args[1]

    client._json = AsyncMock(side_effect=_make_cre(400, "bad request"))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.evse_timeseries_daily_energy()

    client._json = AsyncMock(side_effect=_make_cre(400, "bad request"))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.evse_timeseries_lifetime_energy()


@pytest.mark.asyncio
async def test_evse_timeseries_daily_energy_defaults_start_date(monkeypatch) -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=None)
    fixed_now = datetime.datetime(2026, 3, 12, 4, 5, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(api, "datetime", MagicMock(wraps=datetime.datetime))
    api.datetime.now.return_value = fixed_now

    assert await client.evse_timeseries_daily_energy() is None

    args, _kwargs = client._json.await_args
    assert "start_date=2026-03-12" in args[1]


@pytest.mark.asyncio
async def test_lifetime_energy_handles_non_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["not-a-dict"])
    assert await client.lifetime_energy() is None


@pytest.mark.asyncio
async def test_lifetime_energy_coerce_errors() -> None:
    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "production": [BadFloat(), "bad-number"],
        }
    )
    payload = await client.lifetime_energy()
    assert payload["production"] == [None, None]


@pytest.mark.asyncio
async def test_lifetime_energy_coerce_bad_number_subclass() -> None:
    class BadFloat(float):
        def __float__(self):
            raise ValueError("bad")

    client = _make_client()
    client._json = AsyncMock(return_value={"production": [BadFloat(1.0)]})
    payload = await client.lifetime_energy()
    assert payload["production"] == [None]


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_normalization() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "production": [100, "200"],
                "evse": [None, "3.2"],
                "heatpump": [0, "8.5", "bad"],
                "water_heater": "skip",
                "start_date": "2024-01-01",
                "last_report_date": 1700000000,
                "update_pending": False,
                "interval": "30",
            }
        }
    )

    payload = await client.hems_consumption_lifetime()

    assert payload["production"] == [100.0, 200.0]
    assert payload["evse"] == [None, 3.2]
    assert payload["heatpump"] == [0.0, 8.5, None]
    assert payload["water_heater"] == []
    assert payload["start_date"] == "2024-01-01"
    assert payload["last_report_date"] == 1700000000
    assert payload["update_pending"] is False
    assert payload["interval_minutes"] == 30.0


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_uses_systems_json_headers() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR; XSRF-TOKEN=xsrf",
        eauth="EAUTH",
    )
    client._json = AsyncMock(return_value={"heatpump": []})

    await client.hems_consumption_lifetime()

    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert args[1].endswith("/systems/SITE/hems_consumption_lifetime")
    assert kwargs["log_invalid_payload"] is False
    assert kwargs["headers"] == client._systems_json_headers()


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_unauthorized_reraises() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=api.Unauthorized())

    with pytest.raises(api.Unauthorized):
        await client.hems_consumption_lifetime()


@pytest.mark.asyncio
async def test_hems_heatpump_state_normalization_and_headers() -> None:
    client = _make_client()
    cookie_token = _make_token({"user_id": "user-123"})
    client.update_credentials(
        cookie=f"enlighten_manager_token_production={cookie_token}; XSRF-TOKEN=xsrf",
        eauth="EAUTH",
    )
    client._json = AsyncMock(
        return_value={
            "type": "hems-heatpump-details",
            "timestamp": "2026-03-20T08:19:17.945447902Z",
            "data": {
                "device-uid": "HP-1",
                "heatpump-status": "RUNNING",
                "sg-ready-mode": "MODE_3",
                "vpp-sgready-mode-override": "NONE",
                "last-report-at": "2026-03-20T08:18:59.604Z",
            },
        }
    )

    payload = await client.hems_heatpump_state("HP-1", timezone="Europe/Berlin")

    assert payload == {
        "type": "hems-heatpump-details",
        "timestamp": "2026-03-20T08:19:17.945447902Z",
        "endpoint_type": "hems-heatpump-details",
        "endpoint_timestamp": "2026-03-20T08:19:17.945447902Z",
        "device_uid": "HP-1",
        "heatpump_status": "RUNNING",
        "sg_ready_mode_raw": "MODE_3",
        "sg_ready_mode_label": "Recommended",
        "sg_ready_active": True,
        "sg_ready_contact_state": "closed",
        "vpp_sgready_mode_override": "NONE",
        "last_report_at": "2026-03-20T08:18:59.604Z",
    }
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert (
        args[1]
        == "https://hems-integration.enphaseenergy.com/api/v1/hems/SITE/heatpump/HP-1/state?timezone=Europe/Berlin"
    )
    assert callable(kwargs["headers"])
    headers = kwargs["headers"]()
    assert headers["Authorization"] == f"Bearer {cookie_token}"
    assert headers["e-auth-token"] == "EAUTH"
    assert headers["X-CSRF-Token"] == "xsrf"
    assert headers["username"] == "user-123"
    assert headers["requestId"]


def test_hems_heatpump_state_normalizers_cover_edge_cases() -> None:
    class BadString:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert api.EnphaseEVClient._clean_optional_text(None) is None
    assert api.EnphaseEVClient._clean_optional_text(BadString()) is None
    assert api.EnphaseEVClient._heatpump_sg_ready_mode_details(None) == {
        "sg_ready_mode_label": None,
        "sg_ready_active": None,
        "sg_ready_contact_state": None,
    }
    assert api.EnphaseEVClient._heatpump_sg_ready_mode_details("MODE_2") == {
        "sg_ready_mode_label": "Normal",
        "sg_ready_active": False,
        "sg_ready_contact_state": "open",
    }
    assert api.EnphaseEVClient._heatpump_sg_ready_mode_details("MODE_99") == {
        "sg_ready_mode_label": None,
        "sg_ready_active": None,
        "sg_ready_contact_state": None,
    }
    assert api.EnphaseEVClient._normalize_hems_heatpump_state_payload(["bad"]) is None
    assert api.EnphaseEVClient._normalize_hems_heatpump_state_payload(
        {
            "type": "hems-heatpump-details",
            "device-uid": "HP-2",
            "heatpump-status": "IDLE",
            "sg-ready-mode": "MODE_2",
            "vpp-sgready-mode-override": "NONE",
            "last-report-at": "2026-03-20T08:18:59.604Z",
        }
    ) == {
        "type": "hems-heatpump-details",
        "timestamp": None,
        "endpoint_type": "hems-heatpump-details",
        "endpoint_timestamp": None,
        "device_uid": "HP-2",
        "heatpump_status": "IDLE",
        "sg_ready_mode_raw": "MODE_2",
        "sg_ready_mode_label": "Normal",
        "sg_ready_active": False,
        "sg_ready_contact_state": "open",
        "vpp_sgready_mode_override": "NONE",
        "last_report_at": "2026-03-20T08:18:59.604Z",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("side_effect", [_make_cre(404)])
async def test_hems_heatpump_state_optional_failures_return_none(
    monkeypatch, side_effect
) -> None:
    client = _make_client()
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=side_effect))

    assert await client.hems_heatpump_state("HP-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "side_effect", [api.Unauthorized(), _make_cre(401), _make_cre(403)]
)
async def test_hems_heatpump_state_auth_failures_reraise(
    monkeypatch, side_effect
) -> None:
    client = _make_client()
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=side_effect))

    with pytest.raises((api.Unauthorized, aiohttp.ClientResponseError)):
        await client.hems_heatpump_state("HP-1")


@pytest.mark.asyncio
async def test_hems_heatpump_state_optional_invalid_payload_and_invalid_site() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=_make_optional_payload_error(
            "/api/v1/hems/SITE/heatpump/HP-1/state"
        )
    )

    assert await client.hems_heatpump_state("HP-1") is None

    client = _make_client()
    client._json = AsyncMock(
        side_effect=_make_cre(
            550,
            json.dumps({"error": {"status": "INVALID_SITE", "message": "unsupported"}}),
        )
    )

    assert await client.hems_heatpump_state("HP-1") is None
    assert client.hems_site_supported is False


@pytest.mark.asyncio
async def test_hems_heatpump_state_reraises_non_optional_errors() -> None:
    client = _make_client()
    invalid_json = api.InvalidPayloadError(
        "Invalid JSON response",
        status=200,
        content_type="application/json",
        endpoint="/api/v1/hems/SITE/heatpump/HP-1/state",
    )
    client._json = AsyncMock(side_effect=invalid_json)

    with pytest.raises(api.InvalidPayloadError):
        await client.hems_heatpump_state("HP-1")

    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_heatpump_state("HP-1")


@pytest.mark.asyncio
async def test_hems_heatpump_state_skips_blank_device_uid() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=AssertionError("should not fetch"))

    assert await client.hems_heatpump_state("   ") is None
    client._json.assert_not_awaited()


def test_hems_energy_consumption_normalizers_cover_edge_cases() -> None:
    assert api.EnphaseEVClient._normalize_hems_daily_consumption_entry(["bad"]) is None
    assert api.EnphaseEVClient._normalize_hems_daily_consumption_entry(
        {
            "device-uid": "HP-1",
            "device-name": "Waermepumpe",
            "consumption": [
                "skip-me",
                {"solar": "1.0", "details": [2.0, "bad", None]},
            ],
        }
    ) == {
        "device_uid": "HP-1",
        "device_name": "Waermepumpe",
        "consumption": [
            {
                "solar": 1.0,
                "battery": None,
                "grid": None,
                "details": [2.0, None, None],
            }
        ],
    }
    assert (
        api.EnphaseEVClient._normalize_hems_energy_consumption_payload(["bad"]) is None
    )
    assert api.EnphaseEVClient._normalize_hems_energy_consumption_payload(
        {
            "type": "hems-device-details",
            "timestamp": "2026-03-20T07:53:00.739143826Z",
            "heat_pump": [
                {
                    "device_uid": "HP-1",
                    "device_name": "Waermepumpe",
                    "consumption": [{"solar": 1.0, "details": [2.0]}],
                }
            ],
            "evse": "not-a-list",
            "water_heater": [
                {
                    "device_uid": "WH-1",
                    "device_name": "Boiler",
                    "consumption": [{"grid": 3.0, "details": [4.0]}],
                }
            ],
        }
    ) == {
        "type": "hems-device-details",
        "timestamp": "2026-03-20T07:53:00.739143826Z",
        "endpoint_type": "hems-device-details",
        "endpoint_timestamp": "2026-03-20T07:53:00.739143826Z",
        "data": {
            "heat-pump": [
                {
                    "device_uid": "HP-1",
                    "device_name": "Waermepumpe",
                    "consumption": [
                        {
                            "solar": 1.0,
                            "battery": None,
                            "grid": None,
                            "details": [2.0],
                        }
                    ],
                }
            ],
            "evse": [],
            "water-heater": [
                {
                    "device_uid": "WH-1",
                    "device_name": "Boiler",
                    "consumption": [
                        {
                            "solar": None,
                            "battery": None,
                            "grid": 3.0,
                            "details": [4.0],
                        }
                    ],
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_hems_energy_consumption_normalization_and_query() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "type": "hems-device-details",
            "timestamp": "2026-03-20T07:53:00.739143826Z",
            "data": {
                "heat-pump": [
                    {
                        "device-uid": "HP-1",
                        "device-name": "Waermepumpe",
                        "consumption": [
                            {
                                "solar": 1.0,
                                "battery": "2.0",
                                "grid": 3,
                                "details": [47.0, "53.0", "bad"],
                            }
                        ],
                    }
                ],
                "evse": [],
                "water-heater": [
                    {
                        "device-uid": "WH-1",
                        "device-name": "Boiler",
                        "consumption": [{"solar": 4.0, "details": [5.0]}],
                    }
                ],
            },
        }
    )

    payload = await client.hems_energy_consumption(
        start_at="2026-03-20T00:00:00+01:00",
        end_at="2026-03-21T00:00:00+01:00",
        timezone="Europe/Berlin",
    )

    assert payload == {
        "type": "hems-device-details",
        "timestamp": "2026-03-20T07:53:00.739143826Z",
        "endpoint_type": "hems-device-details",
        "endpoint_timestamp": "2026-03-20T07:53:00.739143826Z",
        "data": {
            "heat-pump": [
                {
                    "device_uid": "HP-1",
                    "device_name": "Waermepumpe",
                    "consumption": [
                        {
                            "solar": 1.0,
                            "battery": 2.0,
                            "grid": 3.0,
                            "details": [47.0, 53.0, None],
                        }
                    ],
                }
            ],
            "evse": [],
            "water-heater": [
                {
                    "device_uid": "WH-1",
                    "device_name": "Boiler",
                    "consumption": [
                        {
                            "solar": 4.0,
                            "battery": None,
                            "grid": None,
                            "details": [5.0],
                        }
                    ],
                }
            ],
        },
    }
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "from=2026-03-20T00:00:00%2B01:00" in args[1]
    assert "to=2026-03-21T00:00:00%2B01:00" in args[1]
    assert "timezone=Europe/Berlin" in args[1]
    assert "step=P1D" in args[1]
    assert callable(kwargs["headers"])
    headers = kwargs["headers"]()
    assert headers["Authorization"] == "Bearer EAUTH"
    assert headers["e-auth-token"] == "EAUTH"
    assert headers["requestId"]


@pytest.mark.asyncio
async def test_hems_energy_consumption_optional_invalid_payload_and_invalid_site() -> (
    None
):
    client = _make_client()
    client._json = AsyncMock(
        side_effect=_make_optional_payload_error("/api/v1/hems/SITE/energy-consumption")
    )

    assert (
        await client.hems_energy_consumption(
            start_at="2026-03-20T00:00:00+01:00",
            end_at="2026-03-21T00:00:00+01:00",
            timezone="Europe/Berlin",
        )
        is None
    )

    client = _make_client()
    client._json = AsyncMock(
        side_effect=_make_cre(
            550,
            json.dumps(
                {
                    "error": {
                        "status": "INVALID_SITE",
                        "message": "site is not a valid hems site",
                    }
                }
            ),
        )
    )

    assert (
        await client.hems_energy_consumption(
            start_at="2026-03-20T00:00:00+01:00",
            end_at="2026-03-21T00:00:00+01:00",
            timezone="Europe/Berlin",
        )
        is None
    )
    assert client.hems_site_supported is False


@pytest.mark.asyncio
async def test_hems_energy_consumption_optional_and_reraise_variants() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=api.Unauthorized())

    with pytest.raises(api.Unauthorized):
        await client.hems_energy_consumption(
            start_at="2026-03-20T00:00:00+01:00",
            end_at="2026-03-21T00:00:00+01:00",
            timezone="Europe/Berlin",
        )

    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(403))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_energy_consumption(
            start_at="2026-03-20T00:00:00+01:00",
            end_at="2026-03-21T00:00:00+01:00",
            timezone="Europe/Berlin",
        )

    client = _make_client()
    invalid_json = api.InvalidPayloadError(
        "Invalid JSON response",
        status=200,
        content_type="application/json",
        endpoint="/api/v1/hems/SITE/energy-consumption",
    )
    client._json = AsyncMock(side_effect=invalid_json)

    with pytest.raises(api.InvalidPayloadError):
        await client.hems_energy_consumption(
            start_at="2026-03-20T00:00:00+01:00",
            end_at="2026-03-21T00:00:00+01:00",
            timezone="Europe/Berlin",
        )

    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_energy_consumption(
            start_at="2026-03-20T00:00:00+01:00",
            end_at="2026-03-21T00:00:00+01:00",
            timezone="Europe/Berlin",
        )


@pytest.mark.asyncio
async def test_pv_system_today_normalization_and_headers() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "stats": [
                {
                    "heat_pump": [
                        {"grid": "100.0", "solar": 20},
                        {"battery": 5.5},
                    ]
                }
            ],
            "timestamp": "2026-03-20T08:00:00Z",
        }
    )

    payload = await client.pv_system_today()

    assert payload == {
        "stats": [
            {
                "heat_pump": [
                    {"grid": "100.0", "solar": 20},
                    {"battery": 5.5},
                ],
                "heatpump": [
                    {"grid": "100.0", "solar": 20},
                    {"battery": 5.5},
                ],
            }
        ],
        "timestamp": "2026-03-20T08:00:00Z",
    }
    args, kwargs = client._json.await_args
    assert args == ("GET", "https://enlighten.enphaseenergy.com/pv/systems/SITE/today")
    headers = kwargs["headers"]()
    assert headers["Accept"] == "application/json, text/javascript, */*; q=0.01"
    assert "/web/SITE/today/graph/hours" in headers["Referer"]
    assert kwargs["allow_reauth"] is True


@pytest.mark.asyncio
async def test_pv_system_today_can_disable_reauth() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"stats": []})

    assert await client.pv_system_today(allow_reauth=False) == {"stats": []}

    _args, kwargs = client._json.await_args
    assert kwargs["allow_reauth"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        api.Unauthorized(),
        _make_cre(401, "Unauthorized"),
        _make_cre(403, "Forbidden"),
    ],
)
async def test_pv_system_today_auth_errors_reraise_when_reauth_disabled(
    error,
) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    with pytest.raises((api.Unauthorized, aiohttp.ClientResponseError)):
        await client.pv_system_today(allow_reauth=False)


@pytest.mark.asyncio
async def test_pv_system_today_optional_failures_return_none() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=_make_optional_payload_error("/pv/systems/SITE/today")
    )
    assert await client.pv_system_today() is None

    client = _make_client()
    client._json = AsyncMock(side_effect=api.Unauthorized())
    assert await client.pv_system_today() is None

    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(404))
    assert await client.pv_system_today() is None

    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(403))
    assert await client.pv_system_today() is None


def test_normalize_pv_system_today_payload_edge_cases() -> None:
    assert api.EnphaseEVClient._normalize_pv_system_today_payload(None) is None
    assert api.EnphaseEVClient._normalize_pv_system_today_payload(
        {
            "stats": [
                "skip-me",
                {"other": 1},
                {"heat-pump": [1, 2, 3]},
            ]
        }
    ) == {
        "stats": [
            {"other": 1},
            {"heat-pump": [1, 2, 3], "heatpump": [1, 2, 3]},
        ]
    }


@pytest.mark.asyncio
async def test_pv_system_today_reraises_non_optional_failures() -> None:
    client = _make_client()
    invalid_json = api.InvalidPayloadError(
        "Invalid JSON response",
        status=200,
        content_type="application/json",
        endpoint="/pv/systems/SITE/today",
    )
    client._json = AsyncMock(side_effect=invalid_json)

    with pytest.raises(api.InvalidPayloadError):
        await client.pv_system_today()

    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(500))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.pv_system_today()


@pytest.mark.asyncio
async def test_hems_devices_uses_dedicated_endpoint_and_headers() -> None:
    client = _make_client()
    cookie_token = _make_token({"user_id": "user-123"})
    client.update_credentials(
        cookie=f"enlighten_manager_token_production={cookie_token}; XSRF-TOKEN=xsrf",
        eauth="EAUTH",
    )
    client._json = AsyncMock(return_value={"data": {"hems-devices": {}}})

    payload = await client.hems_devices()

    assert payload == {"data": {"hems-devices": {}}}
    assert client.hems_site_supported is True
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert args[1].endswith("/api/v1/hems/SITE/hems-devices?refreshData=false")
    assert callable(kwargs["headers"])
    headers = kwargs["headers"]()
    assert headers["Authorization"] == f"Bearer {cookie_token}"
    assert headers["e-auth-token"] == "EAUTH"
    assert headers["X-CSRF-Token"] == "xsrf"
    assert headers["username"] == "user-123"
    assert headers["requestId"]


@pytest.mark.asyncio
async def test_system_dashboard_summary_sets_hems_support_hint() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR; XSRF-TOKEN=xsrf",
        eauth="EAUTH",
    )
    client._json = AsyncMock(return_value={"is_hems": False, "geo": "APAC"})

    payload = await client.system_dashboard_summary()

    assert payload == {"is_hems": False, "geo": "APAC"}
    assert client.hems_site_supported is False
    assert client._system_dashboard_summary_payload == payload  # noqa: SLF001
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert args[1].endswith(
        "/service/system_dashboard/api_internal/cs/sites/SITE/summary"
    )
    assert kwargs["headers"]["Authorization"] == "Bearer BEAR"
    assert kwargs["headers"]["e-auth-token"] == "EAUTH"
    assert kwargs["headers"]["X-CSRF-Token"] == "xsrf"
    assert kwargs["allow_reauth"] is True


@pytest.mark.asyncio
async def test_system_dashboard_summary_can_disable_reauth() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"is_hems": True})

    assert await client.system_dashboard_summary(allow_reauth=False) == {
        "is_hems": True
    }

    _args, kwargs = client._json.await_args
    assert kwargs["allow_reauth"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        api.Unauthorized(),
        api.EnphaseLoginWallUnauthorized(
            endpoint="/service/system_dashboard/api_internal/cs/sites/SITE/summary",
            request_label="GET /service/system_dashboard/api_internal/cs/sites/SITE/summary",
            status=200,
            content_type="text/html",
        ),
        _make_cre(401, "Unauthorized"),
        _make_cre(403, "Forbidden"),
    ],
)
async def test_system_dashboard_summary_auth_errors_reraise_when_reauth_disabled(
    error,
) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    with pytest.raises((api.Unauthorized, aiohttp.ClientResponseError)):
        await client.system_dashboard_summary(allow_reauth=False)


@pytest.mark.asyncio
async def test_system_dashboard_summary_optional_404_still_soft_fails_without_reauth() -> (
    None
):
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(404, "Not found"))

    assert await client.system_dashboard_summary(allow_reauth=False) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_system_dashboard_summary_optional_errors_return_none(
    monkeypatch, status
) -> None:
    client = _make_client()
    err = _make_cre(status, "Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.system_dashboard_summary() is None
    assert client.hems_site_supported is None


@pytest.mark.asyncio
async def test_system_dashboard_summary_returns_none_for_non_dict_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["not-a-dict"])

    assert await client.system_dashboard_summary() is None
    assert client.hems_site_supported is None


@pytest.mark.asyncio
async def test_system_dashboard_events_uses_observed_query_and_headers() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR; XSRF-TOKEN=xsrf",
        eauth="EAUTH",
    )
    client._json = AsyncMock(return_value={"events": []})

    assert await client.system_dashboard_events() == {
        "events": [],
        "_enphase_ev_truncated": False,
    }

    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    url = URL(args[1])
    assert url.path.endswith(
        "/service/system_dashboard/api_internal/cs/sites/SITE/events"
    )
    assert dict(url.query) == {
        "range": "today",
        "cassandra_toggle": "false",
        "filter_columns": (
            "serial_number,device_type,event_date,cleared_date,event_type,"
            "event_state,details,updated_at,alarm_id"
        ),
        "serial_numbers": "",
        "type": "table",
        "event_state": "default",
        "page": "1",
        "per_page": "200",
    }
    assert kwargs["headers"]["Authorization"] == "Bearer BEAR"
    assert kwargs["headers"]["e-auth-token"] == "EAUTH"
    assert kwargs["headers"]["X-CSRF-Token"] == "xsrf"


@pytest.mark.asyncio
async def test_system_dashboard_events_paginates_and_merges_catalogs() -> None:
    client = _make_client()
    first_page = {"events": [{"id": str(index)} for index in range(200)]}
    second_page = {
        "events": [{"id": "last"}],
        "event_types": [{"id": 1}],
        "event_states": [{"id": 2}],
        "event_severities": [{"id": 3}],
    }
    client._json = AsyncMock(side_effect=[first_page, second_page])

    result = await client.system_dashboard_events()

    assert result is not None
    assert len(result["events"]) == 201
    assert result["events"][-1] == {"id": "last"}
    assert result["event_types"] == [{"id": 1}]
    assert result["event_states"] == [{"id": 2}]
    assert result["event_severities"] == [{"id": 3}]
    assert result["_enphase_ev_truncated"] is False
    assert client._json.await_count == 2
    first_url = URL(client._json.await_args_list[0].args[1])
    second_url = URL(client._json.await_args_list[1].args[1])
    assert first_url.query["page"] == "1"
    assert second_url.query["page"] == "2"


@pytest.mark.asyncio
async def test_system_dashboard_events_pagination_is_bounded() -> None:
    client = _make_client()
    full_page = {"events": [{"id": str(index)} for index in range(200)]}
    client._json = AsyncMock(return_value=full_page)

    result = await client.system_dashboard_events()

    assert result is not None
    assert len(result["events"]) == 2_000
    assert result["_enphase_ev_truncated"] is True
    assert client._json.await_count == 10


@pytest.mark.asyncio
async def test_system_dashboard_events_rejects_invalid_later_page() -> None:
    client = _make_client()
    client._json = AsyncMock(
        side_effect=[
            {"events": [{"id": str(index)} for index in range(200)]},
            {"events": "invalid"},
        ]
    )

    assert await client.system_dashboard_events() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [api.Unauthorized(), _make_cre(404)])
async def test_system_dashboard_events_optional_errors_return_none(error) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    assert await client.system_dashboard_events() is None


@pytest.mark.asyncio
async def test_system_dashboard_events_rejects_invalid_shapes_and_unexpected_errors() -> (
    None
):
    client = _make_client()
    client._json = AsyncMock(return_value=[])
    assert await client.system_dashboard_events() is None

    client._json = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await client.system_dashboard_events()


@pytest.mark.asyncio
async def test_homeowner_events_page_uses_cursor_locale_and_history_headers() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR; XSRF-TOKEN=xsrf",
        eauth="EAUTH",
    )
    client._json = AsyncMock(return_value={"events": [], "next": "private cursor"})

    result = await client.homeowner_events_page(
        next_cursor="private cursor",
        page_size=500,
        locale="en-AU",
    )

    assert result == {"events": [], "next": "private cursor"}
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    url = URL(args[1])
    assert url.path.endswith(
        "/service/events-platform-service/v1.0/SITE/events/homeowner"
    )
    assert dict(url.query) == {
        "next": "private cursor",
        "page_size": "200",
        "locale": "en-AU",
    }
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert kwargs["headers"]["e-auth-token"] == "EAUTH"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        [],
        {},
        {"events": "invalid"},
        {"events": [], "next": {}},
        {"events": [], "next": ""},
    ),
)
async def test_homeowner_events_page_rejects_invalid_shapes(payload) -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=payload)

    assert await client.homeowner_events_page() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        api.Unauthorized(),
        _make_cre(403),
        _make_cre(404),
        api.InvalidPayloadError("invalid", status=200),
    ),
)
async def test_homeowner_events_page_optional_errors_return_none(error) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    assert await client.homeowner_events_page() is None


@pytest.mark.asyncio
async def test_homeowner_events_page_preserves_login_wall_failure() -> None:
    client = _make_client()
    error = api.EnphaseLoginWallUnauthorized(
        endpoint="/service/events-platform-service/v1.0/SITE/events/homeowner",
        request_label="GET homeowner events",
        status=401,
        content_type="text/html",
        body_preview_redacted="login",
    )
    client._json = AsyncMock(side_effect=error)

    with pytest.raises(api.EnphaseLoginWallUnauthorized):
        await client.homeowner_events_page()


@pytest.mark.asyncio
async def test_system_dashboard_standing_alarms_uses_observed_query_and_headers() -> (
    None
):
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR; XSRF-TOKEN=xsrf",
        eauth="EAUTH",
    )
    client._json = AsyncMock(return_value={"alarms": []})

    assert await client.system_dashboard_standing_alarms() == {
        "alarms": [],
        "_enphase_ev_truncated": False,
    }

    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    url = URL(args[1])
    assert url.path.endswith(
        "/service/system_dashboard/api_internal/dashboard/sites/SITE/alarms"
    )
    assert dict(url.query) == {
        "range": "today",
        "filter_columns": "id,severity,type,serial_num,description,first_set",
        "type": "table",
        "page": "1",
        "per_page": "200",
    }
    assert kwargs["headers"]["Authorization"] == "Bearer BEAR"
    assert kwargs["headers"]["e-auth-token"] == "EAUTH"
    assert kwargs["headers"]["X-CSRF-Token"] == "xsrf"


@pytest.mark.asyncio
async def test_system_dashboard_standing_alarms_paginates_and_is_bounded() -> None:
    client = _make_client()
    full_page = {"alarms": [{"id": str(index)} for index in range(200)]}
    last_page = {"alarms": [{"id": "last"}]}
    client._json = AsyncMock(side_effect=[full_page, last_page])

    result = await client.system_dashboard_standing_alarms()

    assert result is not None
    assert len(result["alarms"]) == 201
    assert result["alarms"][-1] == {"id": "last"}
    assert result["_enphase_ev_truncated"] is False

    client._json = AsyncMock(return_value=full_page)
    result = await client.system_dashboard_standing_alarms()
    assert result is not None
    assert len(result["alarms"]) == 2_000
    assert result["_enphase_ev_truncated"] is True
    assert client._json.await_count == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [api.Unauthorized(), _make_cre(404)])
async def test_system_dashboard_standing_alarms_optional_errors_return_none(
    error,
) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    assert await client.system_dashboard_standing_alarms() is None


@pytest.mark.asyncio
async def test_system_dashboard_standing_alarms_rejects_invalid_payloads() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=[])
    assert await client.system_dashboard_standing_alarms() is None

    client._json = AsyncMock(return_value={"alarms": "invalid"})
    assert await client.system_dashboard_standing_alarms() is None

    client._json = AsyncMock(
        side_effect=[
            {"alarms": [{"id": str(index)} for index in range(200)]},
            {"alarms": "invalid"},
        ]
    )
    assert await client.system_dashboard_standing_alarms() is None

    client._json = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await client.system_dashboard_standing_alarms()


@pytest.mark.asyncio
async def test_hems_devices_supports_refresh_data_query_flag() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": {}})

    await client.hems_devices(refresh_data=True)

    args, _kwargs = client._json.await_args
    assert args[1].endswith("/api/v1/hems/SITE/hems-devices?refreshData=true")


@pytest.mark.asyncio
async def test_hems_devices_returns_none_when_payload_not_dict() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["bad"])

    assert await client.hems_devices() is None


@pytest.mark.asyncio
async def test_hems_devices_raises_on_unauthorized() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=api.Unauthorized("nope"))

    with pytest.raises(api.Unauthorized):
        await client.hems_devices()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_hems_devices_auth_errors_reraise(monkeypatch, status) -> None:
    client = _make_client()
    err = _make_cre(status, "Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_devices()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404])
async def test_hems_devices_optional_errors_return_none(monkeypatch, status) -> None:
    client = _make_client()
    err = _make_cre(status, "Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_devices() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        '{"type":"hemsIntegrationError","error":{"code":900,"status":"INVALID_SITE","message":"Site is not a valid HEMS site"}}',
        "INVALID_SITE: Site is not a valid HEMS site",
    ],
)
async def test_hems_devices_invalid_site_error_returns_none(
    monkeypatch, message
) -> None:
    client = _make_client()
    err = _make_cre(550, message)
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_devices() is None
    assert client.hems_site_supported is False


@pytest.mark.asyncio
async def test_hems_devices_non_json_payload_returns_none(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=text/html, endpoint=/api/v1/hems/SITE/hems-devices, decode_error=ContentTypeError)",
        status=200,
        content_type="text/html",
        endpoint="/api/v1/hems/SITE/hems-devices",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_devices() is None


@pytest.mark.asyncio
async def test_hems_devices_json_invalid_payload_reraises(monkeypatch) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=application/json, endpoint=/api/v1/hems/SITE/hems-devices, decode_error=ValueError)",
        status=200,
        content_type="application/json",
        endpoint="/api/v1/hems/SITE/hems-devices",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(api.InvalidPayloadError):
        await client.hems_devices()


@pytest.mark.asyncio
async def test_show_livestream_returns_capability_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"live_status": True, "live_vitals": False})

    payload = await client.show_livestream()

    assert payload == {"live_status": True, "live_vitals": False}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/show_livestream",
        headers=client._system_dashboard_headers(),
        allow_reauth=True,
    )


@pytest.mark.asyncio
async def test_show_livestream_can_disable_reauth() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"live_status": True})

    assert await client.show_livestream(allow_reauth=False) == {"live_status": True}

    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/app-api/SITE/show_livestream",
        headers=client._system_dashboard_headers(),
        allow_reauth=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        api.Unauthorized(),
        _make_cre(401, "Unauthorized"),
        _make_cre(403, "Forbidden"),
    ],
)
async def test_show_livestream_auth_errors_reraise_when_reauth_disabled(
    error,
) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    with pytest.raises((api.Unauthorized, aiohttp.ClientResponseError)):
        await client.show_livestream(allow_reauth=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_show_livestream_optional_errors_return_none(monkeypatch, status) -> None:
    client = _make_client()
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=_make_cre(status)))

    assert await client.show_livestream() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "side_effect",
    [
        api.Unauthorized(),
        _make_optional_payload_error("/app-api/SITE/show_livestream"),
    ],
)
async def test_show_livestream_returns_none_for_optional_failures(
    monkeypatch, side_effect
) -> None:
    client = _make_client()
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=side_effect))

    assert await client.show_livestream() is None


@pytest.mark.asyncio
async def test_show_livestream_reraises_unexpected_failures(monkeypatch) -> None:
    client = _make_client()

    monkeypatch.setattr(
        client,
        "_json",
        AsyncMock(
            side_effect=api.InvalidPayloadError(
                "bad json",
                status=200,
                content_type="application/json",
                endpoint="/app-api/SITE/show_livestream",
            )
        ),
    )
    with pytest.raises(api.InvalidPayloadError):
        await client.show_livestream()

    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=_make_cre(500)))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.show_livestream()


@pytest.mark.asyncio
async def test_show_livestream_returns_none_for_non_mapping_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=["not", "a", "dict"])

    assert await client.show_livestream() is None


@pytest.mark.asyncio
async def test_site_livestream_authorizer_returns_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"live_stream_topic": "v1/live-stream/abc"})

    payload = await client.site_livestream_authorizer("GW-1")

    assert payload == {"live_stream_topic": "v1/live-stream/abc"}
    client._json.assert_awaited_once_with(
        "GET",
        f"{api.BASE_URL}/pv/aws_sigv4/livestream.json?serial_num=GW-1",
        headers={
            **client._today_headers(),
            "X-Requested-With": "XMLHttpRequest",
        },
        allow_reauth=True,
    )

    await client.site_livestream_authorizer("GW-1", live_debug=True)
    assert client._json.await_args.args[1] == (
        f"{api.BASE_URL}/service/system_dashboard/api_internal/cs/sites/livestream"
        "?serial_num=GW-1&live_debug=true"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        api.Unauthorized(),
        _make_optional_payload_error("/pv/aws_sigv4/livestream.json"),
        _make_cre(404),
    ],
)
async def test_site_livestream_authorizer_optional_errors_return_none(error) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    assert await client.site_livestream_authorizer("GW-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [api.Unauthorized(), _make_cre(403)])
async def test_site_livestream_authorizer_auth_errors_reraise_when_disabled(
    error,
) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    with pytest.raises((api.Unauthorized, aiohttp.ClientResponseError)):
        await client.site_livestream_authorizer("GW-1", allow_reauth=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        api.InvalidPayloadError(
            "bad json",
            status=200,
            content_type="application/json",
            endpoint="/pv/aws_sigv4/livestream.json",
        ),
        _make_cre(500),
    ],
)
async def test_site_livestream_authorizer_reraises_unexpected_errors(error) -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=error)

    with pytest.raises((api.InvalidPayloadError, aiohttp.ClientResponseError)):
        await client.site_livestream_authorizer("GW-1")


@pytest.mark.asyncio
async def test_site_livestream_payload_reads_json_mqtt_message() -> None:
    client = _make_client()
    client.site_livestream_authorizer = AsyncMock(
        return_value={
            "live_stream_topic": "v1/live-stream/abc",
            "aws_iot_endpoint": "mqtt.example.test",
            "aws_authorizer": "authorizer",
            "aws_token_key": "enph_token",
            "aws_token_value": "session",
            "aws_digest": "sig+/=",
        }
    )
    client._read_mqtt_websocket_payload = AsyncMock(
        return_value=b'{"meters":{"gridRelay":"OPER_RELAY_OPEN"}}'
    )

    payload = await client.site_livestream_payload("GW-1", timeout_s=5)

    assert payload == {"meters": {"gridRelay": "OPER_RELAY_OPEN"}}
    client.site_livestream_authorizer.assert_awaited_once_with(
        "GW-1",
        live_debug=False,
        allow_reauth=True,
    )
    args, kwargs = client._read_mqtt_websocket_payload.await_args
    assert args[:2] == ("mqtt.example.test", "v1/live-stream/abc")
    assert "x-amz-customauthorizer-name=authorizer" in args[2]
    assert "enph_token=session" in args[2]
    assert "x-amz-customauthorizer-signature=sig%2B%2F%3D" in args[2]
    assert kwargs == {"timeout_s": 5}


@pytest.mark.asyncio
async def test_site_livestream_payload_reads_protobuf_mqtt_message() -> None:
    client = _make_client()
    client.site_livestream_authorizer = AsyncMock(
        return_value={
            "live_stream_topic": "v1/live-stream/abc",
            "aws_iot_endpoint": "mqtt.example.test",
            "aws_authorizer": "authorizer",
            "aws_token_key": "enph_token",
            "aws_token_value": "session",
            "aws_digest": "sig",
        }
    )
    # DataMsg.protocol_ver=1, DataMsg.meters.grid_relay=OPER_RELAY_OPEN.
    client._read_mqtt_websocket_payload = AsyncMock(
        return_value=b"\x08\x01\x1a\x02\x28\x01"
    )

    payload = await client.site_livestream_payload("GW-1")

    assert payload == {"meters": {"gridRelay": "OPER_RELAY_OPEN"}}


@pytest.mark.asyncio
async def test_site_livestream_payload_returns_none_for_missing_authorizer_fields() -> (
    None
):
    client = _make_client()
    client.site_livestream_authorizer = AsyncMock(
        side_effect=[
            None,
            {"live_stream_topic": "v1/live-stream/abc"},
            {
                "live_stream_topic": "v1/live-stream/abc",
                "aws_iot_endpoint": "mqtt.example.test",
                "aws_authorizer": "authorizer",
                "aws_token_key": "enph_token",
                "aws_token_value": "session",
                "aws_digest": "sig",
            },
        ]
    )
    client._read_mqtt_websocket_payload = AsyncMock(return_value=b"not-json")

    assert await client.site_livestream_payload("GW-1") is None
    assert await client.site_livestream_payload("GW-1") is None
    assert await client.site_livestream_payload("GW-1") is None


def test_mqtt_packet_helpers_extract_publish_payload() -> None:
    payload = b'{"meters":{"gridRelay":"OPER_RELAY_CLOSED"}}'
    publish = api.EnphaseEVClient._mqtt_packet(
        0x30,
        api.EnphaseEVClient._mqtt_string("v1/live-stream/abc") + payload,
    )

    packets = list(api.EnphaseEVClient._mqtt_packets(publish))

    assert packets == [
        (
            0x30,
            api.EnphaseEVClient._mqtt_string("v1/live-stream/abc") + payload,
        )
    ]
    assert api.EnphaseEVClient._mqtt_publish_payload(*packets[0]) == payload
    assert api.EnphaseEVClient._decode_json_mqtt_payload(payload) == {
        "meters": {"gridRelay": "OPER_RELAY_CLOSED"}
    }
    assert api.EnphaseEVClient._decode_json_mqtt_payload(b'xx {"ok": true} yy') == {
        "ok": True
    }
    assert api.EnphaseEVClient._decode_json_mqtt_payload(b"not-json") is None
    assert api.EnphaseEVClient._decode_json_mqtt_payload(b"xx {bad} yy") is None

    large_packet = api.EnphaseEVClient._mqtt_packet(0x30, b"x" * 128)
    assert len(large_packet) == 131
    assert list(api.EnphaseEVClient._mqtt_packets(large_packet)) == [(0x30, b"x" * 128)]
    assert list(api.EnphaseEVClient._mqtt_packets(b"\x30\x02\x00")) == []
    assert api.EnphaseEVClient._mqtt_publish_payload(0x30, b"") == b""
    qos_payload = api.EnphaseEVClient._mqtt_string("topic") + b"\x00\x01data"
    assert api.EnphaseEVClient._mqtt_publish_payload(0x32, qos_payload) == b"data"


def test_mqtt_validation_helpers_reject_incomplete_acks() -> None:
    with pytest.raises(aiohttp.ClientConnectionError, match="CONNACK.*incomplete"):
        api.EnphaseEVClient._validate_mqtt_connack(b"\x00")
    with pytest.raises(aiohttp.ClientConnectionError, match="SUBACK.*incomplete"):
        api.EnphaseEVClient._validate_mqtt_suback(b"\x00\x01")


@pytest.mark.asyncio
async def test_mqtt_wait_rejects_closed_socket_and_expired_deadline() -> None:
    closed_ws = SimpleNamespace(
        receive=AsyncMock(
            side_effect=[
                SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data="ignored"),
                SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None),
            ]
        )
    )
    deadline = asyncio.get_running_loop().time() + 5

    with pytest.raises(aiohttp.ClientConnectionError, match="WebSocket closed"):
        await api.EnphaseEVClient._wait_for_mqtt_packet(
            closed_ws, {0x30}, deadline=deadline
        )

    with pytest.raises(asyncio.TimeoutError):
        api.EnphaseEVClient._mqtt_remaining_timeout(
            asyncio.get_running_loop().time() - 1
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "OPER_RELAY_UNKNOWN"),
        (1, "OPER_RELAY_OPEN"),
        (2, "OPER_RELAY_CLOSED"),
        (3, "OPER_RELAY_OFFGRID_AC_GRID_PRESENT"),
        (4, "OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD"),
        (5, "OPER_RELAY_WAITING_TO_INITIALIZE_ON_GRID"),
    ],
)
def test_decode_site_livestream_protobuf_grid_relay(value: int, expected: str) -> None:
    # Include unknown fixed-width fields to verify they are skipped safely.
    meters = b"\x09" + (b"\x00" * 8) + b"\x1d" + (b"\x00" * 4)
    meters += b"\x28" + bytes([value])
    payload = b"\x08\x01\x1a" + bytes([len(meters)]) + meters

    assert api.EnphaseEVClient._decode_site_livestream_payload(payload) == {
        "meters": {"gridRelay": expected}
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00",
        b"\x80" * 10,
        b"\x08",
        b"\x09\x00",
        b"\x12\x80",
        b"\x12\x02\x00",
        b"\x1d\x00",
        b"\x0b",
        b"\x08\x01",
        b"\x1a\x02\x28\x06",
        b"\x1a\x01\x28",
    ],
)
def test_decode_site_livestream_rejects_invalid_protobuf(payload: bytes) -> None:
    assert api.EnphaseEVClient._decode_site_livestream_payload(payload) is None


@pytest.mark.asyncio
async def test_read_mqtt_websocket_payload_reads_publish_frame() -> None:
    client = _make_client()
    payload = b'{"meters":{"gridRelay":"OPER_RELAY_OPEN"}}'

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.messages = [
                SimpleNamespace(
                    type=aiohttp.WSMsgType.BINARY, data=b"\x20\x02\x00\x00"
                ),
                SimpleNamespace(
                    type=aiohttp.WSMsgType.BINARY, data=b"\x90\x03\x00\x01\x00"
                ),
                SimpleNamespace(
                    type=aiohttp.WSMsgType.BINARY,
                    data=api.EnphaseEVClient._mqtt_packet(
                        0x30,
                        api.EnphaseEVClient._mqtt_string("v1/live-stream/abc")
                        + payload,
                    ),
                ),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send_bytes(self, data):
            self.sent.append(data)

        async def receive(self, *, timeout):
            return self.messages.pop(0)

    fake_ws = FakeWebSocket()
    client._s.ws_connect = MagicMock(return_value=fake_ws)

    result = await client._read_mqtt_websocket_payload(
        "mqtt.example.test",
        "v1/live-stream/abc",
        "?username",
        timeout_s=5,
    )

    assert result == payload
    assert len(fake_ws.sent) == 2
    client._s.ws_connect.assert_called_once()
    args, kwargs = client._s.ws_connect.call_args
    assert args == ("wss://mqtt.example.test/mqtt",)
    assert kwargs["protocols"] == ("mqtt",)
    assert kwargs["headers"] == {"Origin": api.BASE_URL}
    assert 0 < kwargs["timeout"] <= 5


@pytest.mark.parametrize(
    ("messages", "match", "sent_count"),
    [
        ([b"\x20\x02\x00\x05"], "CONNECT was rejected", 1),
        (
            [b"\x20\x02\x00\x00", b"\x90\x03\x00\x01\x80"],
            "subscription was rejected",
            2,
        ),
    ],
)
@pytest.mark.asyncio
async def test_read_mqtt_websocket_payload_rejects_failed_handshake(
    messages: list[bytes],
    match: str,
    sent_count: int,
) -> None:
    client = _make_client()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.messages = [
                SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=data)
                for data in messages
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send_bytes(self, data):
            self.sent.append(data)

        async def receive(self, *, timeout):
            return self.messages.pop(0)

    fake_ws = FakeWebSocket()
    client._s.ws_connect = MagicMock(return_value=fake_ws)

    with pytest.raises(aiohttp.ClientConnectionError, match=match):
        await client._read_mqtt_websocket_payload(
            "mqtt.example.test",
            "v1/live-stream/abc",
            "?username",
            timeout_s=5,
        )

    assert len(fake_ws.sent) == sent_count


@pytest.mark.asyncio
async def test_heat_pump_events_json_returns_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=[{"statusText": "Recommended"}])

    payload = await client.heat_pump_events_json("HP-1")

    assert payload == [{"statusText": "Recommended"}]
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert args[1].endswith("/systems/SITE/heat_pump/HP-1/events.json")
    assert kwargs["headers"] == client._systems_json_headers()


@pytest.mark.asyncio
async def test_heat_pump_events_json_returns_none_on_optional_errors() -> None:
    client = _make_client()
    client._json = AsyncMock(side_effect=_make_cre(404, "Unavailable"))

    assert await client.heat_pump_events_json("HP-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    [
        "heat_pump_events_json",
        "iq_er_events_json",
    ],
)
async def test_events_json_returns_none_for_blank_device_uid(method_name) -> None:
    client = _make_client()
    client._json = AsyncMock()
    method = getattr(client, method_name)

    assert await method("  ") is None
    assert await method("") is None
    client._json.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "uid", "endpoint"),
    [
        (
            "heat_pump_events_json",
            "HP-1",
            "/systems/SITE/heat_pump/HP-1/events.json",
        ),
        ("iq_er_events_json", "HP-SG", "/systems/SITE/iq_er/HP-SG/events.json"),
    ],
)
@pytest.mark.parametrize(
    "side_effect_factory",
    [
        _make_optional_payload_error,
        lambda _endpoint: _make_cre(404, "Unavailable"),
    ],
)
async def test_events_json_returns_none_for_optional_failures(
    monkeypatch, method_name, uid, endpoint, side_effect_factory
) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_json",
        AsyncMock(side_effect=side_effect_factory(endpoint)),
    )

    method = getattr(client, method_name)
    assert await method(uid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "uid", "side_effect"),
    [
        ("heat_pump_events_json", "HP-1", api.Unauthorized()),
        ("iq_er_events_json", "HP-SG", api.Unauthorized()),
        ("heat_pump_events_json", "HP-1", _make_cre(401)),
        ("iq_er_events_json", "HP-SG", _make_cre(403)),
    ],
)
async def test_events_json_auth_failures_reraise(
    monkeypatch, method_name, uid, side_effect
) -> None:
    client = _make_client()
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=side_effect))

    method = getattr(client, method_name)
    with pytest.raises((api.Unauthorized, aiohttp.ClientResponseError)):
        await method(uid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "uid", "endpoint"),
    [
        (
            "heat_pump_events_json",
            "HP-1",
            "/systems/SITE/heat_pump/HP-1/events.json",
        ),
        ("iq_er_events_json", "HP-SG", "/systems/SITE/iq_er/HP-SG/events.json"),
    ],
)
async def test_events_json_reraises_unexpected_failures(
    monkeypatch, method_name, uid, endpoint
) -> None:
    client = _make_client()
    method = getattr(client, method_name)

    monkeypatch.setattr(
        client,
        "_json",
        AsyncMock(
            side_effect=api.InvalidPayloadError(
                "bad json",
                status=200,
                content_type="application/json",
                endpoint=endpoint,
            )
        ),
    )
    with pytest.raises(api.InvalidPayloadError):
        await method(uid)

    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=_make_cre(500)))
    with pytest.raises(aiohttp.ClientResponseError):
        await method(uid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "uid", "endpoint"),
    [
        (
            "heat_pump_events_json",
            "HP-1",
            "/systems/SITE/heat_pump/HP-1/events.json",
        ),
        (
            "iq_er_events_json",
            "HP-SG",
            "/systems/SITE/iq_er/HP-SG/events.json",
        ),
    ],
)
async def test_events_json_raises_diagnostic_signal_for_html_payload_with_json_content_type(
    monkeypatch,
    method_name,
    uid,
    endpoint,
) -> None:
    client = _make_client()
    method = getattr(client, method_name)
    monkeypatch.setattr(
        client,
        "_json",
        AsyncMock(
            side_effect=api.InvalidPayloadError(
                "Invalid JSON response",
                status=200,
                content_type="application/json; charset=utf-8",
                endpoint=endpoint,
                failure_kind="json_decode",
                decode_error="JSONDecodeError",
                body_preview_redacted="<!DOCTYPE html> <html lang='fr'>login</html>",
            )
        ),
    )

    with pytest.raises(api.OptionalEndpointUnavailable, match="Invalid JSON response"):
        await method(uid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "uid", "endpoint"),
    [
        (
            "heat_pump_events_json",
            "HP-1",
            "/systems/SITE/heat_pump/HP-1/events.json",
        ),
        (
            "iq_er_events_json",
            "HP-SG",
            "/systems/SITE/iq_er/HP-SG/events.json",
        ),
    ],
)
async def test_events_json_real_json_decode_html_payload_suppresses_warning(
    caplog: pytest.LogCaptureFixture,
    method_name,
    uid,
    endpoint,
) -> None:
    response = _FakeResponse(
        status=200,
        json_body=json.JSONDecodeError("Expecting value", "", 0),
        text_body="<!DOCTYPE html><html lang='fr'>login</html>",
    )
    response.headers = {"Content-Type": "application/json; charset=utf-8"}
    client = _make_client(_FakeSession([response]))

    method = getattr(client, method_name)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(
            api.OptionalEndpointUnavailable, match="Invalid JSON response"
        ):
            await method(uid)

    assert "Invalid payload for site" not in caplog.text
    assert endpoint not in client._payload_failure_log_state  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "uid", "endpoint"),
    [
        (
            "heat_pump_events_json",
            "HP-1",
            "/systems/SITE/heat_pump/HP-1/events.json",
        ),
        (
            "iq_er_events_json",
            "HP-SG",
            "/systems/SITE/iq_er/HP-SG/events.json",
        ),
    ],
)
async def test_events_json_real_json_decode_non_html_payload_logs_once(
    caplog: pytest.LogCaptureFixture,
    method_name,
    uid,
    endpoint,
) -> None:
    response = _FakeResponse(
        status=200,
        json_body=json.JSONDecodeError("Expecting value", "", 0),
        text_body='{"broken": true',
    )
    response.headers = {"Content-Type": "application/json; charset=utf-8"}
    client = _make_client(_FakeSession([response]))

    method = getattr(client, method_name)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(api.InvalidPayloadError):
            await method(uid)

    assert "Invalid payload for site [site] endpoint" in caplog.text
    assert endpoint in client._payload_failure_log_state  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "uid"),
    [
        ("heat_pump_events_json", "HP-1"),
        ("iq_er_events_json", "HP-SG"),
    ],
)
async def test_events_json_returns_none_for_non_container_payload(
    method_name, uid
) -> None:
    client = _make_client()
    client._json = AsyncMock(return_value="not-json-container")

    method = getattr(client, method_name)
    assert await method(uid) is None


@pytest.mark.asyncio
async def test_iq_er_events_json_returns_payload() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value=[{"statusText": "Recommended"}])

    payload = await client.iq_er_events_json("HP-SG")

    assert payload == [{"statusText": "Recommended"}]
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert args[1].endswith("/systems/SITE/iq_er/HP-SG/events.json")
    assert kwargs["headers"] == client._systems_json_headers()


def test_is_optional_non_json_payload_false_for_invalid_status() -> None:
    err = api.InvalidPayloadError(
        "Invalid JSON response",
        status=200,
        content_type="text/html",
        endpoint="/systems/SITE/heat_pump/HP-1/events.json",
    )
    err.status = "bad"  # type: ignore[assignment]

    assert api._is_optional_non_json_payload(err) is False


def test_is_optional_non_json_payload_false_for_non_2xx_status() -> None:
    err = api.InvalidPayloadError(
        "Invalid JSON response",
        status=500,
        content_type="text/html",
        endpoint="/systems/SITE/heat_pump/HP-1/events.json",
    )

    assert api._is_optional_non_json_payload(err) is False


def test_is_optional_html_payload_true_for_html_preview() -> None:
    err = api.InvalidPayloadError(
        "Invalid JSON response",
        status=200,
        content_type="application/json; charset=utf-8",
        endpoint="/systems/SITE/heat_pump/HP-1/events.json",
        failure_kind="json_decode",
        decode_error="JSONDecodeError",
        body_preview_redacted="<!DOCTYPE html> <html lang='fr'>login</html>",
    )

    assert api._is_optional_html_payload(err) is True


def test_is_optional_html_payload_false_for_non_html_preview() -> None:
    err = api.InvalidPayloadError(
        "Invalid JSON response",
        status=200,
        content_type="application/json; charset=utf-8",
        endpoint="/systems/SITE/heat_pump/HP-1/events.json",
        failure_kind="json_decode",
        decode_error="JSONDecodeError",
        body_preview_redacted='{"broken": true',
    )

    assert api._is_optional_html_payload(err) is False


def test_is_optional_html_payload_false_for_invalid_status_value() -> None:
    err = api.InvalidPayloadError(
        "Invalid JSON response",
        status=200,
        content_type="application/json; charset=utf-8",
        endpoint="/systems/SITE/heat_pump/HP-1/events.json",
        failure_kind="json_decode",
        decode_error="JSONDecodeError",
        body_preview_redacted="<!DOCTYPE html> <html lang='fr'>login</html>",
    )
    err.status = "bad"  # type: ignore[assignment]

    assert api._is_optional_html_payload(err) is False


def test_is_optional_html_payload_false_for_non_2xx_status() -> None:
    err = api.InvalidPayloadError(
        "Invalid JSON response",
        status=500,
        content_type="application/json; charset=utf-8",
        endpoint="/systems/SITE/heat_pump/HP-1/events.json",
        failure_kind="json_decode",
        decode_error="JSONDecodeError",
        body_preview_redacted="<!DOCTYPE html> <html lang='fr'>login</html>",
    )

    assert api._is_optional_html_payload(err) is False


def test_payload_failure_signature_and_preview_helpers_cover_edge_branches() -> None:
    signature = api.PayloadFailureSignature(failure_kind="shape")
    assert signature.summary() == "Invalid payload shape (failure_kind=shape)"
    assert api._truncate_preview("x" * 5, max_length=3) == "xxx..."
    assert api._payload_preview_and_hash(None) == (None, None, None)

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    redacted = api._redact_debug_json_body(
        {
            BadStr(): "site",
            "items": ["", "SITE", "SERIAL-1234", "value"],
            "blank": " ",
        },
        site_ids=(BadStr(), "SITE"),
    )
    assert redacted["key"] == "site"
    assert redacted["items"] == ["", "[site]", "SERIAL-1234", "value"]
    assert redacted["blank"] == " "
    assert api._redact_debug_json_body(5) == 5


def test_payload_preview_and_hash_handles_bytes_and_fallback_branches(
    monkeypatch,
) -> None:
    length, digest, preview = api._payload_preview_and_hash(b'{"site":"SITE"}')
    assert length == len(b'{"site":"SITE"}')
    assert digest == hashlib.sha256(b'{"site":"SITE"}').hexdigest()
    assert preview == '{"site":"[site]"}'

    monkeypatch.setattr(api.json, "dumps", MagicMock(side_effect=TypeError("boom")))
    monkeypatch.setattr(
        api, "_redact_debug_json_body", MagicMock(side_effect=TypeError)
    )

    class BadPayload:
        def __str__(self) -> str:
            return "SERIAL-12345678"

    length, digest, preview = api._payload_preview_and_hash(BadPayload())
    assert length == len("SERIAL-12345678".encode())
    assert digest == hashlib.sha256(b"SERIAL-12345678").hexdigest()
    assert preview == "SERIAL-12345678"

    monkeypatch.setattr(
        api,
        "_redact_debug_json_body",
        MagicMock(return_value=object()),
    )
    monkeypatch.setattr(api.json, "dumps", MagicMock(side_effect=TypeError("boom")))
    length, digest, preview = api._payload_preview_and_hash('{"site":"SITE"}')
    assert length == len(b'{"site":"SITE"}')
    assert digest == hashlib.sha256(b'{"site":"SITE"}').hexdigest()
    assert preview == '{"site":"SITE"}'


def test_is_hems_invalid_site_error_handles_invalid_status_value() -> None:
    err = _make_cre(550, "INVALID_SITE")
    err.status = "bad"  # type: ignore[assignment]

    assert api._is_hems_invalid_site_error(err) is False


def test_is_hems_invalid_site_error_handles_empty_message() -> None:
    err = _make_cre(550, "")

    assert api._is_hems_invalid_site_error(err) is False


def test_is_hems_invalid_site_error_handles_non_dict_json() -> None:
    err = _make_cre(550, '["INVALID_SITE"]')

    assert api._is_hems_invalid_site_error(err) is False


def test_is_hems_invalid_site_error_handles_non_matching_json_dict() -> None:
    err = _make_cre(550, '{"type":"other","error":{"status":"NOPE"}}')

    assert api._is_hems_invalid_site_error(err) is False


def test_is_hems_invalid_site_error_accepts_missing_type_with_invalid_status() -> None:
    err = _make_cre(
        550,
        '{"error":{"code":900,"status":"INVALID_SITE","message":"Site is not a valid HEMS site"}}',
    )

    assert api._is_hems_invalid_site_error(err) is True


def test_is_hems_invalid_site_error_accepts_code_and_message_fallback() -> None:
    err = _make_cre(
        550,
        '{"type":"hemsIntegrationError","error":{"code":900,"status":"OTHER","message":"Site is not a valid HEMS site"}}',
    )

    assert api._is_hems_invalid_site_error(err) is True


@pytest.mark.asyncio
async def test_hems_devices_reraises_non_optional_error(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(500, "Server Error")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_devices()


@pytest.mark.asyncio
async def test_lifetime_energy_normalization_accepts_alias_fields() -> None:
    client = _make_client()
    client._json = AsyncMock(
        return_value={
            "data": {
                "evse": [1],
                "heat_pump": [10, "20"],
                "water-heater": [30],
                "evse_charging": [40],
                "startDate": "2024-01-02",
                "lastReportDate": 1700000001,
                "updatePending": True,
                "systemId": 12345,
                "intervalMinutes": "45",
            }
        }
    )

    payload = await client.lifetime_energy()

    assert payload["heatpump"] == [10.0, 20.0]
    assert payload["water_heater"] == [30.0]
    # Canonical key wins when alias and canonical are both provided.
    assert payload["evse"] == [1.0]
    assert payload["start_date"] == "2024-01-02"
    assert payload["last_report_date"] == 1700000001
    assert payload["update_pending"] is True
    assert payload["system_id"] == 12345
    assert payload["interval_minutes"] == 45.0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_hems_consumption_lifetime_auth_errors_reraise(
    monkeypatch, status
) -> None:
    client = _make_client()
    err = _make_cre(status, "Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_consumption_lifetime()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404])
async def test_hems_consumption_lifetime_optional_errors_return_none(
    monkeypatch, status
) -> None:
    client = _make_client()
    err = _make_cre(status, "Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_consumption_lifetime() is None


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_invalid_site_error_returns_none(
    monkeypatch,
) -> None:
    client = _make_client()
    err = _make_cre(
        550,
        '{"type":"hemsIntegrationError","error":{"code":900,"status":"INVALID_SITE","message":"Site is not a valid HEMS site"}}',
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_consumption_lifetime() is None
    assert client.hems_site_supported is False


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_non_json_payload_returns_none(
    monkeypatch,
) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=text/html, endpoint=/systems/SITE/hems_consumption_lifetime, decode_error=ContentTypeError)",
        status=200,
        content_type="text/html",
        endpoint="/systems/SITE/hems_consumption_lifetime",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    assert await client.hems_consumption_lifetime() is None


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_reraises_non_optional_error(
    monkeypatch,
) -> None:
    client = _make_client()
    err = _make_cre(500, "Server Error")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.hems_consumption_lifetime()


@pytest.mark.asyncio
async def test_hems_consumption_lifetime_json_invalid_payload_reraises(
    monkeypatch,
) -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, content_type=application/json, endpoint=/systems/SITE/hems_consumption_lifetime, decode_error=ValueError)",
        status=200,
        content_type="application/json",
        endpoint="/systems/SITE/hems_consumption_lifetime",
    )
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))

    with pytest.raises(api.InvalidPayloadError):
        await client.hems_consumption_lifetime()


@pytest.mark.asyncio
async def test_summary_v2_normalizes_list() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": [{"serialNumber": "EV"}]})
    data = await client.summary_v2()
    assert data == [{"serialNumber": "EV"}]


@pytest.mark.asyncio
async def test_summary_v2_handles_exception() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value="not-a-dict")
    assert await client.summary_v2() is None


@pytest.mark.asyncio
async def test_summary_v2_raises_optional_endpoint_unavailable_for_html_json_payload() -> (
    None
):
    client = _make_client()
    client._json = AsyncMock(
        side_effect=api.InvalidPayloadError(
            "Invalid JSON response (status=200, endpoint=/service/evse_controller/api/v2/SITE/ev_chargers/summary)",
            endpoint="/service/evse_controller/api/v2/SITE/ev_chargers/summary",
            status=200,
            content_type="application/json; charset=utf-8",
            failure_kind="json_decode",
            decode_error="JSONDecodeError",
            body_preview_redacted="<!DOCTYPE html><html lang='fr'>",
        )
    )

    with pytest.raises(api.OptionalEndpointUnavailable, match="Invalid JSON response"):
        await client.summary_v2()


@pytest.mark.asyncio
async def test_summary_v2_reraises_non_optional_invalid_payload() -> None:
    client = _make_client()
    err = api.InvalidPayloadError(
        "Invalid JSON response (status=200, endpoint=/service/evse_controller/api/v2/SITE/ev_chargers/summary)",
        endpoint="/service/evse_controller/api/v2/SITE/ev_chargers/summary",
        status=200,
        content_type="application/json; charset=utf-8",
        failure_kind="json_decode",
        decode_error="JSONDecodeError",
        body_preview_redacted='{"bad":true}',
    )
    client._json = AsyncMock(side_effect=err)

    with pytest.raises(api.InvalidPayloadError) as raised:
        await client.summary_v2()

    assert raised.value is err


@pytest.mark.asyncio
async def test_session_history_adds_bearer_from_cookie() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR; other=1", eauth=""
    )
    client._json = AsyncMock(return_value={"sessions": []})
    await client.session_history(
        "SN",
        start_date="01-01-2024",
        timezone="UTC",
        request_id="req-1",
        username="2999",
    )
    args, kwargs = client._json.await_args
    assert kwargs["headers"]["Authorization"] == "Bearer BEAR"
    assert kwargs["headers"]["requestid"] == "req-1"
    assert kwargs["headers"]["username"] == "2999"
    assert kwargs["json"]["source"] == "evse"
    assert kwargs["json"]["params"]["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_session_history_falls_back_to_eauth() -> None:
    client = _make_client()
    client.update_credentials(
        cookie="enlighten_manager_token_production=BEAR", eauth="EAUTH"
    )
    client._json = AsyncMock(return_value={"sessions": []})
    await client.session_history("SN", start_date="01-01-2024", end_date="02-01-2024")
    args, kwargs = client._json.await_args
    assert kwargs["headers"]["Authorization"] == "Bearer EAUTH"
    assert kwargs["json"]["params"]["endDate"] == "02-01-2024"


@pytest.mark.asyncio
async def test_session_history_uses_session_id_header() -> None:
    client = _make_client()
    token = _make_token({"data": {"session_id": "SID123"}})
    client.update_credentials(eauth=token)
    client._json = AsyncMock(return_value={"sessions": []})
    await client.session_history("SN", start_date="01-01-2024")
    args, kwargs = client._json.await_args
    assert kwargs["headers"]["e-auth-token"] == "SID123"


@pytest.mark.asyncio
async def test_session_history_filter_criteria_builds_headers() -> None:
    client = _make_client()
    client._json = AsyncMock(return_value={"data": []})
    await client.session_history_filter_criteria(request_id="req-2", username="2999")
    args, kwargs = client._json.await_args
    assert args[0] == "GET"
    assert "filter_criteria" in args[1]
    assert "requestId=req-2" in args[1]
    assert "username=2999" in args[1]
    assert kwargs["headers"]["Authorization"] == "Bearer EAUTH"
    assert kwargs["headers"]["requestid"] == "req-2"
    assert kwargs["headers"]["username"] == "2999"


@pytest.mark.asyncio
async def test_session_history_filter_criteria_uses_default_username() -> None:
    token = _make_token({"user_id": "2999"})
    client = _make_client()
    client.update_credentials(eauth=token)
    client._json = AsyncMock(return_value={"data": []})

    await client.session_history_filter_criteria(request_id="req-3")

    args, kwargs = client._json.await_args
    assert "requestId=req-3" in args[1]
    assert "username=2999" in args[1]
    assert kwargs["headers"]["username"] == "2999"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("charge_mode", ("SN",), {}),
        ("set_charge_mode", ("SN", "MANUAL_CHARGING"), {}),
        ("green_charging_settings", ("SN",), {}),
        ("set_green_battery_setting", ("SN",), {"enabled": True}),
        ("get_schedules", ("SN",), {}),
        ("patch_schedules", ("SN",), {"server_timestamp": "ts", "slots": []}),
        ("patch_schedule_states", ("SN",), {"slot_states": {"1": True}}),
        ("patch_schedule", ("SN", "1", {}), {}),
        ("create_schedule", ("SN", {"id": "1"}), {}),
        ("delete_schedule", ("SN", "1"), {}),
    ],
)
async def test_scheduler_endpoints_wrap_unavailable(
    monkeypatch, method, args, kwargs
) -> None:
    client = _make_client()
    err = _make_cre(503, "Service Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(api.SchedulerUnavailable):
        await getattr(client, method)(*args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("charge_mode", ("SN",), {}),
        ("set_charge_mode", ("SN", "MANUAL_CHARGING"), {}),
        ("green_charging_settings", ("SN",), {}),
        ("set_green_battery_setting", ("SN",), {"enabled": True}),
        ("get_schedules", ("SN",), {}),
        ("patch_schedules", ("SN",), {"server_timestamp": "ts", "slots": []}),
        ("patch_schedule_states", ("SN",), {"slot_states": {"1": True}}),
        ("patch_schedule", ("SN", "1", {}), {}),
        ("create_schedule", ("SN", {"id": "1"}), {}),
        ("delete_schedule", ("SN", "1"), {}),
    ],
)
async def test_scheduler_endpoints_reraise_non_scheduler_error(
    monkeypatch, method, args, kwargs
) -> None:
    client = _make_client()
    err = _make_cre(400, "Bad Request")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(aiohttp.ClientResponseError):
        await getattr(client, method)(*args, **kwargs)


@pytest.mark.asyncio
async def test_auth_settings_reraise_non_service_errors(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(400, "Bad Request")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.charger_auth_settings("SN")

    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.set_app_authentication("SN", enabled=False)


@pytest.mark.asyncio
async def test_charger_auth_settings_reraises_retry_error(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_json",
        AsyncMock(side_effect=[api.Unauthorized(), _make_cre(400, "Bad")]),
    )
    with pytest.raises(aiohttp.ClientResponseError) as ctx:
        await client.charger_auth_settings("SN")

    assert ctx.value.status == 400


@pytest.mark.asyncio
async def test_charger_auth_settings_wraps_retry_unavailable(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_json",
        AsyncMock(
            side_effect=[api.Unauthorized(), _make_cre(503, "Service Unavailable")]
        ),
    )
    with pytest.raises(api.AuthSettingsUnavailable):
        await client.charger_auth_settings("SN")


@pytest.mark.asyncio
async def test_charger_auth_settings_unauthorized_without_control_auth_reraises() -> (
    None
):
    client = api.EnphaseEVClient(_DefaultSession(), "SITE", None, "COOKIE")
    client._json = AsyncMock(side_effect=api.Unauthorized())

    with pytest.raises(api.Unauthorized):
        await client.charger_auth_settings("SN")


@pytest.mark.asyncio
async def test_charger_auth_settings_retries_without_auth_on_403(monkeypatch) -> None:
    client = _make_client()
    monkeypatch.setattr(
        client,
        "_json",
        AsyncMock(
            side_effect=[
                _make_cre(403, "Forbidden"),
                {"data": [{"key": AUTH_APP_SETTING, "value": "enabled"}]},
            ]
        ),
    )

    settings = await client.charger_auth_settings("SN")

    assert settings == [{"key": AUTH_APP_SETTING, "value": "enabled"}]
    first_call = client._json.await_args_list[0]
    second_call = client._json.await_args_list[1]
    first_headers = client._today_json_headers()
    first_headers.update(client._control_headers())
    second_headers = client._today_json_headers()
    second_headers["Authorization"] = None
    second_headers["e-auth-token"] = None
    assert first_call.kwargs["headers"] == first_headers
    assert second_call.kwargs["headers"] == second_headers


@pytest.mark.asyncio
async def test_lifetime_energy_reraises_non_service_error(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(400, "Bad Request")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.lifetime_energy()


@pytest.mark.asyncio
async def test_session_history_reraises_non_service_error(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(400, "Bad Request")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(aiohttp.ClientResponseError):
        await client.session_history("SN", start_date="01-01-2024")


@pytest.mark.asyncio
async def test_charger_auth_settings_wraps_unavailable(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(503, "Service Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(api.AuthSettingsUnavailable):
        await client.charger_auth_settings("SN")


@pytest.mark.asyncio
async def test_set_app_authentication_wraps_unavailable(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(503, "Service Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(api.AuthSettingsUnavailable):
        await client.set_app_authentication("SN", enabled=True)


@pytest.mark.asyncio
async def test_lifetime_energy_wraps_unavailable(monkeypatch) -> None:
    client = _make_client()
    err = _make_cre(503, "Service Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(api.SiteEnergyUnavailable):
        await client.lifetime_energy()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [503, 550])
async def test_session_history_wraps_unavailable(monkeypatch, status) -> None:
    client = _make_client()
    err = _make_cre(status, "Service Unavailable")
    monkeypatch.setattr(client, "_json", AsyncMock(side_effect=err))
    with pytest.raises(api.SessionHistoryUnavailable):
        await client.session_history("SN", start_date="01-01-2024")
