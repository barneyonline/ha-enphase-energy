"""Synthetic, offline session-renewal safety regressions."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
import pytest_asyncio
from homeassistant.helpers.redact import async_redact_data
from homeassistant.util import dt as dt_util
from yarl import URL

from custom_components.enphase_ev.api import EnphaseEVClient, Unauthorized
from custom_components.enphase_ev.api_models import AuthTokens
from custom_components.enphase_ev.const import (
    BASE_URL,
    CONF_ACCESS_TOKEN,
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_SESSION_ID,
    OPT_VPP_EVENTS_ENABLED,
    SELF_TOKEN_URL,
)
from custom_components.enphase_ev.diagnostics import TO_REDACT
from custom_components.enphase_ev.vpp_runtime import VppRuntime
from custom_components.enphase_ev.vpp_session import (
    STATE_KEY,
    VppSessionRenewal,
    _cookies,
    _identity,
    _retry_deadline,
)

MANAGER = "enlighten_manager_token_production"


def jwt(*, account="synthetic-account-a", seconds=-60, **claims):
    payload = {
        "user_id": account,
        "exp": dt_util.utcnow().timestamp() + seconds,
        **claims,
    }
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    return f"e30.{encoded}.synthetic"


@pytest_asyncio.fixture
async def context(monkeypatch):
    access = jwt()
    cookie = f"_enlighten_4_session=synthetic-session; {MANAGER}={access}"
    entry = SimpleNamespace(
        data={CONF_COOKIE: cookie, CONF_EAUTH: access, CONF_ACCESS_TOKEN: access},
        options={OPT_VPP_EVENTS_ENABLED: True},
    )
    shared_jar = aiohttp.CookieJar()
    client = EnphaseEVClient(
        SimpleNamespace(cookie_jar=shared_jar),
        "synthetic-site",
        access,
        cookie,
        reauth_callback=AsyncMock(),
    )
    coord = SimpleNamespace(
        config_entry=entry,
        client=client,
        _tokens=AuthTokens(cookie=cookie, access_token=access),
        _endpoint_family_should_run=lambda _: True,
        _note_endpoint_family_failure=lambda *_: None,
        _note_endpoint_family_success=lambda *_: None,
    )
    writes = []

    def store(data, *, reason):
        writes.append((data, reason))
        changed = data != entry.data
        entry.data = data
        return changed

    coord._async_update_config_entry_data_internal = store
    response = SimpleNamespace(
        status=200,
        content_type="application/json",
        headers={},
        payload={"token": jwt(seconds=3600)},
        rotation={},
        error=None,
        started=asyncio.Event(),
        release=None,
    )
    sessions = []
    calls = []

    class Response:
        async def __aenter__(self):
            response.started.set()
            if response.release:
                await response.release.wait()
            if response.error:
                raise response.error
            for key, value in response.rotation.items():
                sessions[-1].cookie_jar.update_cookies(
                    {key: value}, response_url=URL(SELF_TOKEN_URL)
                )
            return response

        async def __aexit__(self, *_):
            pass

    class Session:
        def __init__(self, *, cookie_jar, trust_env):
            assert trust_env is False
            assert cookie_jar is not shared_jar
            self.cookie_jar = cookie_jar
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            assert url == SELF_TOKEN_URL
            assert kwargs["allow_redirects"] is False
            assert kwargs["timeout"].total == 15
            assert set(kwargs["headers"]) == {"Accept", "Referer"}
            return Response()

    async def payload():
        return response.payload

    response.json = payload
    monkeypatch.setattr(
        "custom_components.enphase_ev.vpp_session.aiohttp.ClientSession", Session
    )
    return SimpleNamespace(
        coord=coord,
        response=response,
        calls=calls,
        sessions=sessions,
        writes=writes,
        shared=shared_jar,
        renewal=VppSessionRenewal(coord),
    )


def replace_credentials(ctx, *, cookie=None, access=None):
    coord = ctx.coord
    cookie = cookie if cookie is not None else coord.client._cookie
    access = access if access is not None else coord.client._eauth
    coord.config_entry.data = {
        **coord.config_entry.data,
        CONF_COOKIE: cookie,
        CONF_EAUTH: access,
        CONF_ACCESS_TOKEN: access,
    }
    coord._tokens = AuthTokens(cookie=cookie, access_token=access)
    coord.client.update_credentials(eauth=access or "", cookie=cookie)


async def test_success_rotation_atomic_persistence_and_reload(context):
    ctx = context
    ctx.coord.config_entry.data.update(
        {"auth_block_reason": "preserve", "hems_auth_failure_count": 3}
    )
    ctx.response.rotation = {
        "_enlighten_4_session": "synthetic-rotation",
        MANAGER: ctx.response.payload["token"],
    }
    assert await ctx.renewal.async_prepare()
    data = ctx.coord.config_entry.data
    assert (
        ctx.coord._tokens.access_token
        == data[CONF_EAUTH]
        == data[CONF_ACCESS_TOKEN]
        == ctx.response.payload["token"]
    )
    assert ctx.coord._tokens.cookie == ctx.coord.client._cookie == data[CONF_COOKIE]
    assert data[CONF_SESSION_ID] == "synthetic-rotation"
    assert data["auth_block_reason"] == "preserve"
    assert data["hems_auth_failure_count"] == 3
    assert len(ctx.writes) == 2
    assert len(ctx.calls) == 1
    assert await VppSessionRenewal(ctx.coord).async_prepare()
    assert len(ctx.calls) == 1
    serialized = json.dumps(async_redact_data(dict(data), TO_REDACT))
    for private in (
        "synthetic-session",
        "synthetic-account-a",
        "synthetic-rotation",
        data[CONF_EAUTH],
        data[STATE_KEY]["auth_identifier"],
    ):
        assert private not in serialized
    ctx.coord.client._reauth_cb.assert_not_awaited()


@pytest.mark.parametrize(
    "cookie,access",
    [
        ("", None),
        ("other=synthetic", None),
        ("_enlighten_4_session=synthetic", None),
        ("_enlighten_4_session=synthetic", "expired"),
    ],
)
async def test_missing_session_or_identity_never_requests(context, cookie, access):
    replace_credentials(context, cookie=cookie, access=access or "")
    # Opaque access tokens retain the existing selection policy; omit them here.
    if access == "expired":
        replace_credentials(context, access=jwt(account=None))
    assert not await context.renewal.async_prepare()
    assert not context.calls
    assert not await VppSessionRenewal(context.coord).async_prepare()


@pytest.mark.parametrize(
    "claims",
    [
        {"userId": "synthetic-other"},
        {"data": {"user_id": "synthetic-other"}},
        {"userId": []},
    ],
)
async def test_conflicting_expected_identity(context, claims):
    replace_credentials(context, access=jwt(**claims))
    assert not await context.renewal.async_prepare()
    assert not context.calls


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"token": 5},
        {"token": "malformed"},
        {"token": ""},
        "expired",
        "mismatch",
        "ambiguous",
        "two_tokens",
    ],
)
async def test_bad_token_response_fails_closed(context, payload):
    if payload == "expired":
        payload = {"token": jwt(seconds=-1)}
    elif payload == "mismatch":
        payload = {"token": jwt(account="synthetic-other", seconds=600)}
    elif payload == "ambiguous":
        payload = {"token": jwt(seconds=600, userId="synthetic-other")}
    elif payload == "two_tokens":
        payload = {"token": jwt(seconds=600), "access_token": jwt(seconds=700)}
    context.response.payload = payload
    before = context.coord._tokens
    assert not await context.renewal.async_prepare()
    assert context.coord._tokens is before
    assert not await context.renewal.async_prepare()
    assert len(context.calls) == 1


async def test_returned_cookie_identity_mismatch(context):
    context.response.rotation = {MANAGER: jwt(account="synthetic-other", seconds=600)}
    assert not await context.renewal.async_prepare()
    assert "synthetic-other" not in context.coord.client._cookie


async def test_shared_cookie_jar_is_not_seed_or_destination(context):
    context.shared.update_cookies(
        {
            "_enlighten_4_session": "foreign-session",
            MANAGER: jwt(account="foreign-account", seconds=600),
        },
        response_url=URL(BASE_URL),
    )
    before = str(context.shared.filter_cookies(URL(BASE_URL)))
    assert await context.renewal.async_prepare()
    sent = context.sessions[0].cookie_jar.filter_cookies(URL(SELF_TOKEN_URL))
    assert sent["_enlighten_4_session"].value == "synthetic-session"
    assert str(context.shared.filter_cookies(URL(BASE_URL))) == before


async def test_single_flight(context):
    ctx = context
    ctx.response.release = asyncio.Event()
    tasks = [asyncio.create_task(ctx.renewal.async_prepare()) for _ in range(4)]
    await ctx.response.started.wait()
    assert len(ctx.calls) == 1
    ctx.response.release.set()
    assert all(await asyncio.gather(*tasks))
    assert len(ctx.calls) == 1


@pytest.mark.parametrize("replacement", ["credentials", "entry", "client", "tokens"])
async def test_concurrent_replacement_discards_response(context, replacement):
    ctx = context
    ctx.response.release = asyncio.Event()
    task = asyncio.create_task(ctx.renewal.async_prepare())
    await ctx.response.started.wait()
    if replacement == "credentials":
        replace_credentials(
            ctx,
            cookie="_enlighten_4_session=replacement",
            access=jwt(account="replacement"),
        )
    elif replacement == "entry":
        ctx.coord.config_entry = SimpleNamespace(data=dict(ctx.coord.config_entry.data))
    elif replacement == "client":
        ctx.coord.client.update_credentials(cookie="replacement")
    else:
        ctx.coord._tokens.cookie = "replacement"
    writes = len(ctx.writes)
    ctx.response.release.set()
    assert not await task
    assert len(ctx.writes) == writes


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 403])
async def test_invalid_session_suspends_across_reload(context, status, monkeypatch):
    now = dt_util.utcnow()
    context.response.status = status
    context.response.headers = {"Location": "https://foreign.invalid/login"}
    assert not await context.renewal.async_prepare()
    assert not await VppSessionRenewal(context.coord).async_prepare()
    assert len(context.calls) == 1
    assert context.coord.config_entry.data[STATE_KEY]["suspended"]
    replace_credentials(context, cookie="_enlighten_4_session=explicit-replacement")
    context.response.status = 200
    assert not await context.renewal.async_prepare()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(seconds=901))
    assert await context.renewal.async_prepare()
    assert len(context.calls) == 2


async def test_html_login_wall_suspends(context):
    context.response.content_type = "text/html"
    assert not await context.renewal.async_prepare()
    assert not await VppSessionRenewal(context.coord).async_prepare()
    assert len(context.calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        429,
        500,
        aiohttp.ClientError("synthetic-secret"),
        TimeoutError(),
        ValueError("invalid json"),
    ],
)
async def test_transient_failure_cooldown_and_reload(context, failure, monkeypatch):
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    if isinstance(failure, int):
        context.response.status = failure
        context.response.headers = {"Retry-After": "3600"}
        delay = 3600
    else:
        context.response.error = failure
        delay = 900
    assert not await context.renewal.async_prepare()
    runtime = VppSessionRenewal(context.coord)
    assert not await runtime.async_prepare()
    assert len(context.calls) == 1
    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(seconds=delay - 1))
    assert not await runtime.async_prepare()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(seconds=delay + 1))
    assert not await runtime.async_prepare()
    assert len(context.calls) == 2
    context.coord.client._reauth_cb.assert_not_awaited()


async def test_stale_response_expired_in_flight(context, monkeypatch):
    now = dt_util.utcnow()
    context.response.payload = {"token": jwt(seconds=10)}
    context.response.release = asyncio.Event()
    task = asyncio.create_task(context.renewal.async_prepare())
    await context.response.started.wait()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(seconds=20))
    context.response.release.set()
    assert not await task


async def test_runtime_failed_renewal_skips_all_gs_calls(context):
    context.response.status = 401
    context.coord.client._json = AsyncMock()
    runtime = VppRuntime(context.coord)
    await runtime.async_refresh()
    await runtime.async_refresh()
    assert len(context.calls) == 1
    context.coord.client._json.assert_not_awaited()
    context.coord.client._reauth_cb.assert_not_awaited()


async def test_valid_token_gs_rejection_never_renews(context):
    replace_credentials(context, access=jwt(seconds=600))
    context.coord.client._json = AsyncMock(
        side_effect=Unauthorized("synthetic rejection")
    )
    runtime = VppRuntime(context.coord)
    await runtime.async_refresh()
    assert not context.calls
    assert context.coord.client._json.await_args.kwargs["allow_reauth"] is False
    assert (
        context.coord.client._json.await_args.kwargs["use_cookie_header_only"] is True
    )
    context.coord.client._reauth_cb.assert_not_awaited()


async def test_disabled_or_ineligible_poll_does_not_renew(context):
    runtime = VppRuntime(context.coord)
    context.coord.config_entry.options = {OPT_VPP_EVENTS_ENABLED: False}
    await runtime.async_refresh()
    context.coord.config_entry.options = {OPT_VPP_EVENTS_ENABLED: True}
    context.coord._endpoint_family_should_run = lambda _: False
    await runtime.async_refresh()
    assert not context.calls


async def test_missing_entry_or_inconsistent_snapshot(context):
    context.coord.config_entry = None
    assert not await context.renewal.async_prepare()
    assert not context.calls


@pytest.mark.parametrize(
    "target", ["entry_cookie", "entry_token", "coordinator_cookie", "coordinator_token"]
)
async def test_snapshot_consistency(context, target):
    if target == "entry_cookie":
        context.coord.config_entry.data[CONF_COOKIE] = "different"
    elif target == "entry_token":
        context.coord.config_entry.data[CONF_EAUTH] = "different"
    elif target == "coordinator_cookie":
        context.coord._tokens.cookie = "different"
    else:
        context.coord._tokens.access_token = "different"
    assert not await context.renewal.async_prepare()
    assert not context.calls


@pytest.mark.parametrize("after_response", [False, True])
async def test_failed_persistence_never_adopts(context, after_response):
    original = context.coord._async_update_config_entry_data_internal

    def store(data, *, reason):
        if after_response and not context.calls:
            return original(data, reason=reason)
        return False

    context.coord._async_update_config_entry_data_internal = store
    before = context.coord._tokens
    assert not await context.renewal.async_prepare()
    assert context.coord._tokens is before
    assert len(context.calls) == int(after_response)


@pytest.mark.parametrize(
    "claims,expected",
    [
        ({"user_id": None}, None),
        ({"user_id": True}, None),
        ({"user_id": ""}, None),
        ({"user_id": 5}, "5"),
        ({"data": []}, "synthetic-account-a"),
        ({"userId": "synthetic-account-a"}, "synthetic-account-a"),
        ({"user_id": None, "data": {"user_id": "synthetic-account-a"}}, None),
    ],
)
def test_identity_guards(claims, expected):
    assert _identity(jwt(**claims)) == expected
    assert _identity("bad") is None
    assert _identity("e30.e30.synthetic") is None


@pytest.mark.parametrize(
    "header", ["a=1; a=2", "bad", "=value", "bad@=1", 'a="unterminated', "a=1; invalid"]
)
def test_malformed_cookie_snapshot(header):
    assert not _cookies(header)


@pytest.mark.parametrize(
    "value",
    [None, "invalid", "NaN", "-1", "600", "3600", "Wed, 01 Jan 2031 00:00:00 GMT"],
)
def test_retry_after(value):
    now = datetime(2030, 1, 1, tzinfo=UTC).timestamp()
    deadline = _retry_deadline(value, now)
    assert deadline >= now + 900
    if value == "3600":
        assert deadline == now + 3600


async def test_renewal_then_runtime_uses_raw_stateless_gs_headers(context):
    ctx = context
    ctx.coord.client._json = AsyncMock(return_value={"data": None})
    await VppRuntime(ctx.coord).async_refresh()
    assert len(ctx.calls) == 1
    call = ctx.coord.client._json.await_args
    headers = call.kwargs["headers"]()
    assert headers["Authorization"] == ctx.response.payload["token"]
    assert all(
        headers[name] is None for name in ("Cookie", "e-auth-token", "X-CSRF-Token")
    )
    assert call.kwargs["allow_reauth"] is False
    assert call.kwargs["use_cookie_header_only"] is True


@pytest.mark.parametrize("failure", [False, True])
async def test_removed_entry_discards_success_or_exception(context, failure):
    ctx = context
    ctx.response.release = asyncio.Event()
    if failure:
        ctx.response.error = aiohttp.ClientError("synthetic private error")
    task = asyncio.create_task(ctx.renewal.async_prepare())
    await ctx.response.started.wait()
    ctx.coord.config_entry = None
    ctx.response.release.set()
    assert not await task
    assert len(ctx.writes) == 1


async def test_reload_during_request_and_explicit_replacement(context):
    ctx = context
    ctx.response.release = asyncio.Event()
    task = asyncio.create_task(ctx.renewal.async_prepare())
    await ctx.response.started.wait()
    assert not await VppSessionRenewal(ctx.coord).async_prepare()
    assert len(ctx.calls) == 1
    ctx.response.release.set()
    assert await task


async def test_account_conflict_between_entry_aliases(context):
    context.coord.config_entry.data[CONF_ACCESS_TOKEN] = jwt(account="synthetic-other")
    assert not await context.renewal.async_prepare()
    assert not context.calls


async def test_failed_single_flight(context):
    context.response.status = 429
    context.response.release = asyncio.Event()
    tasks = [asyncio.create_task(context.renewal.async_prepare()) for _ in range(4)]
    await context.response.started.wait()
    context.response.release.set()
    assert not any(await asyncio.gather(*tasks))
    assert len(context.calls) == 1


async def test_complete_diagnostic_export_redacts_renewal_binding(
    context, monkeypatch, hass
):
    from custom_components.enphase_ev import diagnostics

    assert await context.renewal.async_prepare()
    entry = context.coord.config_entry
    entry.entry_id = "synthetic-entry"
    entry.data["site_id"] = "synthetic-site"
    monkeypatch.setattr(
        diagnostics, "get_runtime_data", lambda _: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        diagnostics, "async_load_status_history", AsyncMock(return_value=[])
    )
    output = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(output)
    for private in (
        entry.data[CONF_COOKIE],
        entry.data[CONF_EAUTH],
        entry.data[STATE_KEY]["auth_identifier"],
        "synthetic-account-a",
        "synthetic-session",
        "synthetic-site",
    ):
        assert private not in serialized
    assert (
        output["entry_data"][STATE_KEY]["next_attempt"]
        == entry.data[STATE_KEY]["next_attempt"]
    )


@pytest.mark.parametrize("key", ["email", "password", "session_id"])
async def test_explicit_account_credential_change_discards_response(context, key):
    ctx = context
    ctx.response.release = asyncio.Event()
    task = asyncio.create_task(ctx.renewal.async_prepare())
    await ctx.response.started.wait()
    ctx.coord.config_entry.data = {
        **ctx.coord.config_entry.data,
        key: "synthetic-replacement",
    }
    ctx.response.release.set()
    assert not await task
    assert len(ctx.writes) == 1


async def test_successful_renewal_cooldown_survives_token_expiry_and_reload(
    context, monkeypatch
):
    ctx = context
    now = dt_util.utcnow()
    ctx.response.payload = {"token": jwt(seconds=30)}
    assert await ctx.renewal.async_prepare()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(seconds=31))
    assert not await VppSessionRenewal(ctx.coord).async_prepare()
    assert len(ctx.calls) == 1


async def test_retry_after_survives_explicit_credential_replacement(
    context, monkeypatch
):
    now = dt_util.utcnow()
    context.response.status = 429
    context.response.headers = {"Retry-After": "3600"}
    assert not await context.renewal.async_prepare()
    replace_credentials(context, cookie="_enlighten_4_session=synthetic-replacement")
    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(seconds=901))
    assert not await VppSessionRenewal(context.coord).async_prepare()
    assert len(context.calls) == 1
    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(seconds=3601))
    assert not await VppSessionRenewal(context.coord).async_prepare()
    assert len(context.calls) == 2


async def test_preflight_keeps_last_request_credential_diagnostics(context):
    previous = {"source": "stored_access", "account_match": "unknown"}
    context.coord.client._vpp_credential_diagnostics = previous
    context.response.status = 401
    assert not await context.renewal.async_prepare()
    assert context.coord.client._vpp_credential_diagnostics is previous
