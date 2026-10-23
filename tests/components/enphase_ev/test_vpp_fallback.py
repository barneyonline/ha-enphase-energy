"""Bounded alternate-token recovery uses the real isolated GS transport."""

import json
from datetime import timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.util import dt as dt_util
from yarl import URL

from custom_components.enphase_ev import api
from custom_components.enphase_ev.api_client.errors import HttpUnauthorized
from custom_components.enphase_ev.api_client.vpp_auth import (
    credential_diagnostics,
    select_vpp_fallback,
)
from custom_components.enphase_ev.const import DOMAIN, OPT_VPP_EVENTS_ENABLED
from custom_components.enphase_ev.vpp_runtime import VppRuntime
from .test_vpp_api import _Response
from .test_vpp_token_policy import NOW, EXPIRY, token

MANAGER = "enlighten_manager_token_production"
METHODS = [
    ("vpp_enrollment_id", ()),
    ("vpp_enrollment_details", ("a" * 24,)),
    ("vpp_events", ("b" * 24,)),
]


def client_for(manager, access, *, live=None, status=401, retry_after=None, final=200):
    jar = aiohttp.CookieJar()
    if live:
        jar.update_cookies({MANAGER: live}, URL(api.BASE_URL))
    shared = SimpleNamespace(cookie_jar=jar, request=MagicMock())
    first = _Response()
    first.status = status
    first.headers = {"Set-Cookie": "synthetic-private"}
    if retry_after is not None:
        first.headers["Retry-After"] = retry_after
    second = _Response()
    second.status = final
    transport = SimpleNamespace(
        request=MagicMock(side_effect=[first, second]),
        cookie_jar=aiohttp.DummyCookieJar(),
    )
    client = api.EnphaseEVClient(
        shared,
        "1234567",
        access,
        f"session=synthetic; {MANAGER}={manager}" if manager else "session=synthetic",
        cookie_header_session=transport,
    )
    reauth = AsyncMock()
    client.set_reauth_callback(reauth)
    return client, shared, transport, reauth


@pytest.mark.parametrize("method,args", METHODS)
async def test_401_retries_once_with_same_account_access(
    monkeypatch, method, args, caplog
):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    manager, access = token(probe="manager"), token(probe="access")
    client, shared, transport, reauth = client_for(manager, access)
    before = (client._cookie, client._eauth, list(shared.cookie_jar))
    assert await getattr(client, method)(*args) == {"data": None}
    assert [
        call.kwargs["headers"]["Authorization"]
        for call in transport.request.call_args_list
    ] == [manager, access]
    assert transport.request.call_count == 2
    assert client._request_count == 2
    assert all(
        "Cookie" not in call.kwargs["headers"]
        and "e-auth-token" not in call.kwargs["headers"]
        for call in transport.request.call_args_list
    )
    assert before == (client._cookie, client._eauth, list(shared.cookie_jar))
    safe = credential_diagnostics(client._vpp_credential_diagnostics)
    assert safe["source"] == "stored_access"
    assert safe["fallback_attempted"] is True
    assert manager not in json.dumps(safe) + caplog.text
    assert access not in json.dumps(safe) + caplog.text
    shared.request.assert_not_called()
    reauth.assert_not_awaited()


@pytest.mark.parametrize("final", [200, 401, 403, 429])
async def test_live_manager_rejection_tries_only_one_stored_alternative(
    monkeypatch, final
):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    manager, access, live = (
        token(probe="manager"),
        token(probe="access"),
        token(probe="live"),
    )
    client, shared, transport, reauth = client_for(
        manager, access, live=live, final=final
    )
    if final == 200:
        await client.vpp_enrollment_id()
    else:
        with pytest.raises((api.Unauthorized, aiohttp.ClientResponseError)):
            await client.vpp_enrollment_id()
    assert [
        call.kwargs["headers"]["Authorization"]
        for call in transport.request.call_args_list
    ] == [live, manager]
    assert (
        credential_diagnostics(client._vpp_credential_diagnostics)["source"]
        == "stored_manager"
    )
    shared.request.assert_not_called()
    reauth.assert_not_awaited()


@pytest.mark.parametrize(
    "manager,access,live",
    [
        (token(), token(), None),
        (token(), token("other", probe="access"), None),
        (token(), token(probe="access", data={"userid": "other"}), None),
        (token(), token(account=True), None),
        (token(), token(account=""), None),
        (token(), token(expiry=EXPIRY), None),
        (token(), token(expiry=None), None),
        (token(), "opaque-access", None),
        ("opaque-manager", token(), None),
        (token(), token(), token("other", probe="live")),
        (token(), token(), token(probe="live", expiry=EXPIRY)),
    ],
)
async def test_ineligible_alternatives_do_not_retry(monkeypatch, manager, access, live):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    client, shared, transport, reauth = client_for(manager, access, live=live)
    with pytest.raises(api.Unauthorized):
        await client.vpp_events("b" * 24)
    assert transport.request.call_count == 1
    assert (
        credential_diagnostics(client._vpp_credential_diagnostics)["fallback_attempted"]
        is False
    )
    shared.request.assert_not_called()
    reauth.assert_not_awaited()


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_other_http_failures_never_try_alternate(monkeypatch, status):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    client, _, transport, reauth = client_for(
        token(probe="manager"), token(probe="access"), status=status
    )
    with pytest.raises(aiohttp.ClientResponseError):
        await client.vpp_enrollment_id()
    assert transport.request.call_count == 1
    reauth.assert_not_awaited()


@pytest.mark.parametrize(
    "retry_after,expected",
    [
        ("7200", "7200"),
        (format_datetime(NOW + timedelta(hours=3), usegmt=True), "10800"),
        ("synthetic-private-invalid", None),
        ("", None),
    ],
)
async def test_401_retry_after_blocks_retry_and_survives_sanitization(
    monkeypatch, retry_after, expected
):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    client, _, transport, reauth = client_for(
        token(probe="manager"), token(probe="access"), retry_after=retry_after
    )
    with pytest.raises(HttpUnauthorized) as caught:
        await client.vpp_enrollment_id()
    safe = VppRuntime._safe_failure("VPP unavailable", caught.value)
    assert caught.value.retry_after == expected
    assert safe.headers.get("Retry-After") == expected
    assert "synthetic-private" not in str(caught.value.__dict__) + str(safe)
    assert transport.request.call_count == 1
    reauth.assert_not_awaited()


async def test_new_account_credentials_cannot_be_used_as_a_retry(monkeypatch):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    client, _, transport, _ = client_for(token(probe="manager"), token(probe="access"))
    response = _Response()
    response.status = 401

    def request(*args, **kwargs):
        client.update_credentials(
            eauth=token("other"), cookie=f"{MANAGER}={token('other', probe='manager')}"
        )
        return response

    transport.request.side_effect = request
    with pytest.raises(HttpUnauthorized):
        await client.vpp_enrollment_id()
    assert transport.request.call_count == 1


async def test_fallback_can_use_a_new_same_account_live_token(monkeypatch):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    access, live = token(probe="access"), token(probe="live")
    client, shared, transport, _ = client_for(token(expiry=EXPIRY), access)
    responses = [_Response(), _Response()]
    responses[0].status = 401

    def request(*args, **kwargs):
        shared.cookie_jar.update_cookies({MANAGER: live}, URL(api.BASE_URL))
        return responses.pop(0)

    transport.request.side_effect = request
    await client.vpp_enrollment_id()
    assert [
        call.kwargs["headers"]["Authorization"]
        for call in transport.request.call_args_list
    ] == [access, live]
    assert (
        credential_diagnostics(client._vpp_credential_diagnostics)["source"]
        == "live_manager"
    )


async def test_retained_live_token_and_missing_context_are_not_fallbacks(monkeypatch):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    selected, live = token(), token(probe="live")
    client, shared, transport, _ = client_for(selected, selected, live=live)
    client.update_credentials(eauth=selected, cookie=client._cookie + "; changed=1")
    with pytest.raises(HttpUnauthorized):
        await client.vpp_enrollment_id()
    assert transport.request.call_count == 1
    assert (
        select_vpp_fallback(
            None,
            None,
            shared.cookie_jar,
            rejected_token=selected,
            ignored_live_token=None,
            diagnostics={},
        )
        is None
    )


async def test_standalone_fallback_builds_bounded_diagnostics(monkeypatch):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    rejected, access = token(probe="manager"), token(probe="access")
    snapshot = {}
    assert (
        select_vpp_fallback(
            rejected,
            access,
            None,
            rejected_token=rejected,
            ignored_live_token=None,
            diagnostics=snapshot,
        )
        == access
    )
    safe = credential_diagnostics(snapshot)
    assert safe["candidates"]["stored_access"] == {
        "expiry": "not_expired",
        "eligible": True,
    }
    assert safe["fallback_attempted"] is True


async def test_runtime_recovers_through_alternate_without_global_auth_changes(
    hass, coordinator_factory, monkeypatch
):
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    manager, access = token(probe="manager"), token(probe="access")
    client, shared, transport, reauth = client_for(manager, access)

    class PayloadResponse(_Response):
        def __init__(self, status, payload):
            self.status = status
            self.payload = payload

        async def json(self):
            return self.payload

    def request(method, url, *, headers, **kwargs):
        if headers["Authorization"] == manager:
            return PayloadResponse(401, {})
        assert headers["Authorization"] == access
        if "/enrolled/" in url:
            return PayloadResponse(200, {"data": "a" * 24})
        if "/enrollment/" in url:
            return PayloadResponse(200, {"data": {"program_id": "b" * 24}})
        return PayloadResponse(200, {"data": []})

    transport.request.side_effect = request
    coord = coordinator_factory(client=client)
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={OPT_VPP_EVENTS_ENABLED: True}
    )
    entry.add_to_hass(hass)
    coord.config_entry = entry
    await coord.vpp_runtime.async_refresh()
    assert coord.vpp_runtime.available is True
    assert coord.vpp_runtime.snapshot.enrollment_state == "enrolled"
    assert transport.request.call_count == 6
    assert coord._unauth_errors == 0
    assert coord._endpoint_family_state("vpp_enrollment").consecutive_failures == 0
    assert coord._endpoint_family_state("vpp_events").consecutive_failures == 0
    assert client._eauth == access
    assert MANAGER + "=" + manager in client._cookie
    assert entry.data == {}
    shared.request.assert_not_called()
    reauth.assert_not_awaited()
