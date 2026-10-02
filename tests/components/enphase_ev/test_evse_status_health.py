"""Charger outages must not gate fresh sibling telemetry or confirm stale writes."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from custom_components.enphase_ev import evse_status_health as health_mod
from custom_components.enphase_ev.const import (
    ISSUE_EVSE_STATUS_UNAVAILABLE,
    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
)
from custom_components.enphase_ev.diagnostics import async_get_config_entry_diagnostics
from custom_components.enphase_ev.entity import EnphaseBaseEntity
from custom_components.enphase_ev.evse_status_health import (
    EvseStatusHealth,
    async_load_status_history,
)
from custom_components.enphase_ev.sensor import (
    EnphaseBatteryOverallChargeSensor,
    EnphaseCurrentPowerConsumptionSensor,
    EnphaseSiteBackoffEndsSensor,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
REQUEST_ID = "dde1a765-fcc2-458c-9b29-7da2c6cffc54"


def server_error(status=500, headers=None):
    url = URL(
        "https://enlighten.enphaseenergy.com/service/evse_controller/private-site/ev_chargers/status"
    )
    info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    err = aiohttp.ClientResponseError(
        info, (), status=status, message="secret-cookie password", headers=headers
    )
    err.enphase_error_status = "INTERNAL_SERVER_ERROR"
    return err


@pytest.fixture
def status_clock(monkeypatch):
    timers = []
    clock = SimpleNamespace(now=NOW, mono=100.0)
    monkeypatch.setattr(health_mod.dt_util, "utcnow", lambda: clock.now)
    monkeypatch.setattr(health_mod.time, "monotonic", lambda: clock.mono)
    monkeypatch.setattr(health_mod.random, "uniform", lambda *_: 1.0)

    def schedule(_hass, delay, callback):
        cancel = Mock()
        timers.append((delay, callback, cancel))
        return cancel

    monkeypatch.setattr(health_mod, "async_call_later", schedule)
    return clock, timers


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [False, True])
async def test_status_500_keeps_sibling_readings_and_retries_independently(
    coordinator_factory, config_entry, status_clock, cold
):
    clock, timers = status_clock
    coord = coordinator_factory(data={} if cold else None)
    coord.config_entry = config_entry
    coord._has_successful_refresh = not cold
    coord._selected_type_keys = {"iqevse", "envoy", "encharge"}
    coord.client.status = AsyncMock(side_effect=server_error())
    coord.client.summary_v2 = AsyncMock(return_value=[])
    coord.client.battery_status = AsyncMock(
        return_value={"current_charge": "35%", "storages": []}
    )
    coord.client.latest_power = AsyncMock(
        return_value={"value": 2484, "time": clock.now.timestamp(), "units": "W"}
    )
    coord.energy._async_refresh_site_energy = AsyncMock()
    pending = coord.control_updates.begin(
        "charging", RANDOM_SERIAL, {"enabled": True}, {"enabled": False}
    )
    coord.control_updates.finish(("charging", RANDOM_SERIAL), pending)
    try:
        if cold:
            config_entry._async_set_state(
                coord.hass, ConfigEntryState.SETUP_IN_PROGRESS, None
            )
            await coord.async_bootstrap_first_refresh()
            await coord.refresh_runner.async_startup_warmup_runner()
        else:
            await coord.async_refresh()
        assert coord.last_update_success
        assert not coord.evse_status_available
        assert EnphaseBatteryOverallChargeSensor(coord).available
        assert EnphaseBatteryOverallChargeSensor(coord).native_value == 35
        assert EnphaseCurrentPowerConsumptionSensor(coord).available
        assert EnphaseCurrentPowerConsumptionSensor(coord).native_value == 2484
        assert not EnphaseBaseEntity(coord, RANDOM_SERIAL).available
        assert RANDOM_SERIAL in coord.serials
        assert pending.status == "pending"
        assert coord._backoff_until is None
        assert coord.backoff_ends_utc is None
        retry_sensor = EnphaseSiteBackoffEndsSensor(coord)
        assert retry_sensor.native_value == NOW + timedelta(seconds=60)
        assert retry_sensor.extra_state_attributes["endpoint"] == "charger_status"
        assert retry_sensor.extra_state_attributes["http_status"] == 500
        # Cooldown skips only status; force siblings due without bypassing it.
        coord.battery_state._battery_status_cache_until = None
        coord.current_power_runtime._cache_until_mono = None
        coord._endpoint_family_health.clear()
        await coord.async_refresh()
        coord.client.status.assert_awaited_once()
        assert coord.client.battery_status.await_count == 2
        assert coord.client.latest_power.await_count == 2
        assert pending.status == "pending"
        # Recovery follows the timer, without reload or reauthentication.
        coord.client.status.side_effect = None
        coord.client.status.return_value = {
            "evChargerData": [{"sn": RANDOM_SERIAL, "charging": False}],
            "ts": None,
        }
        coord._start_backoff_refresh = Mock()
        clock.now += timedelta(seconds=60)
        clock.mono += 60
        timers[0][1](clock.now)
        coord._start_backoff_refresh.assert_called_once()
        await coord.async_refresh()
        assert coord.evse_status_available
        assert EnphaseBaseEntity(coord, RANDOM_SERIAL).available
        assert retry_sensor.native_value is None
        assert retry_sensor.extra_state_attributes == {}
        assert (
            coord.evse_status_health.diagnostics()["failures"][0]["http_status"] == 500
        )
    finally:
        await coord.async_cleanup_runtime_state()


@pytest.mark.asyncio
async def test_status_history_survives_restart_and_setup_retry(
    coordinator_factory, config_entry, hass, status_clock
):
    clock, _ = status_clock
    coord = coordinator_factory()
    coord.config_entry = config_entry
    health = coord.evse_status_health
    await health.async_success()
    await health.async_failure(
        server_error(
            headers={
                "X-Request-ID": REQUEST_ID,
                "Cookie": "private-cookie",
                "Authorization": "Bearer private-token",
            }
        )
    )
    restored = EvseStatusHealth(coord)
    await restored.async_restore()
    assert not restored.available
    assert restored.cooldown_active
    assert restored.last_success_utc == NOW.isoformat()
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    saved = diag["charger_status"]
    assert saved["next_retry_utc"] == (NOW + timedelta(seconds=60)).isoformat()
    assert saved["failures"][0]["request_id"] == REQUEST_ID
    assert saved["failures"][0]["error_code"] == "INTERNAL_SERVER_ERROR"
    assert "private-cookie" not in str(saved)
    assert "private-token" not in str(saved)
    assert "secret-cookie" not in str(saved)
    assert "private-site" not in str(saved)
    saved["failures"][0]["http_status"] = 599
    assert health.diagnostics()["failures"][0]["http_status"] == 500
    clock.now += timedelta(seconds=61)
    expired = EvseStatusHealth(coord)
    await expired.async_restore()
    assert not expired.available
    assert not expired.cooldown_active
    await restored.async_success()
    recovered = EvseStatusHealth(coord)
    await recovered.async_restore()
    assert recovered.available
    assert not recovered.cooldown_active
    assert recovered.diagnostics()["failures"]
    health.stop()
    restored.stop()
    expired.stop()
    recovered.stop()


@pytest.mark.asyncio
async def test_status_retries_are_bounded_and_respect_retry_after(
    coordinator_factory, status_clock
):
    clock, timers = status_clock
    health = coordinator_factory().evse_status_health
    for index in range(20):
        await health.async_failure(server_error())
        assert timers[-1][0] <= 600
    assert len(health.diagnostics()["failures"]) == 16
    await health.async_failure(server_error(headers={"Retry-After": "1800"}))
    assert timers[-1][0] == 1800
    assert (
        health.diagnostics()["next_retry_utc"]
        == (NOW + timedelta(seconds=1800)).isoformat()
    )
    health.coordinator._start_backoff_refresh = Mock()
    health.coordinator._runtime_stopped = True
    timers[-1][1](clock.now)
    health.coordinator._start_backoff_refresh.assert_not_called()
    assert health.diagnostics()["next_retry_utc"] is None
    health.stop()


@pytest.mark.asyncio
async def test_status_repair_updates_retry_and_clears_on_recovery(
    coordinator_factory, config_entry, mock_issue_registry, status_clock
):
    coord = coordinator_factory()
    coord.config_entry = config_entry
    coord.hass.config_entries.async_update_entry(
        config_entry, options={OPT_DEGRADED_SERVICE_REPAIR_ISSUES: True}
    )
    await coord.evse_status_health.async_failure(server_error())
    coord.diagnostics.report_evse_status_issue()
    first = mock_issue_registry.created[-1]
    assert first[2]["translation_key"] == ISSUE_EVSE_STATUS_UNAVAILABLE
    await coord.evse_status_health.async_failure(server_error(503))
    coord.diagnostics.report_evse_status_issue()
    second = mock_issue_registry.created[-1]
    assert second[2]["translation_placeholders"]["status"] == "503"
    assert (
        first[2]["translation_placeholders"]["next_retry"]
        != second[2]["translation_placeholders"]["next_retry"]
    )
    await coord.evse_status_health.async_success()
    coord.diagnostics.clear_evse_status_issue()
    assert not coord._evse_status_issue_reported
    assert "charger_status" not in coord.collect_site_metrics()["degraded_services"]


@pytest.mark.asyncio
@pytest.mark.parametrize("site_only", [False, True])
async def test_disabled_status_polling_retains_history_without_restoring_outage(
    coordinator_factory, config_entry, mock_issue_registry, status_clock, site_only
):
    coord = coordinator_factory()
    coord.config_entry = config_entry
    coord.hass.config_entries.async_update_entry(
        config_entry, options={OPT_DEGRADED_SERVICE_REPAIR_ISSUES: True}
    )
    await coord.evse_status_health.async_failure(server_error())
    coord.diagnostics.report_evse_status_issue()
    assert coord._evse_status_issue_reported
    coord.evse_status_health.stop()

    restored = coordinator_factory(serials=[])
    restored.config_entry = config_entry
    restored.site_only = site_only
    restored._selected_type_keys = {"envoy", "encharge"}
    restored.client.status = AsyncMock()
    restored.energy._async_refresh_site_energy = AsyncMock()
    restored.refresh_runner.async_run_refresh_plan = AsyncMock()
    try:
        await restored.evse_status_health.async_restore()
        assert restored.evse_status_available
        assert not restored.evse_status_health.cooldown_active
        assert (
            restored.evse_status_health.diagnostics()["failures"][0]["http_status"]
            == 500
        )
        assert any(
            ISSUE_EVSE_STATUS_UNAVAILABLE in issue_id
            for _, issue_id in mock_issue_registry.deleted
        )
        await restored.async_refresh()
        restored.client.status.assert_not_awaited()
        assert (
            "charger_status" not in restored.collect_site_metrics()["degraded_services"]
        )
        assert EnphaseSiteBackoffEndsSensor(restored).native_value is None
    finally:
        await coord.async_cleanup_runtime_state()
        await restored.async_cleanup_runtime_state()


@pytest.mark.asyncio
async def test_shared_backoff_does_not_report_previous_status_outage_as_its_cause(
    coordinator_factory, status_clock
):
    clock, _ = status_clock
    coord = coordinator_factory()
    coord._minimal_setup_refresh_active = True
    coord.client.status = AsyncMock(side_effect=server_error())
    coord.energy._async_refresh_site_energy = AsyncMock()
    try:
        await coord.async_refresh()
        sensor = EnphaseSiteBackoffEndsSensor(coord)
        assert sensor.extra_state_attributes["endpoint"] == "charger_status"
        clock.now += timedelta(seconds=60)
        clock.mono += 60
        coord.client.status.side_effect = server_error(429, {"Retry-After": "1800"})
        await coord.async_refresh()
        assert not coord.last_update_success
        assert sensor.native_value == clock.now + timedelta(seconds=1800)
        assert sensor.extra_state_attributes == {}
        assert (
            coord.evse_status_health.diagnostics()["failures"][0]["http_status"] == 500
        )
        assert coord.last_failure_status == 429
        assert not coord.evse_status_available
    finally:
        await coord.async_cleanup_runtime_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [server_error(429), ConfigEntryAuthFailed("expired")])
async def test_auth_and_rate_limits_still_block_shared_refresh(
    coordinator_factory, error
):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(side_effect=error)
    with pytest.raises((UpdateFailed, ConfigEntryAuthFailed)):
        await coord._async_update_data()
    assert coord.evse_status_health.diagnostics()["failures"] == []


@pytest.mark.asyncio
async def test_charger_writes_blocked_but_explicit_stop_allowed(
    coordinator_factory, status_clock
):
    from custom_components.enphase_ev.evse_runtime import ChargeModeStartPreferences

    coord = coordinator_factory()
    coord.client.start_charging = AsyncMock()
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.set_charge_mode = AsyncMock()
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord._charge_mode_start_preferences = lambda _sn: ChargeModeStartPreferences(
        enforce_mode="SCHEDULED_CHARGING"
    )
    await coord.evse_status_health.async_failure(server_error())
    try:
        with pytest.raises(ServiceValidationError) as raised:
            await coord.async_start_charging(RANDOM_SERIAL, allow_unplugged=True)
        assert raised.value.translation_key == "charger_status_unavailable"
        coord.client.start_charging.assert_not_awaited()
        assert await coord.async_stop_charging(RANDOM_SERIAL) == {"status": "ok"}
        coord.client.stop_charging.assert_awaited_once_with(RANDOM_SERIAL)
        coord.client.set_charge_mode.assert_not_awaited()
        assert coord.control_updates.pending("charging", RANDOM_SERIAL)
    finally:
        await coord.async_cleanup_runtime_state()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "saved",
    [
        None,
        [],
        {},
        {"failures": "bad"},
        {
            "next_retry_utc": "invalid",
            "failures": [
                None,
                {},
                {"failed_at": "invalid", "http_status": 500},
                {"failed_at": NOW.isoformat(), "http_status": 200},
            ],
        },
    ],
)
async def test_status_history_rejects_invalid_storage(hass, monkeypatch, saved):
    monkeypatch.setattr(health_mod.Store, "async_load", AsyncMock(return_value=saved))
    result = await async_load_status_history(hass, "entry")
    assert not result.get("failures")


@pytest.mark.asyncio
async def test_status_history_storage_failure_is_nonfatal(
    coordinator_factory, hass, monkeypatch, status_clock
):
    monkeypatch.setattr(
        health_mod.Store, "async_load", AsyncMock(side_effect=OSError("unavailable"))
    )
    assert await async_load_status_history(hass, "entry") == {}
    health = coordinator_factory().evse_status_health
    await health.async_restore()
    monkeypatch.setattr(
        health_mod.Store, "async_save", AsyncMock(side_effect=OSError("full"))
    )
    error = server_error()
    error.enphase_error_status = "SECRET_PASSWORD"
    await health.async_failure(error)
    assert health.diagnostics()["failures"][0]["error_code"] is None
    assert health.diagnostics()["failures"][0]["request_id"] is None
    health.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"production": [500], "interval_minutes": 60}, {}])
async def test_only_fresh_sibling_acquisitions_extend_reachability(
    coordinator_factory, status_clock, payload
):
    from custom_components.enphase_ev.binary_sensor import (
        SiteCloudReachableBinarySensor,
    )
    from custom_components.enphase_ev.cloud_errors import cloud_error_code

    clock, _ = status_clock
    coord = coordinator_factory()
    coord._minimal_setup_refresh_active = True
    coord.update_interval = timedelta(seconds=15)
    coord.client.status = AsyncMock(side_effect=server_error())
    coord.client.lifetime_energy = AsyncMock(return_value=payload)
    coord.client.hems_consumption_lifetime = AsyncMock(return_value=None)
    coord.refresh_runner.async_run_refresh_plan = AsyncMock()
    old_success = NOW - timedelta(hours=1)
    coord.last_success_utc = old_success
    try:
        await coord.async_refresh()
        assert cloud_error_code(coord) == "service_unavailable"
        assert coord.last_success_utc == (NOW if payload else old_success)
        assert SiteCloudReachableBinarySensor(coord).is_on == bool(payload)
        if payload:
            assert coord.energy.site_energy["solar_production"].value_kwh == 0.5
        # Cached energy must not imply another successful cloud acquisition.
        clock.now += timedelta(seconds=31)
        clock.mono += 31
        await coord.async_refresh()
        assert coord.last_success_utc == (NOW if payload else old_success)
        assert not SiteCloudReachableBinarySensor(coord).is_on
        if payload:
            coord.client.lifetime_energy.assert_awaited_once()
    finally:
        await coord.async_cleanup_runtime_state()


@pytest.mark.asyncio
async def test_retired_status_runtime_cannot_start_new_work(
    coordinator_factory, status_clock
):
    coord = coordinator_factory()
    health = coord.evse_status_health
    await health.async_failure(server_error())
    await coord.async_cleanup_runtime_state()
    with pytest.raises(asyncio.CancelledError):
        await health.async_failure(server_error())
    with pytest.raises(asyncio.CancelledError):
        await health.async_success()
    fresh = EvseStatusHealth(coord)
    await fresh.async_restore()
    assert fresh.available
    assert fresh.diagnostics()["failures"] == []
    assert not fresh.cooldown_active


def test_diagnostics_tolerate_unavailable_battery_capabilities(
    coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    def unavailable(_self):
        raise RuntimeError("battery capability temporarily unavailable")

    monkeypatch.setattr(
        EnphaseCoordinator, "battery_reserve_editable", property(unavailable)
    )
    metrics = coordinator_factory().collect_site_metrics()
    assert metrics["battery_reserve_editable"] is False


@pytest.mark.asyncio
async def test_degraded_startup_publishes_battery_without_gateway_power(
    coordinator_factory, config_entry, status_clock
):
    coord = coordinator_factory(data={})
    coord.config_entry = config_entry
    coord._selected_type_keys = {"iqevse", "encharge"}
    coord.client.status = AsyncMock(side_effect=server_error())
    coord.client.summary_v2 = AsyncMock(return_value=[])
    coord.client.latest_power = AsyncMock(
        side_effect=aiohttp.ClientError("power endpoint down")
    )
    coord.client.lifetime_energy = AsyncMock(return_value={})
    coord.client.battery_status = AsyncMock(
        return_value={"current_charge": "35%", "storages": []}
    )
    config_entry._async_set_state(coord.hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    try:
        await coord.async_bootstrap_first_refresh()
        initial = coord.integration_snapshot
        listener = Mock()
        unsub = coord.async_add_listener(listener)
        try:
            await coord.refresh_runner.async_startup_warmup_runner()
            assert coord.integration_snapshot != initial
            assert listener.call_count > 0
            assert EnphaseBatteryOverallChargeSensor(coord).available
            assert EnphaseBatteryOverallChargeSensor(coord).native_value == 35
            assert not EnphaseCurrentPowerConsumptionSensor(coord).available
            assert not coord.evse_status_available
        finally:
            unsub()
    finally:
        await coord.async_cleanup_runtime_state()


@pytest.mark.asyncio
async def test_unload_during_history_save_prevents_late_refresh_and_repairs(
    coordinator_factory, status_clock, monkeypatch, mock_issue_registry
):
    coord = coordinator_factory()
    coord.client.status = AsyncMock(side_effect=server_error())
    coord.energy._async_refresh_site_energy = AsyncMock()

    async def save(_data):
        await coord.async_cleanup_runtime_state()

    monkeypatch.setattr(health_mod.Store, "async_save", AsyncMock(side_effect=save))
    mock_issue_registry.created.clear()
    with pytest.raises(asyncio.CancelledError):
        await coord._async_update_data()
    coord.energy._async_refresh_site_energy.assert_not_awaited()
    assert not mock_issue_registry.created
    assert not coord.evse_status_health.cooldown_active
