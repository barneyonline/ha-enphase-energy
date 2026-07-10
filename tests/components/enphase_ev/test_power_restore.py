import datetime as _dt

import pytest


@pytest.mark.asyncio
async def test_power_restore_continues_from_last_sample(monkeypatch):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity
    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = "555555555555"

    # Prepare a fixed day and a prior sample at t0
    t0 = _dt.datetime(2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc)
    today_str = t0.strftime("%Y-%m-%d")
    last_ts = t0.timestamp()

    # Build coordinator stub and initial data
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {
        sn: {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 10.5,
            "operating_v": 230,
            "charging": True,
        }
    }
    coord.serials = {sn}
    coord.site_id = "1234567"
    coord.last_update_success = True

    ent = EnphasePowerSensor(coord, sn)

    # Provide a fake last state via monkeypatch without relying on hass restore cache
    class _FakeState:
        def __init__(self, state: str, attrs: dict):
            self.state = state
            self.attributes = attrs

    async def _fake_get_last_state(self):
        return _FakeState(
            "3600",
            {
                "baseline_kwh": 10.0,
                "baseline_day": today_str,
                "last_energy_today_kwh": 0.5,
                "last_ts": last_ts,
            },
        )

    # Avoid calling the CoordinatorEntity async_added_to_hass implementation
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(
        EnphasePowerSensor, "async_get_last_state", _fake_get_last_state
    )
    monkeypatch.setattr(
        EnphasePowerSensor,
        "async_get_last_extra_data",
        lambda self: _async_none(),
    )
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)

    await ent.async_added_to_hass()

    # Advance time by 60s and increase lifetime by 0.1 kWh → 6000 W
    t1 = t0 + _dt.timedelta(seconds=60)
    monkeypatch.setattr(dt_util, "now", lambda: t1)
    coord.data[sn]["lifetime_kwh"] = 10.6

    assert ent.native_value == 6000


async def _async_none():
    return None


def test_power_restore_data_rejects_invalid_values():
    """Private restore payload parsing tolerates stale or corrupt data."""
    from custom_components.enphase_ev.sensor import _PowerRestoreData

    empty = _PowerRestoreData.from_dict(None)
    assert empty.as_dict() == {
        "last_lifetime_kwh": None,
        "last_energy_ts": None,
        "last_sample_ts": None,
        "last_power_w": None,
        "last_window_seconds": None,
        "method": None,
        "last_reset_at": None,
    }

    invalid = _PowerRestoreData.from_dict(
        {
            "last_lifetime_kwh": object(),
            "last_energy_ts": object(),
            "last_sample_ts": object(),
            "last_power_w": object(),
            "last_window_seconds": object(),
            "method": "",
            "last_reset_at": object(),
        }
    )
    assert invalid == empty


@pytest.mark.asyncio
async def test_power_restore_uses_private_extra_data(monkeypatch):
    """Derived power baselines survive restart without public attributes."""
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = "555555555555"
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {
        sn: {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 10.6,
            "last_reported_at": "2025-09-09T10:01:00Z",
            "charging": True,
        }
    }
    coord.serials = {sn}
    coord.site_id = "1234567"
    coord.last_update_success = True

    sensor = EnphasePowerSensor(coord, sn)

    class _LastExtra:
        def as_dict(self):
            return {
                "last_lifetime_kwh": 10.5,
                "last_energy_ts": 1_757_412_000.0,
                "last_sample_ts": 1_757_412_000.0,
                "last_power_w": 3600,
                "last_window_seconds": 300.0,
                "method": "lifetime_energy_window",
                "last_reset_at": None,
            }

    class _LastState:
        state = "3600"
        attributes = {}

    async def _last_extra():
        return _LastExtra()

    async def _last_state():
        return _LastState()

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(sensor, "async_get_last_extra_data", _last_extra)
    monkeypatch.setattr(sensor, "async_get_last_state", _last_state)
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop)

    await sensor.async_added_to_hass()

    assert sensor.native_value == 6000
    assert sensor.extra_state_attributes == {
        "sampled_at_utc": "2025-09-09T10:01:00+00:00",
        "last_window_seconds": pytest.approx(60.0),
        "method": "lifetime_energy_window",
        "actual_charging": True,
    }
    assert sensor.extra_restore_state_data.as_dict() == {
        "last_lifetime_kwh": 10.6,
        "last_energy_ts": 1_757_412_060.0,
        "last_sample_ts": 1_757_412_060.0,
        "last_power_w": 6000,
        "last_window_seconds": 60.0,
        "method": "lifetime_energy_window",
        "last_reset_at": None,
    }
