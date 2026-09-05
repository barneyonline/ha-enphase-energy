from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock

from homeassistant.util import dt as dt_util

from tests.components.enphase_ev.random_ids import RANDOM_SITE_ID


def _make_site_coord():
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.site_id = RANDOM_SITE_ID
    coord.update_interval = timedelta(seconds=10)
    coord.last_success_utc = None
    coord.last_update_success = True
    coord.last_failure_utc = None
    coord.last_failure_status = None
    coord.last_failure_description = None
    coord.last_failure_response = None
    coord.last_failure_source = None
    coord.last_failure_endpoint = None
    coord.payload_using_stale = False
    coord.payload_failure_kind = None
    coord.backoff_ends_utc = None
    return coord


def test_cloud_latency_sensor_value():
    from custom_components.enphase_ev.sensor import EnphaseCloudLatencySensor

    # Minimal coordinator stub
    coord = _make_site_coord()
    coord.latency_ms = 123
    coord.update_interval = timedelta(seconds=15)

    s = EnphaseCloudLatencySensor(coord)
    assert s.native_value == 123


def test_current_power_consumption_sensor_value_and_attributes():
    from custom_components.enphase_ev.sensor import EnphaseCurrentPowerConsumptionSensor

    coord = _make_site_coord()
    coord.last_success_utc = datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc)
    coord._current_power_consumption_w = 752.0
    coord._current_power_consumption_sample_utc = datetime(
        2026, 3, 11, 5, 40, tzinfo=timezone.utc
    )
    coord._current_power_consumption_reported_units = "W"
    coord._current_power_consumption_reported_precision = 0
    coord._current_power_consumption_source = "app-api:get_latest_power"

    sensor = EnphaseCurrentPowerConsumptionSensor(coord)
    assert sensor.available is True
    assert sensor.native_value == 752
    assert sensor.extra_state_attributes == {
        "sampled_at_utc": "2026-03-11T05:40:00+00:00",
        "cached_at_utc": "2026-03-11T05:40:00+00:00",
        "source": "app-api:get_latest_power",
        "reported_units": "W",
        "reported_precision": 0,
        "using_stale": False,
    }


def test_cloud_site_sensors_are_not_gated_by_envoy_type():
    from custom_components.enphase_ev.binary_sensor import (
        SiteCloudReachableBinarySensor,
    )
    from custom_components.enphase_ev.sensor import (
        _SiteBaseEntity,
        EnphaseCloudLatencySensor,
        EnphaseCurrentPowerConsumptionSensor,
        EnphaseSiteLastUpdateSensor,
    )

    coord = _make_site_coord()
    coord.inventory_view.has_type_for_entities = lambda _type_key: False
    coord.last_success_utc = datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc)
    coord.latency_ms = 123
    coord._current_power_consumption_w = 752.0

    assert EnphaseSiteLastUpdateSensor(coord).available is True
    assert EnphaseCloudLatencySensor(coord).available is True
    assert EnphaseCurrentPowerConsumptionSensor(coord).available is True
    assert SiteCloudReachableBinarySensor(coord).available is True
    base_entity = _SiteBaseEntity(coord, "cloud_base", "Cloud Base", type_key=None)
    assert base_entity.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:cloud")
    }
    assert base_entity.extra_state_attributes == {
        "last_success_utc": "2026-03-11T05:41:00+00:00"
    }


def test_current_power_consumption_sensor_edge_paths():
    from custom_components.enphase_ev.sensor import EnphaseCurrentPowerConsumptionSensor

    coord = _make_site_coord()
    coord.last_update_success = False
    sensor = EnphaseCurrentPowerConsumptionSensor(coord)
    assert sensor.available is False
    assert sensor.native_value is None

    coord.last_success_utc = datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc)
    coord._current_power_consumption_w = 752.1254
    assert sensor.native_value == pytest.approx(752.125)


def test_current_power_consumption_sensor_keeps_fresh_last_good_runtime_sample(
    monkeypatch,
):
    from custom_components.enphase_ev.sensor import EnphaseCurrentPowerConsumptionSensor
    from custom_components.enphase_ev import sensor as sensor_mod

    coord = _make_site_coord()
    coord.last_success_utc = datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc)
    coord._current_power_consumption_w = 752.0
    coord._current_power_consumption_sample_utc = datetime(
        2026, 3, 11, 5, 40, 45, tzinfo=timezone.utc
    )
    coord._current_power_consumption_reported_units = "W"
    coord._current_power_consumption_reported_precision = 0
    coord._current_power_consumption_source = "app-api:get_latest_power"
    monkeypatch.setattr(
        sensor_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc),
    )

    sensor = EnphaseCurrentPowerConsumptionSensor(coord)

    assert sensor.native_value == 752
    assert sensor.available is True

    coord._current_power_consumption_w = None
    coord._current_power_consumption_sample_utc = None
    coord._current_power_consumption_reported_units = None
    coord._current_power_consumption_reported_precision = None
    coord._current_power_consumption_source = None

    assert sensor.available is True
    assert sensor.native_value == 752
    assert sensor.extra_state_attributes == {
        "sampled_at_utc": "2026-03-11T05:40:45+00:00",
        "cached_at_utc": "2026-03-11T05:40:45+00:00",
        "source": "app-api:get_latest_power",
        "reported_units": "W",
        "reported_precision": 0,
        "using_stale": False,
    }


def test_current_power_consumption_sensor_expires_stale_cached_sample(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseCurrentPowerConsumptionSensor
    from custom_components.enphase_ev import sensor as sensor_mod

    now = datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc)
    coord = _make_site_coord()
    coord.last_success_utc = now
    coord._current_power_consumption_w = 752.0
    coord._current_power_consumption_sample_utc = now - timedelta(seconds=15)
    coord._current_power_consumption_reported_units = "W"
    coord._current_power_consumption_reported_precision = 0
    coord._current_power_consumption_source = "app-api:get_latest_power"
    monkeypatch.setattr(sensor_mod.dt_util, "utcnow", lambda: now)

    sensor = EnphaseCurrentPowerConsumptionSensor(coord)
    assert sensor.native_value == 752

    coord._current_power_consumption_w = None
    coord._current_power_consumption_sample_utc = None
    coord._current_power_consumption_reported_units = None
    coord._current_power_consumption_reported_precision = None
    coord._current_power_consumption_source = None

    assert sensor.available is True

    stale_now = now + timedelta(seconds=6)
    coord.last_success_utc = stale_now
    monkeypatch.setattr(sensor_mod.dt_util, "utcnow", lambda: stale_now)

    assert sensor.available is False
    assert sensor.native_value is None


def test_current_power_consumption_sensor_freshness_fallback_paths(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseCurrentPowerConsumptionSensor
    from custom_components.enphase_ev import sensor as sensor_mod

    now = datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc)
    coord = _make_site_coord()
    coord.update_interval = None
    coord.last_success_utc = None
    coord.last_update_success = True
    monkeypatch.setattr(sensor_mod.dt_util, "utcnow", lambda: now)

    sensor = EnphaseCurrentPowerConsumptionSensor(coord)
    assert sensor._cache_ttl() == timedelta(minutes=2)  # noqa: SLF001
    assert sensor._freshness_reference_utc() == now  # noqa: SLF001

    sensor._last_good_value = 1.0  # noqa: SLF001
    assert sensor.available is False
    assert sensor.native_value is None

    coord.last_success_utc = now

    def _raise_utcnow():
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr(sensor_mod.dt_util, "utcnow", _raise_utcnow)
    assert sensor._freshness_reference_utc() == now  # noqa: SLF001

    coord.last_success_utc = None
    assert sensor._freshness_reference_utc().tzinfo is timezone.utc  # noqa: SLF001


@pytest.mark.asyncio
async def test_current_power_consumption_sensor_restores_last_good_state(
    monkeypatch,
):
    from custom_components.enphase_ev.sensor import EnphaseCurrentPowerConsumptionSensor
    from custom_components.enphase_ev import sensor as sensor_mod
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    coord = _make_site_coord()
    coord.last_success_utc = datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc)

    async def _noop(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)
    monkeypatch.setattr(
        sensor_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc),
    )

    sensor = EnphaseCurrentPowerConsumptionSensor(coord)
    sensor.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=type("LastSensorData", (), {"native_value": "752.0"})()
    )
    sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=type(
            "LastState",
            (),
            {
                "attributes": {
                    "sampled_at_utc": "2026-03-11T05:40:45",
                    "source": "app-api:get_latest_power",
                    "reported_units": "W",
                    "reported_precision": object(),
                }
            },
        )()
    )

    await sensor.async_added_to_hass()

    assert sensor.available is True
    assert sensor.native_value == 752
    assert sensor.extra_state_attributes == {
        "sampled_at_utc": "2026-03-11T05:40:45+00:00",
        "cached_at_utc": None,
        "source": "app-api:get_latest_power",
        "reported_units": "W",
        "reported_precision": None,
        "using_stale": False,
    }


@pytest.mark.asyncio
async def test_current_power_consumption_sensor_restore_tolerates_last_state_error(
    monkeypatch,
):
    from custom_components.enphase_ev.sensor import EnphaseCurrentPowerConsumptionSensor
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    coord = _make_site_coord()
    coord.last_success_utc = datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc)

    async def _noop(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)

    sensor = EnphaseCurrentPowerConsumptionSensor(coord)
    sensor.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=type("LastSensorData", (), {"native_value": "bad"})()
    )
    sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )

    await sensor.async_added_to_hass()

    assert sensor.available is False
    assert sensor.native_value is None


@pytest.mark.asyncio
async def test_current_power_consumption_sensor_discards_extreme_restored_state(
    monkeypatch,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseCurrentPowerConsumptionSensor
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    coord = _make_site_coord()
    coord.last_success_utc = datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc)

    async def _noop(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)

    sensor = EnphaseCurrentPowerConsumptionSensor(coord)
    sensor.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=type("LastSensorData", (), {"native_value": "-12000000"})()
    )
    sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=type(
            "LastState",
            (),
            {
                "attributes": {
                    "sampled_at_utc": "2026-03-11T05:40:45+00:00",
                    "cached_at_utc": "2026-03-11T05:40:45+00:00",
                    "reported_units": "W",
                }
            },
        )()
    )

    await sensor.async_added_to_hass()

    assert sensor.available is False
    assert sensor.native_value is None


@pytest.mark.asyncio
async def test_current_power_consumption_sensor_restore_prefers_cached_at_freshness(
    monkeypatch,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseCurrentPowerConsumptionSensor
    from custom_components.enphase_ev import sensor as sensor_mod
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    coord = _make_site_coord()
    coord.last_update_success = True

    async def _noop(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)
    monkeypatch.setattr(
        sensor_mod.dt_util,
        "utcnow",
        lambda: datetime(2026, 3, 11, 5, 41, tzinfo=timezone.utc),
    )

    sensor = EnphaseCurrentPowerConsumptionSensor(coord)
    sensor.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=type("LastSensorData", (), {"native_value": "752.0"})()
    )
    sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=type(
            "LastState",
            (),
            {
                "attributes": {
                    "sampled_at_utc": "2026-03-11T05:35:00+00:00",
                    "cached_at_utc": "2026-03-11T05:40:45+00:00",
                    "source": "app-api:get_latest_power",
                    "reported_units": "W",
                    "reported_precision": 0,
                }
            },
        )()
    )

    await sensor.async_added_to_hass()

    assert sensor.available is True
    assert sensor.native_value == 752
    assert sensor.extra_state_attributes == {
        "sampled_at_utc": "2026-03-11T05:35:00+00:00",
        "cached_at_utc": "2026-03-11T05:40:45+00:00",
        "source": "app-api:get_latest_power",
        "reported_units": "W",
        "reported_precision": 0,
        "using_stale": False,
    }


def test_site_cloud_reachable_binary_sensor_states():
    from custom_components.enphase_ev.binary_sensor import (
        SiteCloudReachableBinarySensor,
    )

    coord = _make_site_coord()
    coord.update_interval = timedelta(seconds=10)

    bs = SiteCloudReachableBinarySensor(coord)
    # No last success yet -> off
    coord.last_success_utc = None
    assert bs.is_on is False

    # Recent success within 2x interval -> on
    now = datetime.now(timezone.utc)
    coord.last_success_utc = now - timedelta(seconds=15)
    assert bs.is_on is True

    # Stale success beyond 2x interval -> off
    coord.last_success_utc = now - timedelta(seconds=25)
    assert bs.is_on is False

    coord.last_failure_status = 500
    coord.last_failure_description = "Server error"
    payload = '{"error":"server"}'
    coord.last_failure_response = payload
    coord.last_failure_source = "http"
    coord.last_failure_endpoint = "/service/evse_controller/[site]/ev_chargers/status"
    coord.payload_failure_kind = "json_decode"
    coord.payload_using_stale = True
    coord.last_failure_utc = now

    attrs = bs.extra_state_attributes
    assert attrs == {}


def test_site_error_code_sensor_state_and_attributes():
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import EnphaseSiteLastErrorCodeSensor

    coord = _make_site_coord()
    sensor = EnphaseSiteLastErrorCodeSensor(coord)
    info = sensor.device_info
    assert info["identifiers"] == {(DOMAIN, f"type:{coord.site_id}:cloud")}
    assert info["name"] == "Enphase Cloud"
    assert info["model"] == "Cloud Service"

    # No failure yet -> None
    assert sensor.native_value == "none"

    failure_time = datetime.now(timezone.utc)
    coord.last_failure_utc = failure_time
    coord.last_failure_status = 429
    coord.last_failure_description = "Rate limited"
    payload = '{"error":{"code":429}}'
    coord.last_failure_response = payload
    coord.last_failure_source = "http"
    coord.payload_using_stale = True
    coord.payload_failure_kind = "json_decode"

    assert sensor.native_value == "invalid_payload"
    assert "rate_limited" in sensor.options
    assert sensor.extra_state_attributes == {}

    coord.payload_failure_kind = None
    assert sensor.native_value == "rate_limited"

    coord.last_failure_status = 401
    assert sensor.native_value == "authentication_error"

    coord.last_failure_status = 404
    assert sensor.native_value == "request_error"

    coord.last_failure_status = "unavailable"
    assert sensor.native_value == "request_error"

    coord.last_failure_status = 503
    assert sensor.native_value == "service_unavailable"

    coord.last_success_utc = failure_time + timedelta(seconds=1)
    assert sensor.native_value == "none"

    # DNS failures should surface a dns_error code even without HTTP status
    dns_failure_time = failure_time + timedelta(minutes=5)
    coord.last_success_utc = failure_time  # ensure failure remains active
    coord.last_failure_utc = dns_failure_time
    coord.last_failure_status = None
    coord.last_failure_description = "Timeout while contacting DNS servers"
    coord.last_failure_response = None
    coord.last_failure_source = "network"
    assert sensor.native_value == "dns_error"

    # Generic network errors should surface network_error
    generic_failure_time = dns_failure_time + timedelta(minutes=1)
    coord.last_failure_utc = generic_failure_time
    coord.last_failure_description = "Connection reset by peer"
    assert sensor.native_value == "network_error"

    # Non-network failures without status fall back to none
    coord.last_failure_source = "other"
    assert sensor.native_value == "none"

    coord.last_failure_source = "auth"
    assert sensor.native_value == "authentication_error"

    coord._last_error = "auth_blocked"
    assert sensor.native_value == "auth_blocked"

    coord._last_error = None
    coord.last_failure_status = 401
    coord._auth_blocked_until_utc = datetime.now(timezone.utc) + timedelta(hours=1)
    assert sensor.native_value == "auth_blocked"

    coord._auth_blocked_until_utc = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert sensor.native_value == "authentication_error"


def test_site_backoff_sensor_handles_none_and_datetime(monkeypatch):
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import EnphaseSiteBackoffEndsSensor

    coord = _make_site_coord()
    sensor = EnphaseSiteBackoffEndsSensor(coord)
    info = sensor.device_info
    assert info["identifiers"] == {(DOMAIN, f"type:{coord.site_id}:cloud")}

    assert sensor.native_value is None

    now = datetime.now(timezone.utc)
    backoff_until = now + timedelta(seconds=3665)
    coord.backoff_ends_utc = backoff_until
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)

    value = sensor.native_value
    assert value == backoff_until
    assert sensor.extra_state_attributes == {}

    # Once the backoff window has elapsed the sensor should reset to none
    monkeypatch.setattr(dt_util, "utcnow", lambda: backoff_until + timedelta(seconds=1))
    assert sensor.native_value is None

    # Exception when computing remaining time should fall back to none without attribute
    def _raise():
        raise RuntimeError("utc failure")

    monkeypatch.setattr(dt_util, "utcnow", _raise)
    assert sensor.native_value is None
    assert "backoff_seconds" not in sensor.extra_state_attributes


@pytest.mark.asyncio
async def test_site_backoff_sensor_schedules_and_clears_timer(hass, monkeypatch):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev import sensor as sensor_mod

    coord = _make_site_coord()
    start = datetime(2025, 11, 3, 19, 12, 0, tzinfo=timezone.utc)
    ends = start + timedelta(seconds=3)
    coord.backoff_ends_utc = ends
    monkeypatch.setattr(dt_util, "utcnow", lambda: start)

    scheduled: dict[str, object] = {}

    def _fake_track_point(hass_obj, cb, when):
        assert hass_obj is hass
        scheduled["cb"] = cb
        scheduled["when"] = when
        scheduled["cancelled"] = False

        def _cancel():
            scheduled["cancelled"] = True

        return _cancel

    monkeypatch.setattr(sensor_mod, "async_track_point_in_utc_time", _fake_track_point)

    async def _noop(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)
    monkeypatch.setattr(CoordinatorEntity, "async_will_remove_from_hass", _noop)

    sensor = sensor_mod.EnphaseSiteBackoffEndsSensor(coord)
    sensor.hass = hass

    recorded: list[object] = []
    sensor.async_write_ha_state = lambda: recorded.append(sensor.native_value)

    await sensor.async_added_to_hass()
    assert scheduled["when"] == ends + timedelta(seconds=1)
    assert sensor.native_value == ends

    # When the countdown expires the timer callback should clear the state
    monkeypatch.setattr(dt_util, "utcnow", lambda: ends + timedelta(seconds=1))
    scheduled["cb"](scheduled["when"])
    assert recorded == [None]
    assert scheduled["cancelled"] is True

    # Coordinator updates should restart the timer when a new backoff window begins
    new_end = start + timedelta(seconds=5)
    coord.backoff_ends_utc = new_end
    monkeypatch.setattr(dt_util, "utcnow", lambda: start)
    scheduled.clear()
    sensor._handle_coordinator_update()
    assert scheduled["when"] == new_end + timedelta(seconds=1)

    # Clearing the backoff should stop the timer
    coord.backoff_ends_utc = None
    sensor._ensure_expiry_timer()
    assert scheduled.get("cancelled") is True
    assert sensor._expiry_cancel is None

    # Removing the entity should swallow ticker cancellation failures
    cancel_calls: list[bool] = []

    def _cancel_raise():
        cancel_calls.append(True)
        raise RuntimeError("boom")

    sensor._expiry_cancel = _cancel_raise
    await sensor.async_will_remove_from_hass()
    assert cancel_calls == [True]
    assert sensor._expiry_cancel is None


def test_site_backoff_sensor_rounds_up_remaining_seconds(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseSiteBackoffEndsSensor

    coord = _make_site_coord()
    sensor = EnphaseSiteBackoffEndsSensor(coord)

    start = datetime(2025, 11, 3, 19, 12, 0, tzinfo=timezone.utc)
    coord.backoff_ends_utc = start + timedelta(seconds=1)
    monkeypatch.setattr(dt_util, "utcnow", lambda: start + timedelta(seconds=0.6))

    assert sensor.native_value == coord.backoff_ends_utc


def test_cloud_site_sensors_device_info_prefers_coordinator_info():
    from custom_components.enphase_ev.sensor import (
        EnphaseCurrentPowerConsumptionSensor,
        EnphaseCloudLatencySensor,
        EnphaseSiteBackoffEndsSensor,
        EnphaseSiteLastErrorCodeSensor,
        EnphaseSiteLastUpdateSensor,
    )

    coord = _make_site_coord()
    provided = {"identifiers": {("enphase_ev", "type:site-x:cloud")}}
    coord.inventory_view.type_device_info = lambda _key: provided  # type: ignore[assignment]

    assert EnphaseSiteLastUpdateSensor(coord).device_info is provided
    assert EnphaseCloudLatencySensor(coord).device_info is provided
    assert EnphaseCurrentPowerConsumptionSensor(coord).device_info is provided
    assert EnphaseSiteLastErrorCodeSensor(coord).device_info is provided
    assert EnphaseSiteBackoffEndsSensor(coord).device_info is provided


def test_site_backoff_sensor_does_not_start_ticker_without_hass(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseSiteBackoffEndsSensor

    coord = _make_site_coord()
    start = datetime(2025, 11, 3, 19, 12, 0, tzinfo=timezone.utc)
    coord.backoff_ends_utc = start + timedelta(seconds=10)
    sensor = EnphaseSiteBackoffEndsSensor(coord)

    monkeypatch.setattr(dt_util, "utcnow", lambda: start)
    sensor._ensure_expiry_timer()
    assert sensor._expiry_cancel is None


def test_site_backoff_sensor_cancels_timer_when_utc_fails(hass, monkeypatch):
    from custom_components.enphase_ev import sensor as sensor_mod

    coord = _make_site_coord()
    start = datetime(2025, 11, 3, 19, 12, 0, tzinfo=timezone.utc)
    coord.backoff_ends_utc = start + timedelta(seconds=5)
    sensor = sensor_mod.EnphaseSiteBackoffEndsSensor(coord)
    sensor.hass = hass

    cancelled: list[bool] = []

    def _cancel():
        cancelled.append(True)

    sensor._expiry_cancel = _cancel

    def _raise():
        raise RuntimeError("utc failure")

    monkeypatch.setattr(dt_util, "utcnow", _raise)

    scheduled: list[bool] = []

    def _should_not_schedule(*args, **kwargs):
        scheduled.append(True)
        raise RuntimeError("should not schedule when utc fails")

    monkeypatch.setattr(
        sensor_mod, "async_track_point_in_utc_time", _should_not_schedule
    )

    sensor._ensure_expiry_timer()
    assert cancelled == [True]
    assert sensor._expiry_cancel is None
    assert not scheduled


def test_site_last_update_sensor_reflects_success_timestamp():
    from custom_components.enphase_ev.sensor import EnphaseSiteLastUpdateSensor

    coord = _make_site_coord()
    sensor = EnphaseSiteLastUpdateSensor(coord)
    assert sensor.native_value is None

    success_time = datetime.now(timezone.utc)
    coord.last_success_utc = success_time
    assert sensor.native_value == success_time
