"""Cloud metadata privacy, access ranking, and optional refresh behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev.cloud_metadata import (
    ACCESS_PRIORITY,
)
from custom_components.enphase_ev.sensor_cloud_metadata import (
    EnphaseAccountAccessSensor,
    EnphaseSiteInformationSensor,
)


def bootstrap(**flags):
    return {
        "app": {
            "timezone": "Australia/Melbourne",
            "user": {
                "id": 1,
                "email": "private@example.invalid",
                "country": "US",
                "isAdmin": False,
                "isInstaller": True,
                "isHost": False,
                "hasConsumptionDataAccess": True,
                **flags,
            },
            "owner": {"id": 1},
        }
    }


@pytest.mark.asyncio
async def test_metadata_refresh_and_sensors(coordinator_factory, monkeypatch):
    coord = coordinator_factory()
    coord.client.site_bootstrap = AsyncMock(return_value=bootstrap())
    coord.client.system_dashboard_summary = AsyncMock(
        return_value={"country_code": "AU", "currency_unit": "AUD"}
    )
    runtime = coord.cloud_metadata_runtime
    await runtime.async_refresh()
    await runtime.async_refresh()
    coord.client.site_bootstrap.assert_awaited_once()
    assert not runtime.refresh_due()
    assert coord.inventory_state.cloud_metadata == {
        "timezone": "Australia/Melbourne",
        "country": "AU",
        "currency": "AUD",
        "access": {
            "administrator": False,
            "installer": True,
            "host": False,
            "consumption_data": True,
            "owner": True,
        },
    }
    site = EnphaseSiteInformationSensor(coord)
    access = EnphaseAccountAccessSensor(coord)
    assert site.native_value == str(coord.site_id)
    assert site.extra_state_attributes == {
        "site_id": str(coord.site_id),
        "timezone": "Australia/Melbourne",
        "country": "AU",
        "currency": "AUD",
    }
    assert access.native_value == "installer"
    assert access.extra_state_attributes == {
        "administrator": False,
        "installer": True,
        "owner": True,
        "host": False,
        "consumption_data": True,
        "viewer": False,
    }
    assert site.device_info["sw_version"] is None
    monkeypatch.setattr(
        type(coord), "last_success_utc", property(lambda _self: None), raising=False
    )
    coord.last_update_success = True
    assert access.available
    coord.last_update_success = False
    assert not access.available
    runtime._next_refresh = 0
    coord.client.site_bootstrap.side_effect = RuntimeError("offline")
    coord.client.system_dashboard_summary.side_effect = RuntimeError("offline")
    await runtime.async_refresh()
    assert not access.available
    assert access.native_value is None
    assert access.extra_state_attributes == {}
    assert site.extra_state_attributes["timezone"] is None


@pytest.mark.parametrize("role", [*ACCESS_PRIORITY, "viewer"])
def test_access_display_priority(coordinator_factory, role):
    coord = coordinator_factory()
    roles = dict.fromkeys(ACCESS_PRIORITY, False)
    if role != "viewer":
        roles.update(
            {key: True for key in ACCESS_PRIORITY[ACCESS_PRIORITY.index(role) :]}
        )
    coord.inventory_state.cloud_metadata = {"access": roles}
    sensor = EnphaseAccountAccessSensor(coord)
    assert sensor.native_value == role
    assert sensor.extra_state_attributes["viewer"] == (role == "viewer")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"app": None},
        {"app": {}},
        {"app": {"user": {}}},
        {"app": {"user": {"id": 1}, "owner": {"id": 2}}},
    ],
)
async def test_incomplete_metadata_does_not_invent_access(coordinator_factory, payload):
    coord = coordinator_factory()
    coord.client.site_bootstrap = AsyncMock(return_value=payload)
    coord.client.system_dashboard_summary = AsyncMock(
        return_value={"country_code": " ", "currency_unit": None}
    )
    await coord.cloud_metadata_runtime.async_refresh()
    assert "access" not in coord.inventory_state.cloud_metadata
    assert not EnphaseAccountAccessSensor(coord).available


@pytest.mark.asyncio
async def test_bootstrap_api_contract(monkeypatch):
    from custom_components.enphase_ev.api import EnphaseEVClient
    from custom_components.enphase_ev.api_client.dashboard_surface import site_bootstrap

    client = SimpleNamespace(
        _site="123",
        _system_dashboard_headers={"test": "header"},
        _json=AsyncMock(return_value=bootstrap()),
    )
    assert await EnphaseEVClient.site_bootstrap(client) == bootstrap()
    client._json.assert_awaited_once_with(
        "GET",
        "https://enlighten.enphaseenergy.com/app-api/123/data.json?app=1&device_status=non_retired&is_mobile=0",
        headers={"test": "header"},
        allow_reauth=False,
    )
    client._json.return_value = []
    with pytest.raises(ValueError, match="Invalid site bootstrap"):
        await site_bootstrap(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_success", [True, False])
async def test_manual_refresh_bypasses_metadata_cache_and_retry(
    coordinator_factory, monkeypatch, initial_success
):
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    coord = coordinator_factory()
    coord.client.site_bootstrap = AsyncMock(
        return_value=bootstrap() if initial_success else {}
    )
    coord.client.system_dashboard_summary = AsyncMock(
        return_value={"country_code": "AU", "currency_unit": "AUD"}
    )
    runtime = coord.cloud_metadata_runtime
    await runtime.async_refresh()
    assert not runtime.refresh_due()
    coord.client.site_bootstrap.return_value = bootstrap(isAdmin=True)

    # Keep the actual coordinator's manual-bypass lifecycle around the metadata read.
    async def refresh_metadata(_coord):
        await runtime.async_refresh()

    monkeypatch.setattr(
        DataUpdateCoordinator, "async_request_refresh", refresh_metadata
    )
    await coord.async_request_refresh()
    assert coord.client.site_bootstrap.await_count == 2
    assert EnphaseAccountAccessSensor(coord).native_value == "administrator"
    assert not coord.endpoint_manual_bypass_active()
    assert not runtime.refresh_due()


@pytest.mark.asyncio
@pytest.mark.parametrize("older_fails", [True, False])
async def test_overlapping_manual_refreshes_do_not_race(
    coordinator_factory, older_fails
):
    import asyncio

    coord = coordinator_factory()
    coord._endpoint_manual_bypass_active = True
    entered = asyncio.Event()
    release = asyncio.Event()
    started = []

    async def fetch_bootstrap():
        started.append(len(started) + 1)
        if len(started) == 1:
            entered.set()
            await release.wait()
            if older_fails:
                raise TimeoutError("older warmup request failed")
            return bootstrap(isAdmin=False)
        return bootstrap(isAdmin=True)

    coord.client.site_bootstrap = AsyncMock(side_effect=fetch_bootstrap)
    coord.client.system_dashboard_summary = AsyncMock(
        return_value={"country_code": "AU", "currency_unit": "AUD"}
    )
    first = asyncio.create_task(coord.cloud_metadata_runtime.async_refresh())
    await entered.wait()
    second = asyncio.create_task(coord.cloud_metadata_runtime.async_refresh())
    # Finish the new manual read before the older warmup failure arrives.
    await second
    assert EnphaseAccountAccessSensor(coord).native_value == "administrator"
    release.set()
    await first
    assert EnphaseAccountAccessSensor(coord).native_value == "administrator"
