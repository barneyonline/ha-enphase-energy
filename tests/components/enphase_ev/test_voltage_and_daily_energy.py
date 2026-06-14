from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SERIAL_ALT


def test_power_derived_from_lifetime_delta(monkeypatch):
    import datetime as _dt

    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    sn = RANDOM_SERIAL_ALT
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {
        sn: {
            "sn": sn,
            "name": "Garage EV",
            "lifetime_kwh": 10.0,
            "operating_v": 230,
            "charging": True,
        }
    }
    coord.serials = {sn}

    ent = EnphasePowerSensor(coord, sn)

    t0 = _dt.datetime(2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(dt_util, "now", lambda: t0)
    assert ent.native_value == 0

    t1 = t0 + _dt.timedelta(seconds=120)
    monkeypatch.setattr(dt_util, "now", lambda: t1)
    coord.data[sn]["lifetime_kwh"] = 10.24
    assert ent.native_value == 7200


def test_last_session_sensor_name_and_value(monkeypatch):
    import datetime as _dt

    from homeassistant.util import dt as dt_util

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    sn = RANDOM_SERIAL
    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {
        sn: {
            "sn": sn,
            "name": "IQ EV Charger",
            "session_energy_wh": 1000,
            "session_start": _dt.datetime(
                2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc
            ).timestamp(),
            "session_end": _dt.datetime(
                2025, 9, 9, 11, 0, 0, tzinfo=_dt.timezone.utc
            ).timestamp(),
        }
    }
    coord.serials = {sn}

    monkeypatch.setattr(
        dt_util,
        "utcnow",
        lambda: _dt.datetime(2025, 9, 9, 10, 0, 0, tzinfo=_dt.timezone.utc),
    )

    ent = EnphaseEnergyTodaySensor(coord, sn)
    assert ent.translation_key == "last_session"
    assert ent.native_value == 1.0
