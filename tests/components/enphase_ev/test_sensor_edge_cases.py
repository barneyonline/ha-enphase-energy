from __future__ import annotations

import builtins
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.sensor import (
    EnphaseChargeModeSensor,
    EnphaseChargerAuthenticationSensor,
    EnphaseConnectorStatusSensor,
    EnphaseEnergyTodaySensor,
    EnphaseStatusSensor,
    _LastSessionRestoreData,
    _TimestampFromEpochSensor,
    _TimestampFromIsoSensor,
    EnphaseSiteBackoffEndsSensor,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


@pytest.mark.parametrize("timestamp", [1_700_000_000, "1700000000", 1_700_000_000_000])
def test_gateway_seconds_and_milliseconds_represent_same_utc_time(timestamp):
    from custom_components.enphase_ev.sensor_snapshot_helpers import (
        parse_gateway_timestamp,
    )

    assert parse_gateway_timestamp(timestamp) == datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc
    )


def test_base_sensor_native_value(coordinator_factory):
    coord = coordinator_factory(
        data={RANDOM_SERIAL: {"connector_status": "AVAILABLE", "sn": RANDOM_SERIAL}}
    )
    sensor = EnphaseConnectorStatusSensor(coord, RANDOM_SERIAL)
    assert sensor.native_value == "AVAILABLE"


def test_electrical_phase_sensor_edge_cases(coordinator_factory):
    from custom_components.enphase_ev.sensor import EnphaseElectricalPhaseSensor

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    class BadBool:
        def __bool__(self):
            raise RuntimeError("bad bool")

    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "phase_mode": None,
                "dlb_enabled": BadBool(),
                "dlb_active": BadBool(),
            }
        }
    )
    sensor = EnphaseElectricalPhaseSensor(coord, RANDOM_SERIAL)
    assert sensor._friendly_phase_mode(None) == (None, None)

    friendly, raw = sensor._friendly_phase_mode(BadStr())
    assert friendly is None
    assert isinstance(raw, BadStr)

    friendly, raw = sensor._friendly_phase_mode("bad")
    assert friendly == "bad"
    assert raw == "bad"

    friendly, raw = sensor._friendly_phase_mode("1")
    assert friendly == "Single Phase"
    assert raw == "1"

    attrs = sensor.extra_state_attributes
    assert attrs["dlb_enabled"] is None
    assert attrs["dlb_active"] is None


def test_charger_authentication_sensor_values_and_attributes(coordinator_factory):
    class BadBool:
        def __bool__(self):
            raise RuntimeError("bad bool")

    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "auth_required": True,
                "app_auth_enabled": True,
                "rfid_auth_enabled": False,
                "app_auth_supported": True,
                "rfid_auth_supported": True,
                "auth_feature_supported": True,
                "rfid_feature_supported": True,
                "plug_and_charge_supported": False,
            }
        }
    )
    sensor = EnphaseChargerAuthenticationSensor(coord, RANDOM_SERIAL)
    assert sensor.native_value == "enabled"
    assert sensor.available is True
    assert sensor.entity_category is None
    attrs = sensor.extra_state_attributes
    assert attrs["app_auth_enabled"] is True
    assert attrs["rfid_auth_enabled"] is False
    assert attrs["app_auth_supported"] is True
    assert attrs["rfid_auth_supported"] is True
    assert attrs["auth_feature_supported"] is True
    assert attrs["rfid_feature_supported"] is True
    assert attrs["plug_and_charge_supported"] is False

    coord.data[RANDOM_SERIAL]["app_auth_enabled"] = None
    coord.data[RANDOM_SERIAL]["rfid_auth_supported"] = BadBool()
    sensor_error = EnphaseChargerAuthenticationSensor(coord, RANDOM_SERIAL)
    attrs = sensor_error.extra_state_attributes
    assert attrs["app_auth_enabled"] is None
    assert attrs["rfid_auth_supported"] is None

    coord.data[RANDOM_SERIAL]["auth_required"] = False
    sensor_disabled = EnphaseChargerAuthenticationSensor(coord, RANDOM_SERIAL)
    assert sensor_disabled.native_value == "disabled"

    coord.data[RANDOM_SERIAL]["auth_feature_supported"] = False
    coord.data[RANDOM_SERIAL]["rfid_feature_supported"] = False
    coord.data[RANDOM_SERIAL]["plug_and_charge_supported"] = False
    sensor_hint_disabled = EnphaseChargerAuthenticationSensor(coord, RANDOM_SERIAL)
    assert sensor_hint_disabled.available is True

    coord.data[RANDOM_SERIAL]["auth_required"] = None
    sensor_unknown = EnphaseChargerAuthenticationSensor(coord, RANDOM_SERIAL)
    assert sensor_unknown.native_value is None


def test_charger_authentication_sensor_unavailable_when_service_down(
    coordinator_factory,
):
    coord = coordinator_factory(
        data={RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "auth_required": True}}
    )
    coord._auth_settings_available = False  # noqa: SLF001
    sensor = EnphaseChargerAuthenticationSensor(coord, RANDOM_SERIAL)
    assert sensor.available is False


def test_last_session_restore_data_handles_bad_values():
    class Boom:
        def __float__(self):
            raise ValueError("bad float")

        def __int__(self):
            raise ValueError("bad int")

    restored = _LastSessionRestoreData.from_dict(
        {
            "last_session_kwh": Boom(),
            "last_session_wh": Boom(),
            "last_session_start": Boom(),
            "last_session_end": Boom(),
            "session_key": "abc",
            "last_duration_min": Boom(),
        }
    )
    assert restored.last_session_kwh is None
    assert restored.last_duration_min is None


def test_last_session_metadata_attribute_edge_values(monkeypatch):
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor
    from custom_components.enphase_ev import sensor as sensor_mod

    class BadFloat:
        def __float__(self):
            raise ValueError("bad float")

    class BadStr:
        def __str__(self):
            raise ValueError("bad str")

    bad_float = BadFloat()
    bad_str = BadStr()

    attrs = EnphaseEnergyTodaySensor._session_metadata_attributes(
        {},
        hass=None,
        context={
            "cost_calculated": 1,
            "manual_override": "yes",
            "active_charge_time_s": bad_float,
            "avg_cost_per_kwh": bad_float,
            "charge_profile_stack_level": bad_float,
            "session_id": bad_str,
        },
        energy_kwh=1.0,
        energy_wh=1000.0,
        duration_min=None,
        session_key=None,
    )
    assert attrs["cost_calculated"] is True
    assert attrs["manual_override"] is True
    assert attrs["active_charge_time_s"] is None
    assert attrs["avg_cost_per_kwh"] is None
    assert attrs["charge_profile_stack_level"] is None
    assert "session_id" not in attrs

    attrs_unknown = EnphaseEnergyTodaySensor._session_metadata_attributes(
        {},
        hass=None,
        context={"cost_calculated": [], "manual_override": []},
        energy_kwh=1.0,
        energy_wh=1000.0,
        duration_min=None,
        session_key=None,
    )
    assert attrs_unknown["cost_calculated"] is None
    assert attrs_unknown["manual_override"] is None

    original_round = builtins.round

    def fake_round(value, ndigits=None):
        if value == 1.234 and ndigits == 3:
            raise RuntimeError("boom")
        if ndigits is None:
            return original_round(value)
        return original_round(value, ndigits)

    monkeypatch.setattr(builtins, "round", fake_round)
    attrs_round = EnphaseEnergyTodaySensor._session_metadata_attributes(
        {},
        hass=None,
        context={"avg_cost_per_kwh": 1.234},
        energy_kwh=1.0,
        energy_wh=1000.0,
        duration_min=None,
        session_key=None,
    )
    assert attrs_round["avg_cost_per_kwh"] == 1.234

    import types

    code = EnphaseEnergyTodaySensor._session_metadata_attributes.__code__
    as_float_code = next(
        const
        for const in code.co_consts
        if isinstance(const, types.CodeType) and const.co_name == "_as_float"
    )
    assert not as_float_code.co_freevars
    as_float = types.FunctionType(as_float_code, sensor_mod.__dict__)
    as_float.__kwdefaults__ = {"precision": None}
    assert as_float("1.25") == 1.25


@pytest.mark.asyncio
async def test_last_session_restore_state_handles_bad_inputs(hass, coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)
    sensor.hass = hass

    class ExplodingStr:
        def __str__(self):
            raise ValueError("explode")

        def __bool__(self):
            return True

    class ExplodingInt:
        def __int__(self):
            raise ValueError("explode")

        def __bool__(self):
            return True

    bad_state = State(
        "sensor.enphase_bad",
        "boom",
        attributes={
            "session_key": ExplodingStr(),
            "session_duration_min": ExplodingInt(),
        },
    )
    sensor.async_get_last_state = AsyncMock(return_value=bad_state)
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)

    await sensor.async_added_to_hass()
    assert sensor._session_key is None
    assert sensor._last_duration_min is None


def test_coerce_timestamp_edges():
    class BadFloat(float):
        def __float__(self):
            raise ValueError("bad float")

    assert EnphaseEnergyTodaySensor._coerce_timestamp(BadFloat(1.0)) is None
    assert EnphaseEnergyTodaySensor._coerce_timestamp("not-a-date") is None
    assert EnphaseEnergyTodaySensor._coerce_timestamp("2024-01-01T00:00:00") is not None
    assert EnphaseEnergyTodaySensor._coerce_timestamp([]) is None


def test_status_sensor_offline_since_edge_cases(monkeypatch, coordinator_factory):
    from custom_components.enphase_ev.sensor import EnphaseStatusSensor

    monkeypatch.setattr(dt_util, "as_local", lambda dt: dt)

    coord = coordinator_factory(
        data={RANDOM_SERIAL: {"sn": RANDOM_SERIAL, "offline_since": None}}
    )
    sensor = EnphaseStatusSensor(coord, RANDOM_SERIAL)

    coord.data[RANDOM_SERIAL]["offline_since"] = 0
    assert sensor.extra_state_attributes["offline_since"].startswith(
        "1970-01-01T00:00:00"
    )

    coord.data[RANDOM_SERIAL]["offline_since"] = " "
    assert sensor.extra_state_attributes["offline_since"] is None

    coord.data[RANDOM_SERIAL]["offline_since"] = "2025-01-01T00:00:00Z[UTC]"
    assert sensor.extra_state_attributes["offline_since"] == "2025-01-01T00:00:00+00:00"

    coord.data[RANDOM_SERIAL]["offline_since"] = "2025-01-01T00:00:00"
    assert sensor.extra_state_attributes["offline_since"] == "2025-01-01T00:00:00+00:00"

    coord.data[RANDOM_SERIAL]["offline_since"] = []
    assert sensor.extra_state_attributes["offline_since"] is None

    coord.data[RANDOM_SERIAL]["offline_since"] = "bad-date"
    assert sensor.extra_state_attributes["offline_since"] is None


def test_coerce_energy_exception_paths(monkeypatch, coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)

    class BoomFloat(float):
        def __float__(self):
            raise ValueError("bad float")

    kwh, wh = sensor._coerce_energy(BoomFloat(1.0), None)
    assert kwh is None and wh is None

    kwh, wh = sensor._coerce_energy(None, BoomFloat(2.0))
    assert kwh is None and wh is None

    call_count = 0
    orig_round = round

    def boom_round(val, ndigits=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ValueError("round failure")
        return orig_round(val, ndigits) if ndigits is not None else orig_round(val)

    monkeypatch.setattr("builtins.round", boom_round)
    kwh, wh = sensor._coerce_energy(1.0, None)
    assert kwh == 1.0
    assert wh is None


def test_extract_history_session_bad_session_key(coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)

    class BadStr:
        def __str__(self):
            raise ValueError("nope")

    data = {
        "energy_today_sessions": [
            {
                "energy_kwh_total": 1,
                "start": 1,
                "end": 2,
                "session_id": BadStr(),
            }
        ]
    }
    session = sensor._extract_history_session(data)
    assert session is not None
    assert session["session_key"].startswith("1")


def test_compute_duration_minutes_edge_cases(coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)
    assert sensor._compute_duration_minutes(1, None, False) is None
    assert sensor._compute_duration_minutes("a", "b", True) is None


def test_last_session_native_value_error_paths(monkeypatch, coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)

    class BadFloat(float):
        def __float__(self):
            raise ValueError("bad")

    sensor._pick_session_context = lambda _d: {
        "energy_kwh": BadFloat(1.0),
        "energy_wh": None,
        "start": None,
        "end": None,
        "charging": False,
        "session_key": "bad",
    }
    assert sensor.native_value is None

    class BadFloat2(float):
        def __float__(self):
            raise ValueError("bad2")

    sensor._pick_session_context = lambda _d: {
        "energy_kwh": 0.1,
        "energy_wh": BadFloat2(1.0),
        "start": None,
        "end": None,
        "charging": False,
        "session_key": "mid",
    }
    assert sensor.native_value == 0.1

    # Force round failures to exercise exception handlers
    sensor._session_key = "keep"
    call_count = 0

    def boom_round(val, ndigits=None):
        nonlocal call_count
        call_count += 1
        raise ValueError(f"fail {call_count}")

    with monkeypatch.context() as m:
        m.setattr("builtins.round", boom_round)
        sensor._pick_session_context = lambda _d: {
            "energy_kwh": 1.0,
            "energy_wh": None,
            "start": 10.0,
            "end": 20.0,
            "charging": False,
            "session_key": "keep",
        }
        sensor.native_value

    # When round fails and session key unchanged, fallback leaves last_session_wh unchanged
    sensor._session_key = "keep"
    sensor._last_session_wh = 123.0
    with monkeypatch.context() as m:
        m.setattr(
            "builtins.round",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("fail")),
        )
        sensor._pick_session_context = lambda _d: {
            "energy_kwh": 2.0,
            "energy_wh": None,
            "start": 0.0,
            "end": 1.0,
            "charging": False,
            "session_key": "keep",
        }
        sensor.native_value
    assert sensor._last_session_wh == 123.0

    sensor._pick_session_context = lambda _d: {
        "energy_kwh": 2.0,
        "energy_wh": 3.0,
        "start": 1.0,
        "end": 5.0,
        "charging": False,
        "session_key": sensor._session_key,
    }
    sensor.native_value
    assert sensor._last_session_kwh == 2.0
    assert sensor._last_session_wh == 3.0
    assert sensor._last_session_end == 5.0


def test_last_session_pick_context_negative_energy(coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)
    context = sensor._pick_session_context(
        {
            "session_kwh": -1,
            "session_energy_wh": None,
            "charging": False,
        }
    )
    assert context["energy_kwh"] == pytest.approx(-1.0)


def test_last_session_native_value_rounds_energy_wh(monkeypatch, coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)
    sensor._pick_session_context = lambda _d: {
        "energy_kwh": 1.5,
        "energy_wh": None,
        "start": 0,
        "end": 1,
        "charging": False,
        "session_key": "same",
    }
    sensor._session_key = "same"
    sensor._last_session_wh = None
    sensor.native_value
    assert sensor._last_session_wh == pytest.approx(1500.0)


def test_status_sensor_bool_parsing(coordinator_factory):
    sensor = EnphaseStatusSensor(coordinator_factory(), RANDOM_SERIAL)

    class BadBool:
        def __bool__(self):
            raise ValueError("boom")

    sensor.data.update({"commissioned": None, "faulted": BadBool()})
    attrs = sensor.extra_state_attributes
    assert attrs["commissioned"] is None
    assert attrs["charger_problem"] is None


def test_last_session_energy_wh_computation(coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)
    sensor._pick_session_context = lambda _d: {
        "energy_kwh": 0.5,
        "energy_wh": None,
        "start": None,
        "end": None,
        "charging": False,
        "session_key": "new",
    }
    assert sensor.native_value == pytest.approx(0.5)
    assert sensor._last_session_wh == pytest.approx(500.0)


def test_last_session_wh_calculation_same_session(coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)
    sensor._session_key = "keep"
    sensor._last_session_wh = None
    sensor._pick_session_context = lambda _d: {
        "energy_kwh": 0.2,
        "energy_wh": None,
        "start": 0,
        "end": 1,
        "charging": False,
        "session_key": "keep",
    }
    sensor.native_value
    assert sensor._last_session_wh == pytest.approx(200.0)


def test_last_session_energy_wh_round_failure(monkeypatch, coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)
    call_counter = 0
    real_round = round

    def boom_round(val, ndigits=None):
        nonlocal call_counter
        call_counter += 1
        if call_counter == 2:
            raise ValueError("boom")
        return real_round(val, ndigits) if ndigits is not None else real_round(val)

    sensor._pick_session_context = lambda _d: {
        "energy_kwh": 1.0,
        "energy_wh": None,
        "start": None,
        "end": None,
        "charging": False,
        "session_key": "first",
    }
    with monkeypatch.context() as m:
        m.setattr("builtins.round", boom_round)
        sensor.native_value
    assert sensor._last_session_wh == pytest.approx(1000.0)


def test_last_session_wh_round_failure_same_session(monkeypatch, coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)
    sensor._session_key = "keep"
    real_round = round
    call_count = 0

    def boom_round(val, ndigits=None):
        nonlocal call_count
        call_count += 1
        if call_count in (2, 3):
            raise ValueError("bad round")
        return real_round(val, ndigits) if ndigits is not None else real_round(val)

    sensor._pick_session_context = lambda _d: {
        "energy_kwh": 0.3,
        "energy_wh": None,
        "start": None,
        "end": None,
        "charging": False,
        "session_key": "keep",
    }
    with monkeypatch.context() as m:
        m.setattr("builtins.round", boom_round)
        sensor.native_value
    assert sensor._last_session_kwh == pytest.approx(0.3)
    assert sensor._last_session_wh is None


def test_last_session_merge_history_start_end_match(coordinator_factory):
    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)
    base = datetime(2025, 1, 5, 8, 0, 0, tzinfo=timezone.utc)
    end = base + timedelta(minutes=10)
    sensor._data = {
        "energy_today_sessions": [
            {
                "session_id": "hist-1",
                "start": base.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
                "energy_kwh_total": 1.0,
                "session_cost": 1.23,
            }
        ]
    }
    sensor._last_context_source = "realtime"
    context = {
        "session_key": "rt-1",
        "start": base.timestamp() + 0.4,
        "end": end.timestamp() + 0.4,
    }
    merged = sensor._merge_history_context(context)
    assert merged["session_cost"] == pytest.approx(1.23)


def test_last_session_merge_history_end_match_with_bad_float(coordinator_factory):
    class BadFloat:
        def __float__(self):
            raise ValueError("boom")

    sensor = EnphaseEnergyTodaySensor(coordinator_factory(), RANDOM_SERIAL)
    end = datetime(2025, 2, 2, 9, 30, 0, tzinfo=timezone.utc)
    sensor._data = {
        "energy_today_sessions": [
            {
                "session_id": "hist-2",
                "end": end.isoformat().replace("+00:00", "Z"),
                "energy_kwh_total": 2.0,
                "session_cost": 2.5,
            }
        ]
    }
    sensor._last_context_source = "realtime"
    context = {
        "session_key": "rt-2",
        "start": BadFloat(),
        "end": end.timestamp() + 0.6,
    }
    merged = sensor._merge_history_context(context)
    assert merged["session_cost"] == pytest.approx(2.5)


def test_timestamp_sensors_parsing(coordinator_factory):
    iso_sensor = _TimestampFromIsoSensor(
        coordinator_factory(), RANDOM_SERIAL, "ts", "TS", "uniq-ts"
    )
    iso_sensor.data["ts"] = "2024-01-01T00:00:00Z"
    assert iso_sensor.native_value is not None

    epoch_sensor = _TimestampFromEpochSensor(
        coordinator_factory(), RANDOM_SERIAL, "ts", "TS", "uniq-epoch"
    )
    epoch_sensor.data["ts"] = None
    assert epoch_sensor.native_value is None


def test_session_metadata_attributes_error_branches(monkeypatch):
    sensor = EnphaseEnergyTodaySensor(SimpleNamespace(data={}), RANDOM_SERIAL)

    class RoundBoom:
        def __float__(self):
            return 1.234

        def __round__(self, _ndigits=None):
            raise ValueError("no round")

    class Boom:
        def __float__(self):
            raise ValueError("bad float")

        def __int__(self):
            raise ValueError("bad int")

    class BadConfig:
        @property
        def units(self):
            raise RuntimeError("boom")

    hass_broken = SimpleNamespace(config=BadConfig())

    bad_energy = Boom()
    bad_wh = Boom()
    bad_cost = Boom()
    bad_charge = Boom()
    bad_miles = Boom()

    attrs = sensor._session_metadata_attributes(
        {
            "session_plug_in_at": "bad",
            "session_plug_out_at": {},
            "session_kwh": Boom(),
            "session_energy_wh": Boom(),
            "session_cost": bad_cost,
            "session_charge_level": bad_charge,
            "session_miles": bad_miles,
        },
        hass=hass_broken,
        context={
            "energy_kwh": bad_energy,
            "energy_wh": bad_wh,
            "session_cost": bad_cost,
            "session_charge_level": bad_charge,
            "session_miles": bad_miles,
        },
    )
    assert attrs["plugged_in_at"] is None
    assert attrs["plugged_out_at"] is None
    assert attrs["energy_consumed_kwh"] is None
    assert attrs["energy_consumed_wh"] is None
    assert attrs["session_cost"] is bad_cost
    assert attrs["session_charge_level"] is bad_charge
    assert attrs["range_added"] is None

    call_counter = 0
    orig_round = round

    def boom_round(val, ndigits=None):
        nonlocal call_counter
        call_counter += 1
        if call_counter == 2:
            raise ValueError("rounding error")
        return orig_round(val, ndigits) if ndigits is not None else orig_round(val)

    with monkeypatch.context() as m:
        m.setattr("builtins.round", boom_round)
        attrs = sensor._session_metadata_attributes(
            {
                "session_plug_in_at": " ",
                "session_plug_out_at": "2024-01-01T00:00:00",
                "session_miles": 10,
            },
            hass=SimpleNamespace(config=SimpleNamespace(units=None)),
            context={"energy_kwh": 1.0, "energy_wh": None},
        )
    assert attrs["plugged_in_at"] is None
    assert attrs["plugged_out_at"] is not None
    assert attrs["energy_consumed_kwh"] == 1.0
    assert attrs["energy_consumed_wh"] is None
    assert attrs["range_added"] == 10


def test_connector_status_icon_default(coordinator_factory):
    coord = coordinator_factory(data={RANDOM_SERIAL: {"connector_status": "unknown"}})
    sensor = EnphaseConnectorStatusSensor(coord, RANDOM_SERIAL)
    assert sensor.icon == "mdi:ev-station"


def test_charge_mode_icon_and_value(coordinator_factory):
    coord = coordinator_factory(
        data={RANDOM_SERIAL: {"charge_mode_pref": None, "charge_mode": "idle"}}
    )
    sensor = EnphaseChargeModeSensor(coord, RANDOM_SERIAL)
    assert sensor.native_value == "idle"
    assert sensor.icon == "mdi:timer-sand-paused"


def test_status_sensor_bool_errors(coordinator_factory):
    class ExplodingBool:
        def __bool__(self):
            raise ValueError("boom")

    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {"commissioned": ExplodingBool(), "faulted": ExplodingBool()}
        }
    )
    sensor = EnphaseStatusSensor(coord, RANDOM_SERIAL)
    attrs = sensor.extra_state_attributes
    assert attrs["commissioned"] is None
    assert attrs["charger_problem"] is None


def test_timestamp_sensors_parse_errors(coordinator_factory):
    coord_iso = coordinator_factory(data={RANDOM_SERIAL: {"foo": "bad timestamp"}})
    iso_sensor = _TimestampFromIsoSensor(coord_iso, RANDOM_SERIAL, "foo", "Foo", "uniq")
    assert iso_sensor.native_value is None

    coord_epoch = coordinator_factory(data={RANDOM_SERIAL: {"bar": "abc"}})
    epoch_sensor = _TimestampFromEpochSensor(
        coord_epoch, RANDOM_SERIAL, "bar", "Bar", "uniq2"
    )
    assert epoch_sensor.native_value is None

    coord_iso_empty = coordinator_factory(data={RANDOM_SERIAL: {"foo": ""}})
    iso_sensor_empty = _TimestampFromIsoSensor(
        coord_iso_empty, RANDOM_SERIAL, "foo", "Foo", "uniq3"
    )
    assert iso_sensor_empty.native_value is None

    coord_epoch_bad = coordinator_factory(data={RANDOM_SERIAL: {"bar": "nan"}})
    epoch_sensor_bad = _TimestampFromEpochSensor(
        coord_epoch_bad, RANDOM_SERIAL, "bar", "Bar", "uniq4"
    )
    assert epoch_sensor_bad.native_value is None


def test_status_sensor_truthy(coordinator_factory):
    coord = coordinator_factory(
        data={RANDOM_SERIAL: {"commissioned": True, "faulted": False}}
    )
    sensor = EnphaseStatusSensor(coord, RANDOM_SERIAL)
    attrs = sensor.extra_state_attributes
    assert attrs["commissioned"] is True
    assert attrs["charger_problem"] is False


def test_charging_level_sensor_optional_bool_helper(coordinator_factory):
    from custom_components.enphase_ev.sensor import EnphaseChargingLevelSensor

    sensor = EnphaseChargingLevelSensor(coordinator_factory(), RANDOM_SERIAL)

    assert sensor._optional_bool(1) is True
    assert sensor._optional_bool("enabled") is True
    assert sensor._optional_bool("off") is False
    assert sensor._optional_bool("maybe") is None


def test_status_sensor_normalizes_state_text(coordinator_factory):
    coord = coordinator_factory(data={RANDOM_SERIAL: {"status": "NORMAL"}})
    sensor = EnphaseStatusSensor(coord, RANDOM_SERIAL)
    assert sensor.native_value == "Normal"

    coord.data[RANDOM_SERIAL]["status"] = "NOT_READY"
    assert sensor.native_value == "Not Ready"

    coord.data[RANDOM_SERIAL]["status"] = "SUSPENDED_EVSE"
    assert sensor.native_value == "Suspended EVSE"

    coord.data[RANDOM_SERIAL]["status"] = "rfid_api_ac_dc"
    assert sensor.native_value == "RFID API AC DC"
    assert sensor.extra_state_attributes["status_raw"] == "rfid_api_ac_dc"

    coord.data[RANDOM_SERIAL]["status"] = "ac/dc"
    assert sensor.native_value == "AC/DC"


def test_status_sensor_normalize_status_edge_cases(coordinator_factory):
    sensor = EnphaseStatusSensor(coordinator_factory(), RANDOM_SERIAL)
    assert sensor._normalize_status(None) is None
    assert sensor._normalize_status("   ") is None
    assert sensor._normalize_status("__-__") is None
    assert sensor._normalize_status("///") is None

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    assert sensor._normalize_status(BadStr()) is None
    sensor.data["status"] = BadStr()
    assert sensor.extra_state_attributes["status_raw"] is None


def test_site_base_entity_diagnostics(monkeypatch, coordinator_factory):
    coord = coordinator_factory()
    coord.last_success_utc = dt_util.utcnow()
    coord.last_failure_utc = coord.last_success_utc - timedelta(seconds=1)
    coord.last_failure_status = 503
    coord.last_failure_description = "timeout"
    coord.last_failure_response = "bad"
    coord.last_failure_source = "network"
    coord.last_failure_endpoint = "/service/evse_controller/[site]/ev_chargers/status"
    coord.payload_failure_kind = "json_decode"
    coord.payload_using_stale = True
    site_sensor = EnphaseSiteBackoffEndsSensor(coord)
    site_sensor.hass = SimpleNamespace()
    coord.backoff_ends_utc = None
    assert site_sensor.available is True
    attrs = site_sensor._cloud_diag_attrs(include_last_success=False)
    assert "last_success_utc" not in attrs
    assert "last_failure_status" in attrs
    assert attrs["last_failure_endpoint"].endswith("/ev_chargers/status")
    assert attrs["payload_failure_kind"] == "json_decode"
    assert attrs["payload_using_stale"] is True
    assert site_sensor._backoff_remaining_seconds() is None

    coord.backoff_ends_utc = "bad"
    assert site_sensor._backoff_remaining_seconds() is None

    coord.backoff_ends_utc = dt_util.utcnow() + timedelta(seconds=0.4)
    assert site_sensor._backoff_remaining_seconds() == 1
    attrs = site_sensor._cloud_diag_attrs(include_last_success=False)
    assert attrs["backoff_ends_utc"] == coord.backoff_ends_utc.isoformat()
    assert site_sensor.device_info["identifiers"]

    coord.last_success_utc = None
    assert isinstance(site_sensor.available, bool)

    fixed_now = dt_util.utcnow()
    coord.backoff_ends_utc = fixed_now + timedelta(seconds=5)
    monkeypatch.setattr("homeassistant.util.dt.utcnow", lambda: fixed_now)
    assert site_sensor._backoff_remaining_seconds() == 5
