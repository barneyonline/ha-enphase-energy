"""Cross-boundary authentication and concurrent publication regressions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.enphase_ev import api
from custom_components.enphase_ev.auth_refresh_state import AuthRefreshState
from custom_components.enphase_ev.refresh_runner import merge_warmup_enrichment
from custom_components.enphase_ev.state_models import EndpointFamilyHealth

from .test_api_client_methods import _FakeResponse, _FakeSession


@pytest.mark.asyncio
async def test_poll_401_refreshes_credentials_without_waiting_on_poll_lock(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._email = "synthetic@example.test"
    coord._remember_password = True
    coord._stored_password = "synthetic-password"
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(status=200, json_body={"data": []}),
        ]
    )
    coord.client = api.EnphaseEVClient(session, "SITE", "old", "session=old")
    coord.client.set_reauth_callback(coord._handle_client_unauthorized)

    async def login() -> bool:
        coord.client.update_credentials(eauth="new", cookie="session=new")
        return True

    login_mock = AsyncMock(side_effect=login)
    coord.auth_refresh_runtime.async_run_auto_refresh = login_mock

    async def refresh(_context):
        await coord.client.status()
        return {"EVSE": {"charging": False}}

    coord._async_update_data_impl = refresh
    result = await asyncio.wait_for(coord._async_update_data(), timeout=1)
    assert result["EVSE"]["charging"] is False
    login_mock.assert_awaited_once()
    assert [call[2]["headers"]["e-auth-token"] for call in session.calls] == [
        "old",
        "new",
    ]
    assert [call[2]["headers"]["Cookie"] for call in session.calls] == [
        "session=old",
        "session=new",
    ]


@pytest.mark.asyncio
async def test_auth_waiters_share_login_and_cancellation_isolated(coordinator_factory):
    coord = coordinator_factory()
    coord._email = "synthetic@example.test"
    coord._remember_password = True
    coord._stored_password = "synthetic-password"
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def login() -> bool:
        entered.set()
        await finish.wait()
        return True

    coord.auth_refresh_runtime.async_run_auto_refresh = AsyncMock(side_effect=login)
    first = asyncio.create_task(coord._attempt_auto_refresh())
    await asyncio.wait_for(entered.wait(), timeout=1)
    second = asyncio.create_task(coord._attempt_auto_refresh())
    manual = asyncio.create_task(coord.async_try_reauth_now())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    assert await asyncio.wait_for(second, timeout=1) is True
    assert (await asyncio.wait_for(manual, timeout=1)).success is True
    coord.auth_refresh_runtime.async_run_auto_refresh.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["scheduler", "text", "tariff", "battery"])
async def test_endpoint_policy_rebuilt_after_authentication(family):
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(status=200, json_body={"data": {}}, text_body="telemetry"),
        ]
    )
    client = api.EnphaseEVClient(
        session, "SITE", "old", "session=old; bp-xsrf-token=old-xsrf"
    )

    async def reauth():
        client.update_credentials(
            eauth="new", cookie="session=new; bp-xsrf-token=new-xsrf"
        )
        return True

    client.set_reauth_callback(reauth)
    if family == "scheduler":
        await client.get_schedules("EVSE")
    elif family == "text":
        assert await client.ac_battery_detail_page("BATTERY") == "telemetry"
    elif family == "tariff":
        await client.site_tariff_billing_details()
    else:
        await client.battery_site_settings()
    headers = session.calls[-1][2]["headers"]
    if family == "battery":
        assert "e-auth-token" not in headers
        assert "X-Requested-With" not in headers
    else:
        assert headers.get("e-auth-token") == "new"
    assert "session=new" in headers["Cookie"]
    if family == "scheduler":
        assert headers["Authorization"] == "Bearer new"


def test_warmup_merge_preserves_concurrent_changes_and_removals():
    baseline = {"A": {"charging": False, "mode": "old", "remove": 1}, "B": {"x": 1}}
    enriched = {
        "A": {"charging": False, "mode": "warm", "added": 2},
        "B": {"x": 2},
        "C": {"new": 3},
    }
    current = {"A": {"charging": True, "mode": "command", "remove": 1}}
    result = merge_warmup_enrichment(baseline, enriched, current)
    assert result == {
        "A": {"charging": True, "mode": "command", "added": 2},
        "C": {"new": 3},
    }
    assert current["A"]["remove"] == 1


@pytest.mark.asyncio
async def test_warmup_publishes_current_charger_state_between_stages(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord.data = {"EVSE": {"charging": False}}

    async def power():
        coord.data = {"EVSE": {"charging": True}}

    coord._startup_power_task = asyncio.create_task(power())
    coord.refresh_runner.async_run_refresh_plan = AsyncMock()
    publications = []

    def publish(data):
        publications.append(deepcopy(data))
        coord.data = data

    coord.async_set_updated_data = Mock(side_effect=publish)
    await coord.refresh_runner.async_startup_warmup_runner()
    assert publications
    assert all(data["EVSE"]["charging"] is True for data in publications)


def test_observation_timestamp_excluded_but_command_auth_health_changes_publish(
    coordinator_factory,
):
    coord = coordinator_factory()
    before = {"EVSE": {"charging": False, "fetched_at_utc": "old"}}
    coord.async_set_updated_data(before)
    snapshot = coord.integration_snapshot
    coord.async_set_updated_data({"EVSE": {"charging": False, "fetched_at_utc": "new"}})
    assert coord.integration_snapshot == snapshot
    assert coord.data["EVSE"]["fetched_at_utc"] == "new"
    coord.evse_runtime.state._desired_charging["EVSE"] = True
    coord.async_set_updated_data(coord.data)
    assert coord.integration_snapshot != snapshot
    snapshot = coord.integration_snapshot
    coord.auth_refresh_runtime.state._auth_refresh_last_failure_reason = (
        "invalid_credentials"
    )
    coord.async_set_updated_data(coord.data)
    assert coord.integration_snapshot != snapshot
    snapshot = coord.integration_snapshot
    coord._endpoint_family_health["battery"] = EndpointFamilyHealth(
        degraded=True, cache_stale=True
    )
    coord.async_set_updated_data(coord.data)
    assert coord.integration_snapshot != snapshot
    assert coord.auth_state is coord.auth_refresh_runtime.state
    assert isinstance(coord.auth_state, AuthRefreshState)


@pytest.mark.asyncio
async def test_refresh_callback_contract_rejects_nonawaitable(coordinator_factory):
    coord = coordinator_factory()
    with pytest.raises(TypeError):
        await coord.refresh_runner.async_run_refresh_call(
            "broken_callback_s", "broken callback", lambda: None
        )
    assert coord.last_failure_source == "refresh_stage"
    assert coord.last_failure_endpoint == "broken_callback_s"


@pytest.mark.asyncio
async def test_text_request_awaits_header_factory_on_each_auth_attempt():
    session = _FakeSession(
        [
            _FakeResponse(status=401, json_body={}),
            _FakeResponse(status=200, json_body={}, text_body="telemetry"),
        ]
    )
    client = api.EnphaseEVClient(session, "SITE", "old", "session=old")

    async def headers():
        await asyncio.sleep(0)
        return client._today_headers()

    async def reauth():
        client.update_credentials(eauth="new", cookie="session=new")
        return True

    client.set_reauth_callback(reauth)
    assert await client._text("GET", api.BASE_URL, headers=headers) == "telemetry"
    assert [call[2]["headers"]["e-auth-token"] for call in session.calls] == [
        "old",
        "new",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["session", "secondary"])
async def test_direct_warmup_helpers_preserve_concurrent_control(
    coordinator_factory, family
):
    coord = coordinator_factory()
    coord.async_set_updated_data({"EVSE": {"charging": False}})

    async def concurrent_update(*args, **kwargs):
        coord.async_set_updated_data({"EVSE": {"charging": True}})
        return {"EVSE": []} if family == "session" else {}

    if family == "session":
        coord._async_enrich_sessions = AsyncMock(side_effect=concurrent_update)
        coord._sum_session_energy = Mock(return_value=0)
        coord._sync_session_history_issue = Mock()
        await coord.refresh_runner.async_refresh_session_state_for_warmup()
        assert coord.data["EVSE"]["energy_today_sessions_kwh"] == 0
    else:
        coord.iter_serials = Mock(return_value=["EVSE"])
        coord.evse_runtime.async_resolve_charge_modes = AsyncMock(
            side_effect=concurrent_update
        )
        coord.evse_runtime.async_resolve_green_battery_settings = AsyncMock(
            return_value={}
        )
        coord.evse_runtime.async_resolve_auth_settings = AsyncMock(return_value={})
        coord.evse_runtime.async_resolve_charger_config = AsyncMock(return_value={})
        await coord.refresh_runner.async_refresh_secondary_evse_state_for_warmup()
    assert coord.data["EVSE"]["charging"] is True
