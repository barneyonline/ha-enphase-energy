"""Offline regressions for VPP credential selection and authorization isolation.

Unsigned JWT-shaped strings and an in-process fake request function only.
The fake server's token policy does not establish the Enphase service contract.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

from custom_components.enphase_ev import api
from custom_components.enphase_ev.api_client.errors import HttpUnauthorized
from custom_components.enphase_ev.const import DOMAIN, OPT_VPP_EVENTS_ENABLED
from custom_components.enphase_ev.coordinator import RefreshPipelineContext
from custom_components.enphase_ev.refresh_plan import (
    RefreshPlan,
    RefreshStage,
    object_method_task,
)
from .test_api_helpers import _make_token
from .test_vpp_api import ENROLLMENT_ID, PROGRAM_ID, _Response


class _FakeResponse(_Response):
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return "synthetic denial"


def _fake_client(mode, now, denied_status=401, reject_all=False, valid_for=7200):
    expiry = int(now.timestamp())
    old = _make_token(
        {
            "exp": expiry - 60 if mode == "expired" else expiry + 3600,
            "probe": "OLD",
            "user_id": "synthetic-account-a",
        }
    )
    new = _make_token(
        {"exp": expiry + valid_for, "probe": "NEW", "user_id": "synthetic-account-a"}
    )
    fallback = _make_token(
        {
            "exp": expiry + valid_for,
            "probe": "FALLBACK",
            "user_id": "synthetic-account-a",
        }
    )
    jar = aiohttp.CookieJar()
    if mode == "jar":
        jar.update_cookies(
            {"enlighten_manager_token_production": new, "session": "synthetic-session"},
            URL(api.BASE_URL),
        )
    shared = SimpleNamespace(
        cookie_jar=jar,
        request=MagicMock(
            side_effect=AssertionError("shared session must not send VPP")
        ),
    )
    accepted_tokens = {new} if mode == "jar" else {new, fallback}

    def request(method, url, *, headers, **kwargs):
        assert method == "GET"
        assert URL(url).host == URL(api.GS_BASE_URL).host
        assert not any(
            name in headers
            for name in ("Cookie", "e-auth-token", "X-CSRF-Token", "X-Requested-With")
        )
        if reject_all or headers.get("Authorization") not in accepted_tokens:
            response = _FakeResponse(denied_status, {"ignored": "synthetic denial"})
            response.headers = {
                "Retry-After": "7200",
                "Set-Cookie": "synthetic-private",
            }
            return response
        if "/enrolled/" in url:
            return _FakeResponse(200, {"data": ENROLLMENT_ID})
        if "/enrollment/" in url:
            return _FakeResponse(200, {"data": {"program_id": PROGRAM_ID}})
        assert "/events/get" in url
        return _FakeResponse(200, {"data": []})

    stateless = SimpleNamespace(
        cookie_jar=aiohttp.DummyCookieJar(), request=MagicMock(side_effect=request)
    )
    reauth = AsyncMock(return_value=True)
    client = api.EnphaseEVClient(
        shared,
        "1234567",
        fallback,
        f"session=synthetic-session; enlighten_manager_token_production={old}",
        cookie_header_session=stateless,
        reauth_callback=reauth,
    )
    return client, shared, stateless, reauth, old, new, fallback


@pytest.mark.parametrize("mode", ["jar", "expired"])
@pytest.mark.parametrize(
    "method,args",
    [
        ("vpp_enrollment_id", ()),
        ("vpp_enrollment_details", (ENROLLMENT_ID,)),
        ("vpp_events", (PROGRAM_ID,)),
    ],
)
async def test_request_selects_current_eligible_vpp_token(
    mode, method, args, monkeypatch
):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    client, shared, stateless, reauth, old, new, fallback = _fake_client(mode, now)
    if mode == "jar":
        battery_cookies = api._cookie_map_from_header(client._battery_config_cookie())
        assert battery_cookies["enlighten_manager_token_production"] == new
        assert client.scheduler_bearer() == old
        assert api._jwt_user_id(old) == api._jwt_user_id(new)
    else:
        assert (
            api._decode_jwt_exp(old) < now.timestamp() < api._decode_jwt_exp(fallback)
        )
    assert isinstance(await getattr(client, method)(*args), dict)
    assert stateless.request.call_args.kwargs["headers"]["Authorization"] == (
        new if mode == "jar" else fallback
    )
    assert client.scheduler_bearer() == old
    assert client._control_headers()["Authorization"] == f"Bearer {old}"
    reauth.assert_not_awaited()
    shared.request.assert_not_called()
    # The callable VPP headers observe updates without altering global selection.
    client.update_credentials(eauth=new)
    assert isinstance(await getattr(client, method)(*args), dict)
    assert stateless.request.call_args.kwargs["headers"]["Authorization"] == new
    if mode == "jar":
        client.update_credentials(
            cookie=f"session=synthetic-session; enlighten_manager_token_production={new}"
        )
    else:
        client.update_credentials(eauth=fallback, cookie="session=synthetic-session")
    assert isinstance(await getattr(client, method)(*args), dict)
    reauth.assert_not_awaited()
    assert stateless.request.call_count == 3


async def test_shared_jar_is_not_an_account_bound_token_source(monkeypatch):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    client, shared, _stateless, _reauth, old, _new, _fallback = _fake_client("jar", now)
    other_account = _make_token(
        {"exp": int(now.timestamp()) + 7200, "user_id": "synthetic-account-b"}
    )
    shared.cookie_jar.update_cookies(
        {"enlighten_manager_token_production": other_account}, URL(api.BASE_URL)
    )
    assert api._jwt_user_id(old) != api._jwt_user_id(other_account)
    assert client._vpp_headers()["Authorization"] == old
    # Cookie merging alone does not establish that a token belongs to this account.
    merged = api._cookie_map_from_header(client._battery_config_cookie())
    assert merged["enlighten_manager_token_production"] == other_account


@pytest.mark.parametrize("stored_manager", [True, False])
@pytest.mark.parametrize(
    "method,args",
    [
        ("vpp_enrollment_id", ()),
        ("vpp_enrollment_details", (ENROLLMENT_ID,)),
        ("vpp_events", (PROGRAM_ID,)),
    ],
)
async def test_updated_credentials_are_not_overridden_by_retained_jar_token(
    monkeypatch, stored_manager, method, args
):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    client, shared, stateless, reauth, old, new, fallback = _fake_client("jar", now)
    shared.cookie_jar.update_cookies(
        {"enlighten_manager_token_production": old}, URL(api.BASE_URL)
    )
    cookie = "session=synthetic-session"
    if stored_manager:
        cookie += f"; enlighten_manager_token_production={new}"
    client.update_credentials(eauth=new, cookie=cookie)

    for _ in range(2):
        # Reapplying the same credentials must not re-enable the retained token.
        client.update_credentials(eauth=new, cookie=cookie)
        assert isinstance(await getattr(client, method)(*args), dict)
        assert stateless.request.call_args.kwargs["headers"]["Authorization"] == new
    assert (
        shared.cookie_jar.filter_cookies(URL(api.BASE_URL))[
            "enlighten_manager_token_production"
        ].value
        == old
    )
    assert client.scheduler_bearer() == (new if stored_manager else None)
    assert client._control_headers()["Authorization"] == f"Bearer {new}"
    reauth.assert_not_awaited()
    shared.request.assert_not_called()

    # A later eligible jar rotation can be adopted without a credential update.
    shared.cookie_jar.update_cookies(
        {"enlighten_manager_token_production": fallback}, URL(api.BASE_URL)
    )
    assert client._vpp_headers()["Authorization"] == fallback


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize(
    "method,args",
    [
        ("vpp_enrollment_id", ()),
        ("vpp_enrollment_details", (ENROLLMENT_ID,)),
        ("vpp_events", (PROGRAM_ID,)),
    ],
)
async def test_real_request_error_path_preserves_status_without_private_metadata(
    coordinator_factory, status, method, args, monkeypatch
):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    client, shared, stateless, reauth, *_tokens = _fake_client(
        "expired", now, denied_status=status, reject_all=True
    )
    error_type = api.Unauthorized if status == 401 else aiohttp.ClientResponseError
    with pytest.raises(error_type) as caught:
        await getattr(client, method)(*args)
    if status == 401:
        assert isinstance(caught.value, HttpUnauthorized)
        assert caught.value.status == 401
        assert caught.value.__dict__ == {"retry_after": "7200"}
    coord = coordinator_factory(client=client)
    safe = coord.vpp_runtime._safe_failure("VPP unavailable", caught.value)
    assert coord._endpoint_family_status_from_error(safe) == status
    assert str(safe) == "VPP unavailable" or "VPP unavailable" in str(safe)
    assert "synthetic-private" not in str(safe)
    assert not getattr(safe, "headers", {}).get("Set-Cookie")
    assert safe.headers["Retry-After"] == "7200"
    reauth.assert_not_awaited()
    shared.request.assert_not_called()
    assert stateless.request.call_count == 1


async def test_text_request_401_preserves_safe_status_without_reauth(monkeypatch):
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    client, shared, stateless, reauth, *_tokens = _fake_client(
        "expired", now, reject_all=True
    )
    with pytest.raises(api.Unauthorized) as caught:
        await client._text_response(
            "GET",
            api.GS_BASE_URL + "/synthetic-probe",
            headers=client._vpp_headers,
            allow_reauth=False,
            use_cookie_header_only=True,
        )
    assert isinstance(caught.value, HttpUnauthorized)
    assert caught.value.status == 401
    assert caught.value.__dict__ == {}
    assert str(caught.value) == ""
    reauth.assert_not_awaited()
    shared.request.assert_not_called()
    assert stateless.request.call_count == 1


@pytest.mark.parametrize("mode", ["jar", "expired"])
async def test_actual_401_pipeline_cooldown_and_credential_update_recovery(
    hass, coordinator_factory, monkeypatch, mode
):
    now = [datetime(2026, 10, 13, 12, tzinfo=UTC)]
    clock = [1000.0]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.time",
        SimpleNamespace(monotonic=lambda: clock[0], time=lambda: now[0].timestamp()),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.vpp_runtime.time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    client, shared, stateless, reauth, old, new, fallback = _fake_client(
        mode, now[0], valid_for=10800
    )
    client.update_credentials(
        eauth="SYNTHETIC-REJECTED",
        cookie="enlighten_manager_token_production=SYNTHETIC-REJECTED",
    )
    coord = coordinator_factory(client=client)
    entry = MockConfigEntry(
        domain=DOMAIN, data={}, options={OPT_VPP_EVENTS_ENABLED: True}
    )
    entry.add_to_hass(hass)
    coord.config_entry = entry
    coord.update_interval = None
    plan = RefreshPlan(
        stages=(
            RefreshStage(
                parallel_tasks=(
                    object_method_task(
                        "vpp_s", "VPP events", "vpp_runtime", "async_refresh"
                    ),
                )
            ),
        )
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.build_followup_plan",
        lambda *_args, **_kwargs: plan,
    )
    context = RefreshPipelineContext(
        started_mono=clock[0],
        refresh_started_utc=now[0],
        phase_timings={},
        fallback_data=coord.data,
        first_refresh=False,
    )
    coord._record_status_refresh_success(context)
    await coord._async_run_post_status_refresh_pipeline(context)
    health = coord._endpoint_family_state("vpp_enrollment")
    assert health.consecutive_failures == 1
    assert health.cooldown_active is True
    assert health.last_status == 401
    assert "VPP enrollment unavailable" in health.last_error
    assert "SYNTHETIC-REJECTED" not in health.last_error
    retry_delay = health.next_retry_mono - clock[0]
    assert retry_delay == 7200
    assert coord.vpp_runtime.snapshot.enrollment_state == "unknown"
    assert coord.vpp_runtime.available is False
    assert coord._unauth_errors == 0
    assert coord.last_update_success is True
    assert coord.last_success_utc == now[0]
    assert stateless.request.call_count == 1
    assert "/enrolled/" in stateless.request.call_args.args[1]
    assert old not in health.last_error
    reauth.assert_not_awaited()
    shared.request.assert_not_called()
    # A successful credential update does not implicitly bypass family cooldown.
    if mode == "jar":
        client.update_credentials(
            eauth=fallback,
            cookie=f"session=synthetic-session; enlighten_manager_token_production={new}",
        )
    else:
        client.update_credentials(eauth=fallback, cookie="session=synthetic-session")
    await coord._async_run_post_status_refresh_pipeline(context)
    assert stateless.request.call_count == 1
    clock[0] += retry_delay + 1
    now[0] += timedelta(seconds=retry_delay + 1)
    await coord._async_run_post_status_refresh_pipeline(context)
    assert stateless.request.call_count == 4  # lookup, details, events after expiry
    assert health.consecutive_failures == 0
    assert health.cooldown_active is False
    assert health.support_state == "supported"
    assert coord.vpp_runtime.available is True
    assert reauth.await_count == 0
    assert coord._unauth_errors == 0
    shared.request.assert_not_called()
