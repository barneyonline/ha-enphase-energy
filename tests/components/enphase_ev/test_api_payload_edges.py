"""Optional authentication and sparse payload contracts at the API boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.enphase_ev import api
from custom_components.enphase_ev.api_client import common
from custom_components.enphase_ev.api_client import battery_surface
from custom_components.enphase_ev.api_client import authentication
from tests.components.enphase_ev.test_api_client_methods import (
    _make_client,
    _make_token,
    _RequestMock,
    _make_cre,
)


def test_debug_request_without_query_redacts_site_path():
    client = _make_client()
    assert client._debug_request_context("get", "https://example.test/sites/SITE") == {
        "request": "GET /sites/[site]"
    }


def test_solar_modes_can_be_invalidated_before_timestamped_readback():
    client = _make_client()
    client._charge_mode_solar_modes["SN"] = ("GREEN_CHARGING",)
    assert client.invalidate_charge_mode_solar_modes("SN")
    assert client.charge_mode_solar_modes("SN") is None


@pytest.mark.parametrize(
    "payload", [{"other": 1}, {"data": {}}, {"data": {"sessionId": "sid"}}]
)
def test_jwt_identity_helpers_handle_sparse_claims(payload):
    token = _make_token(payload)
    assert common._jwt_user_id(token) is None
    assert common._jwt_session_id(token) == payload.get("data", {}).get("sessionId")


def test_diagnostic_payload_ignores_blank_site_id():
    assert common._redact_debug_json_body({"value": "safe"}, site_ids=[" "]) == {
        "value": "safe"
    }


def test_scheduler_name_alone_does_not_imply_outage():
    assert not common.is_scheduler_unavailable_error("scheduler ms replied normally")


def test_history_and_hems_headers_without_optional_authentication():
    client = api.EnphaseEVClient(SimpleNamespace(), "SITE", "", "")
    history = client._session_history_headers(None, None)
    assert "requestid" not in history
    assert "Authorization" not in history
    hems = client._hems_headers()
    assert "Authorization" not in hems
    assert "username" not in hems


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"data": None}, {"data": {"latest_power": {}}}]
)
async def test_latest_power_rejects_sparse_payloads(payload):
    client = _make_client()
    client._json = _RequestMock(return_value=payload)
    assert await client.latest_power() is None
    client._json.assert_awaited_once()


async def test_dashboard_summary_without_hems_flag_preserves_unknown_support():
    client = _make_client()
    client._json = _RequestMock(return_value={"name": "site"})
    assert await client.system_dashboard_summary() == {"name": "site"}
    assert client._hems_site_supported is None


async def test_timeseries_uses_explicit_username():
    client = _make_client()
    client._json = _RequestMock(return_value={})
    assert (
        await client.evse_timeseries_daily_energy(
            start_date="2026-10-31", username="user"
        )
        == {}
    )
    assert client._json.await_args.kwargs["headers"]["username"] == "user"


def test_mqtt_truncated_remaining_length_is_ignored():
    assert list(api.EnphaseEVClient._mqtt_packets(b"\x30\x80")) == []


async def test_mqtt_wait_skips_empty_and_unexpected_packets():
    ws = SimpleNamespace(
        receive=AsyncMock(
            side_effect=[
                SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b""),
                SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"\xd0\x00"),
                SimpleNamespace(
                    type=aiohttp.WSMsgType.BINARY, data=b"\x20\x02\x00\x00"
                ),
            ]
        )
    )
    assert await api.EnphaseEVClient._wait_for_mqtt_packet(
        ws, {0x20}, deadline=asyncio.get_running_loop().time() + 5
    ) == (0x20, b"\x00\x00")
    assert ws.receive.await_count == 3


@pytest.mark.parametrize(
    "write_intent, family",
    [
        ("profile_update", "profile"),
        ("battery_settings_update", "battery_settings"),
        ("battery_settings_disclaimer_accept", "battery_settings_disclaimer"),
        ("schedule_update", "schedule"),
    ],
)
@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("stateful", [False, True])
def test_battery_write_candidates_preserve_available_auth_and_source(
    write_intent, family, authenticated, stateful
):
    client = api.EnphaseEVClient(
        SimpleNamespace(), "SITE", "EAUTH" if authenticated else "", ""
    )
    if stateful:
        client._battery_config_write_bases[family] = {"profile": "self-consumption"}
    attempts = client._battery_config_write_attempts(
        family,
        write_intent=write_intent,
        supports_mqtt=False,
        params={},
        json_body={},
    )
    assert attempts
    assert not any(attempt.omit_source for attempt in attempts)
    assert any(
        attempt.auth_mode == battery_surface._BATTERY_CONFIG_VARIANT_PRIMARY
        for attempt in attempts
    )
    if not authenticated:
        assert not any(
            attempt.auth_mode
            in {
                battery_surface._BATTERY_CONFIG_VARIANT_COOKIE_EAUTH,
                battery_surface._BATTERY_CONFIG_VARIANT_MIXED,
            }
            for attempt in attempts
        )


@pytest.mark.parametrize("headers", [None, {}])
def test_xsrf_response_without_headers_has_no_token(headers):
    assert (
        battery_surface._extract_xsrf_from_response_cookies(
            SimpleNamespace(headers=headers)
        )
        is None
    )


async def test_battery_settings_update_can_preserve_devices():
    client = _make_client()
    client._battery_config_write_request = AsyncMock(return_value={"ok": True})
    payload = {"devices": {"iqEvse": {"enabled": True}}}
    assert await client.set_battery_settings_compat(payload, strip_devices=False) == {
        "ok": True
    }
    assert client._battery_config_write_request.call_args.kwargs["json_body"] == payload


def test_cookie_eauth_headers_can_omit_xsrf_for_reads():
    headers = _make_client()._battery_config_cookie_eauth_headers(include_xsrf=False)
    assert "X-XSRF-Token" not in headers
    assert headers["Cookie"] == "COOKIE"


async def test_battery_write_without_body_does_not_add_json_content_type(monkeypatch):
    client = _make_client()
    client._json = _RequestMock(return_value={"ok": True})
    monkeypatch.setattr(client, "_acquire_xsrf_token", AsyncMock())
    assert await client._battery_config_write_request(
        "POST", "https://example.test/write"
    ) == {"ok": True}
    assert "Content-Type" not in client._json.await_args.kwargs["headers"]


async def test_hems_not_found_does_not_mark_site_unsupported():
    client = _make_client()
    client._json = _RequestMock(side_effect=_make_cre(404))
    assert (
        await client.hems_energy_consumption(
            start_at="2026-10-30", end_at="2026-10-31", timezone="UTC", step="day"
        )
        is None
    )
    assert client._hems_site_supported is None


@pytest.mark.parametrize("start", [1700000000, "1700000000", "not-a-timestamp"])
async def test_status_preserves_explicit_start_timestamps(start):
    client = _make_client()
    client._json = _RequestMock(
        return_value={
            "data": {
                "chargers": [
                    {"sn": "SN", "connectors": [{}], "session_d": {"start_time": start}}
                ]
            }
        }
    )
    data = await client.status()
    session = data["evChargerData"][0]["session_d"]
    assert session["start_time"] == start


@pytest.mark.parametrize("include_level", [True, False])
async def test_start_charging_obeys_strict_payload_preference(include_level):
    client = _make_client()
    client._json = _RequestMock(return_value={"status": "ok"})
    assert await client.start_charging(
        "SN", 16, include_level=include_level, strict_preference=True
    ) == {"status": "ok"}
    request_body = client._json.call_args.kwargs.get("json") or {}
    assert ("chargingLevel" in str(request_body)) is include_level


@pytest.mark.parametrize(
    "status, message", [(404, "missing route"), (400, '{"message": "other error"}')]
)
async def test_start_charging_exhausted_variants_raise_original_error(status, message):
    client = _make_client()
    error = _make_cre(status, message)
    client._json = _RequestMock(side_effect=error)
    with pytest.raises(aiohttp.ClientResponseError) as caught:
        await client.start_charging("SN", 16)
    assert caught.value is error


async def test_empty_token_and_site_payloads_preserve_cookie_login(monkeypatch):
    session = SimpleNamespace(cookie_jar=aiohttp.CookieJar())
    request = AsyncMock(side_effect=[{}, {"sites": []}])
    monkeypatch.setattr(authentication, "_request_json", request)
    tokens, sites = await authentication._build_tokens_and_sites(
        session, "user@example.com", "session", timeout=10
    )
    assert tokens.access_token is None
    assert tokens.session_id == "session"
    assert sites == []
    assert request.await_count == 2


async def test_mfa_rejects_non_mapping_response(monkeypatch):
    session = SimpleNamespace(cookie_jar=aiohttp.CookieJar())
    monkeypatch.setattr(
        authentication, "_request_mfa_json", AsyncMock(return_value=["unexpected"])
    )
    with pytest.raises(
        api.EnlightenAuthInvalidOTP, match="Missing MFA session identifier"
    ):
        await api.async_validate_login_otp(session, "user@example.com", "123456", {})
