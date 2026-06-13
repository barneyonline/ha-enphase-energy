from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from homeassistant.const import UnitOfLength

from custom_components.enphase_ev.sensor import (
    EnphaseEnergyTodaySensor,
    EnphasePowerSensor,
    EnphaseSiteBackoffEndsSensor,
)

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

pytest.importorskip("homeassistant")


def _dummy_coord(payload: dict) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.data = {RANDOM_SERIAL: payload}
    coord.serials = {RANDOM_SERIAL}
    coord.site_id = "site"
    coord.last_update_success = True
    coord.iter_serials = lambda: coord.serials
    coord.async_add_listener = lambda cb, context=None: (lambda: None)
    return coord


@pytest.mark.asyncio
async def test_last_session_restores_state_and_extra(monkeypatch):
    """Exercise restore handling and as_dict helpers for Last Session sensor."""
    coord = _dummy_coord({"sn": RANDOM_SERIAL, "name": "EV"})
    sensor = EnphaseEnergyTodaySensor(coord, RANDOM_SERIAL)

    class _State:
        state = "2.5"
        attributes = {"session_key": 123, "session_duration_min": "45"}

    class _Extra:
        def as_dict(self):
            return {
                "last_session_kwh": "1.5",
                "last_session_wh": "1500",
                "last_session_start": "100",
                "last_session_end": "200",
                "session_key": None,
                "last_duration_min": None,
            }

    async def _fake_last_state():
        return _State()

    async def _fake_last_extra():
        return _Extra()

    monkeypatch.setattr(sensor, "async_get_last_state", _fake_last_state)
    monkeypatch.setattr(sensor, "async_get_last_extra_data", _fake_last_extra)

    await sensor.async_added_to_hass()
    extra = sensor.extra_restore_state_data
    assert extra is not None
    assert extra.as_dict()["last_session_kwh"] == pytest.approx(2.5)
    assert sensor._session_key == "123"
    assert sensor._last_duration_min == 45


def test_last_session_helper_branches(monkeypatch):
    """Cover timestamp/energy helpers and context selection branches."""
    sensor = EnphaseEnergyTodaySensor(
        _dummy_coord({"sn": RANDOM_SERIAL, "name": "EV"}), RANDOM_SERIAL
    )
    assert sensor._coerce_timestamp(None) is None
    assert sensor._coerce_timestamp("  ") is None
    assert sensor._coerce_timestamp("2025-01-01T00:00:00Z") is not None
    assert sensor._coerce_timestamp(1700000000) == pytest.approx(1700000000.0)

    # Energy fallback paths
    kwh, wh = sensor._coerce_energy(None, 150.0)
    assert kwh == pytest.approx(0.15)
    assert wh == pytest.approx(150.0)
    kwh2, wh2 = sensor._coerce_energy(None, 250.0)
    assert kwh2 == pytest.approx(0.25)
    assert wh2 == pytest.approx(250.0)
    kwh3, wh3 = sensor._coerce_energy(None, 140.038)
    assert kwh3 == pytest.approx(0.14)
    assert wh3 == pytest.approx(140.038)

    # History context when realtime lacks energy
    data = {
        "energy_today_sessions": [
            {
                "session_id": "s1",
                "start": "2025-01-02T00:00:00Z",
                "end": "2025-01-02T00:30:00Z",
                "energy_kwh_total": 1.0,
            }
        ]
    }
    history = sensor._extract_history_session(data)
    assert history["session_key"] == "s1"
    context = sensor._pick_session_context(data)
    assert context["energy_kwh"] == pytest.approx(1.0)

    # Prefer history when realtime is zeroed and not charging
    data_zero = {
        "charging": False,
        "session_kwh": 0,
        "energy_today_sessions": [
            {
                "session_id": "s2",
                "energy_kwh_total": 4.5,
                "start": "2025-01-03T00:00:00Z",
                "end": "2025-01-03T01:00:00Z",
            }
        ],
    }
    context_zero = sensor._pick_session_context(data_zero)
    assert context_zero["energy_kwh"] == pytest.approx(4.5)

    # Idle realtime with history lacking energy should still surface history context
    data_zero_missing = {
        "charging": False,
        "session_kwh": 0,
        "energy_today_sessions": [
            {
                "session_id": "s4",
                "energy_kwh_total": None,
                "start": "2025-01-03T02:00:00Z",
                "end": "2025-01-03T03:00:00Z",
            }
        ],
    }
    context_zero_missing = sensor._pick_session_context(data_zero_missing)
    assert context_zero_missing["energy_kwh"] is None

    # Zero-energy history sessions should be honored
    zero_history = sensor._extract_history_session(
        {
            "energy_today_sessions": [
                {
                    "session_id": "s3",
                    "start": "2025-01-04T00:00:00Z",
                    "end": "2025-01-04T01:00:00Z",
                    "energy_kwh_total": 0.0,
                }
            ]
        }
    )
    assert zero_history["energy_kwh"] == pytest.approx(0.0)

    # Realtime zero should be treated as missing when idle and no history exists
    rt_zero = {"charging": False, "session_kwh": 0.0}
    monkeypatch.setattr(
        sensor,
        "_extract_realtime_session",
        lambda d: {"energy_kwh": 0.0, "charging": False},
    )
    monkeypatch.setattr(sensor, "_extract_history_session", lambda d: None)
    picked = sensor._pick_session_context(rt_zero)
    assert picked is None

    sensor._last_session_kwh = 5.0
    sensor._last_session_wh = 5000.0
    sensor._data = rt_zero
    assert sensor.native_value == 5.0

    # Realtime with small positive energy should be returned when no history exists
    rt_small = {"charging": False, "session_kwh": 0.001}
    monkeypatch.setattr(
        sensor,
        "_extract_realtime_session",
        lambda d: {"energy_kwh": 0.001, "charging": False},
    )
    monkeypatch.setattr(sensor, "_extract_history_session", lambda d: None)
    ctx_small = sensor._pick_session_context(rt_small)
    assert ctx_small["energy_kwh"] == pytest.approx(0.001)

    # History should still be returned when realtime is unavailable
    monkeypatch.setattr(sensor, "_extract_realtime_session", lambda d: None)
    monkeypatch.setattr(
        sensor, "_extract_history_session", lambda d: {"energy_kwh": None}
    )
    ctx_history_only = sensor._pick_session_context({})
    assert ctx_history_only["energy_kwh"] is None

    # When both realtime and history are missing, None is returned
    monkeypatch.setattr(sensor, "_extract_realtime_session", lambda d: None)
    monkeypatch.setattr(sensor, "_extract_history_session", lambda d: None)
    assert sensor._pick_session_context({}) is None

    # Duration when charging and end missing
    start = datetime(2025, 1, 2, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(
        sensor,
        "_pick_session_context",
        lambda d: {"start": start, "end": None, "charging": True},
    )
    val = sensor.native_value
    assert val == sensor._last_session_kwh


def test_session_metadata_attributes_fill_gaps(monkeypatch):
    """Ensure attribute helper handles missing values and conversions."""
    sensor = EnphaseEnergyTodaySensor(
        _dummy_coord({"sn": RANDOM_SERIAL, "name": "EV"}), RANDOM_SERIAL
    )

    class DummyUnits:
        length_unit = UnitOfLength.KILOMETERS

    class DummyHass:
        config = SimpleNamespace(units=DummyUnits())

    sensor.hass = DummyHass()  # type: ignore[assignment]
    base = {
        "session_plug_in_at": "2025-01-03T01:00:00Z",
        "session_plug_out_at": 1704253200,
        "session_kwh": "2.5",
        "session_energy_wh": "2600",
        "session_cost": "3.5",
        "session_charge_level": "16",
        "session_miles": "10",
    }
    attrs = sensor._session_metadata_attributes(
        base,
        hass=sensor.hass,
        context={"session_cost": "3.75", "energy_wh": "2600"},
        energy_kwh=None,
        energy_wh=None,
        duration_min=75,
        session_key="abc",
    )
    assert attrs["energy_consumed_kwh"] == pytest.approx(2.5)
    assert attrs["energy_consumed_wh"] == pytest.approx(2600.0)
    assert attrs["session_cost"] == pytest.approx(3.75)
    assert attrs["session_charge_level"] == 16
    assert attrs["range_added"] is not None
    assert attrs["session_duration_min"] == 75
    assert "session_id" not in attrs
    assert attrs["session_started_at"] is None
    assert attrs["session_ended_at"] is None
    assert attrs["active_charge_time_s"] is None
    assert attrs["avg_cost_per_kwh"] is None
    assert attrs["cost_calculated"] is None
    assert attrs["session_cost_state"] is None
    assert attrs["manual_override"] is None
    assert attrs["charge_profile_stack_level"] is None


@pytest.mark.asyncio
async def test_power_sensor_restore_parses_legacy_and_resets(monkeypatch):
    """Hit restore and timestamp parsing branches on the power sensor."""
    coord = _dummy_coord(
        {
            "sn": RANDOM_SERIAL,
            "name": "EV",
            "lifetime_kwh": 10.0,
            "last_reported_at": None,
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)

    class _State:
        state = "5"
        attributes = {
            "last_lifetime_kwh": "12.0",
            "last_energy_ts": "bad",
            "last_sample_ts": None,
            "last_power_w": "bad",
            "last_window_seconds": "bad",
            "method": None,
            "last_reset_at": "bad",
            "baseline_kwh": "2.0",
            "last_energy_today_kwh": "0.5",
            "last_ts": "1700000000",
        }

    async def _fake_last_state():
        return _State()

    monkeypatch.setattr(sensor, "async_get_last_state", _fake_last_state)
    await sensor.async_added_to_hass()

    coord.data[RANDOM_SERIAL]["charging"] = False
    assert sensor.native_value == 0


def test_site_backoff_remaining_seconds(monkeypatch):
    """Cover _backoff_remaining_seconds edge cases."""
    now = datetime(2025, 1, 4, 12, 0, 0, tzinfo=timezone.utc)
    coord = SimpleNamespace(
        backoff_ends_utc=now + timedelta(seconds=2),
        last_success_utc=None,
        last_failure_utc=None,
        last_failure_status=None,
        last_failure_description=None,
        last_failure_response=None,
        last_failure_source=None,
        site_id="12345",
    )
    sensor = EnphaseSiteBackoffEndsSensor(coord)
    monkeypatch.setattr(sensor, "_coord", coord)
    monkeypatch.setattr(
        "homeassistant.util.dt.utcnow", lambda: now + timedelta(seconds=5)
    )
    assert sensor._backoff_remaining_seconds() == 0
