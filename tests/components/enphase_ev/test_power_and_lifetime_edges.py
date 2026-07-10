from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import State

from custom_components.enphase_ev.sensor import (
    EnphaseLifetimeEnergySensor,
    EnphasePowerSensor,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


@pytest.mark.asyncio
async def test_power_restore_missing_state(hass, coordinator_factory):
    sensor = EnphasePowerSensor(coordinator_factory(), RANDOM_SERIAL)
    sensor.hass = hass
    sensor.async_get_last_state = AsyncMock(return_value=None)
    await sensor.async_added_to_hass()


@pytest.mark.asyncio
async def test_power_restore_handles_bad_attrs(hass, coordinator_factory):
    class Boom:
        def __float__(self):
            raise ValueError("bad float")

        def __int__(self):
            raise ValueError("bad int")

    attrs = {
        "last_lifetime_kwh": Boom(),
        "last_energy_ts": Boom(),
        "last_sample_ts": Boom(),
        "last_power_w": Boom(),
        "last_window_seconds": Boom(),
        "method": "legacy",
        "last_reset_at": Boom(),
        "baseline_kwh": Boom(),
        "last_energy_today_kwh": Boom(),
        "last_ts": Boom(),
    }
    bad_state = State("sensor.enphase_power", "invalid", attributes=attrs)

    sensor = EnphasePowerSensor(coordinator_factory(), RANDOM_SERIAL)
    sensor.hass = hass
    sensor.async_get_last_state = AsyncMock(return_value=bad_state)
    await sensor.async_added_to_hass()
    assert sensor._last_power_w == 0
    assert sensor._last_window_s is None


def test_power_parse_timestamp_variants(coordinator_factory):
    sensor = EnphasePowerSensor(coordinator_factory(), RANDOM_SERIAL)
    assert sensor._parse_timestamp(1_600_000_000_000) == 1_600_000_000
    assert sensor._parse_timestamp("") is None
    assert sensor._parse_timestamp("bad-ts") is None
    assert sensor._parse_timestamp("2024-01-01T00:00:00") is not None
    assert sensor._parse_timestamp([]) is None


def test_power_resolve_max_throughput_negative_amp(coordinator_factory):
    sensor = EnphasePowerSensor(coordinator_factory(), RANDOM_SERIAL)
    watts, source, amps, voltage, unbounded, topology, phase_multiplier = (
        sensor._resolve_max_throughput({"operating_v": 240, "session_charge_level": -1})
    )
    assert watts == sensor._STATIC_MAX_WATTS
    assert source == "static_default"
    assert unbounded == sensor._STATIC_MAX_WATTS
    assert topology == "unknown"
    assert phase_multiplier == pytest.approx(1.0)

    watts, source, amps, voltage, unbounded, topology, phase_multiplier = (
        sensor._resolve_max_throughput(
            {"operating_v": 240, "session_charge_level": 0.0001}
        )
    )
    assert watts == sensor._STATIC_MAX_WATTS
    assert amps is None
    assert unbounded == sensor._STATIC_MAX_WATTS
    assert topology == "unknown"
    assert phase_multiplier == pytest.approx(1.0)


def test_power_resolve_max_throughput_uses_nominal_fallback(coordinator_factory):
    coord = coordinator_factory()
    coord._nominal_v = 120
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)

    watts, source, amps, voltage, unbounded, topology, phase_multiplier = (
        sensor._resolve_max_throughput({"session_charge_level": 16})
    )
    assert source == "session_charge_level"
    assert amps == pytest.approx(16.0)
    assert voltage == pytest.approx(120.0)
    assert watts == 1920
    assert unbounded == 1920
    assert topology == "unknown"
    assert phase_multiplier == pytest.approx(1.0)
    assert sensor._max_throughput_voltage == pytest.approx(120.0)

    watts, source, amps, voltage, unbounded, topology, phase_multiplier = (
        sensor._resolve_max_throughput({"nominal_v": 230, "session_charge_level": 16})
    )
    assert source == "session_charge_level"
    assert amps == pytest.approx(16.0)
    assert voltage == pytest.approx(230.0)
    assert watts == 3680
    assert unbounded == 3680
    assert topology == "unknown"
    assert phase_multiplier == pytest.approx(1.0)


def test_power_topology_normalization_and_fallbacks(coordinator_factory):
    sensor = EnphasePowerSensor(coordinator_factory(), RANDOM_SERIAL)

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    assert sensor._power_topology({"phase_mode": BadStr()}) == "unknown"
    assert sensor._power_topology({"phase_mode": "three phase"}) == "three_phase"
    assert sensor._power_topology({"phase_mode": "single-phase"}) == "single_phase"
    assert sensor._power_topology({"phase_mode": "split"}) == "split_phase"
    assert sensor._power_topology({"phase_count": 1}) == "single_phase"
    assert sensor._power_topology({"phase_count": 3}) == "three_phase"
    assert sensor._power_topology({"phase_count": 2}) == "unknown"
    assert sensor._three_phase_multiplier(
        {"wiring_configuration": {BadStr(): "L1"}}
    ) == pytest.approx(1.7320508075688772)
    assert sensor._three_phase_multiplier(
        {"wiring_configuration": {"L1": "L1"}}
    ) == pytest.approx(1.7320508075688772)
    assert sensor._three_phase_multiplier(
        {"wiring_configuration": {"L1": "L1", "Neutral": "N"}}
    ) == pytest.approx(3.0)


def test_power_native_value_idle_and_defaults(monkeypatch, coordinator_factory):
    coord = coordinator_factory(data={RANDOM_SERIAL: {"charging": False}})
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    with patch(
        "custom_components.enphase_ev.sensor.dt_util.now",
        return_value=datetime(2024, 1, 1, 0, 0, 0),
    ):
        assert sensor.native_value == 0
        assert sensor._last_method == "idle"


def test_power_native_value_string_false_not_treated_as_charging(coordinator_factory):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 10.0,
                "last_reported_at": 1200,
                "charging": "false",
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    sensor._last_lifetime_kwh = 9.5
    sensor._last_energy_ts = 900
    sensor._last_power_w = 123

    assert sensor.native_value == 0
    assert sensor._last_method == "idle"
    assert sensor.extra_state_attributes["actual_charging"] is False


def test_power_actual_charging_attribute_prefers_precomputed_flag(coordinator_factory):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 10.0,
                "last_reported_at": 1200,
                "charging": True,
                "actual_charging": False,
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    sensor._last_lifetime_kwh = 9.5
    sensor._last_energy_ts = 900
    sensor._last_power_w = 123

    assert sensor.native_value == 0
    attrs = sensor.extra_state_attributes
    assert attrs["actual_charging"] is False


def test_power_native_value_default_window(coordinator_factory):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 2.0,
                "last_reported_at": 1000,
                "charging": True,
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    sensor._last_lifetime_kwh = 1.0
    sensor._last_energy_ts = None
    value = sensor.native_value
    assert value > 0
    assert sensor._last_window_s == sensor._DEFAULT_WINDOW_S


def test_power_uses_stable_sampled_at_when_report_time_missing(coordinator_factory):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 2.0,
                "sampled_at_utc": "2026-03-11T05:40:00+00:00",
                "sampled_at_ts": 1_741_672_800.0,
                "charging": True,
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    sensor._last_lifetime_kwh = 1.0
    sensor._last_energy_ts = None

    value = sensor.native_value

    assert value > 0
    assert (
        sensor.extra_state_attributes["sampled_at_utc"] == "2026-03-11T05:40:00+00:00"
    )
    assert sensor._last_sample_ts == pytest.approx(1_741_672_800.0)


def test_power_uses_precomputed_snapshot_without_mutating_between_reads(
    coordinator_factory,
):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 2.0,
                "sampled_at_utc": "2026-03-11T05:40:00+00:00",
                "derived_power_w": 7200,
                "derived_power_method": "lifetime_energy_window",
                "derived_power_window_seconds": 300.0,
                "derived_last_lifetime_kwh": 2.0,
                "derived_last_energy_ts": 1_741_672_800.0,
                "derived_last_sample_ts": 1_741_672_800.0,
                "derived_last_reset_at": None,
                "derived_power_max_throughput_w": 7680,
                "derived_power_max_throughput_unbounded_w": 7680,
                "derived_power_max_throughput_source": "charging_level",
                "derived_power_max_throughput_amps": 32.0,
                "derived_power_max_throughput_voltage": 240.0,
                "derived_power_max_throughput_topology": "single_phase",
                "derived_power_max_throughput_phase_multiplier": 1.0,
                "charging": True,
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)

    first = sensor.native_value
    second = sensor.native_value
    attrs = sensor.extra_state_attributes

    assert first == 7200
    assert second == 7200
    assert attrs["method"] == "lifetime_energy_window"
    assert attrs["last_window_seconds"] == pytest.approx(300.0)
    assert attrs["sampled_at_utc"] == "2026-03-11T05:40:00+00:00"


def test_power_snapshot_uses_nominal_voltage_fallback_when_missing(
    coordinator_factory,
):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "derived_power_w": 0,
                "derived_power_method": "idle",
                "derived_power_max_throughput_w": 7680,
                "derived_power_max_throughput_unbounded_w": 7680,
                "derived_power_max_throughput_source": "charging_level",
                "derived_power_max_throughput_amps": 32.0,
                "derived_power_max_throughput_voltage": None,
                "charging": False,
            }
        }
    )
    coord._nominal_v = 208
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)

    assert sensor.native_value == 0
    assert sensor._max_throughput_voltage == pytest.approx(208.0)


def test_power_native_value_suspended_connector_resets_power(coordinator_factory):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 10.0,
                "last_reported_at": 1200,
                "charging": True,
                "connector_status": "SUSPENDED",
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    sensor._last_lifetime_kwh = 10.0
    sensor._last_energy_ts = 600
    sensor._last_power_w = 123
    assert sensor.native_value == 0
    assert sensor._last_method == "idle"
    assert sensor.extra_state_attributes["actual_charging"] is False


def test_power_native_value_suspended_ev_resets_power(coordinator_factory):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 10.0,
                "last_reported_at": 1200,
                "charging": True,
                "connector_status": "SUSPENDED_EV",
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    sensor._last_lifetime_kwh = 9.5
    sensor._last_energy_ts = 900
    sensor._last_power_w = 123

    assert sensor.native_value == 0
    assert sensor._last_method == "idle"
    attrs = sensor.extra_state_attributes
    assert attrs["actual_charging"] is False


def test_power_native_value_suspended_by_evse_resets_power(coordinator_factory):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 10.0,
                "last_reported_at": 1200,
                "charging": True,
                "suspended_by_evse": True,
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    sensor._last_lifetime_kwh = 10.0
    sensor._last_energy_ts = 600
    sensor._last_power_w = 321
    assert sensor.native_value == 0
    assert sensor._last_method == "idle"
    assert sensor.extra_state_attributes["actual_charging"] is False


def test_power_native_value_suspended_evse_resets_power(coordinator_factory):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 10.0,
                "last_reported_at": 1200,
                "charging": True,
                "connector_status": "SUSPENDED_EVSE",
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    sensor._last_lifetime_kwh = 9.5
    sensor._last_energy_ts = 900
    sensor._last_power_w = 321

    assert sensor.native_value == 0
    assert sensor._last_method == "idle"
    assert sensor.extra_state_attributes["actual_charging"] is False


def test_power_native_value_ignores_delta_when_not_charging(coordinator_factory):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 10.0,
                "last_reported_at": 1000,
                "charging": False,
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    sensor._last_lifetime_kwh = 9.0
    sensor._last_energy_ts = 500
    sensor._last_power_w = 123
    assert sensor.native_value == 0
    assert sensor._last_lifetime_kwh == 10.0
    assert sensor._last_energy_ts == 1000
    assert sensor._last_method == "idle"
    assert sensor.extra_state_attributes["actual_charging"] is False


def test_power_native_value_negative_delta(monkeypatch, coordinator_factory):
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "lifetime_kwh": 4.0,
                "last_reported_at": 10,
                "charging": True,
            }
        }
    )
    sensor = EnphasePowerSensor(coord, RANDOM_SERIAL)
    sensor._RESET_DROP_KWH = 999
    sensor._MIN_DELTA_KWH = -999
    sensor._last_lifetime_kwh = 5.0
    sensor._last_energy_ts = 0
    sensor._last_power_w = 123
    watts = sensor.native_value
    assert watts == 0


@pytest.mark.asyncio
async def test_power_restore_legacy_last_ts_failure(hass, coordinator_factory):
    attrs = {
        "baseline_kwh": 1.0,
        "last_energy_today_kwh": 1.0,
        "last_ts": object(),
    }
    bad_state = State("sensor.power", "0", attributes=attrs)
    sensor = EnphasePowerSensor(coordinator_factory(), RANDOM_SERIAL)
    sensor.hass = hass
    sensor.async_get_last_state = AsyncMock(return_value=bad_state)
    await sensor.async_added_to_hass()
    assert sensor._last_energy_ts is None


@pytest.mark.asyncio
async def test_lifetime_restore_none(hass, coordinator_factory):
    sensor = EnphaseLifetimeEnergySensor(coordinator_factory(), RANDOM_SERIAL)
    sensor.hass = hass
    sensor.async_get_last_sensor_data = AsyncMock(return_value=None)
    await sensor.async_added_to_hass()


@pytest.mark.asyncio
async def test_lifetime_restore_error_and_attrs(hass, coordinator_factory):
    class BadLast:
        @property
        def native_value(self):
            raise ValueError("bad")

    bad_last = BadLast()
    bad_state = State(
        "sensor.enphase_lifetime",
        "invalid",
        attributes={"last_reset_value": "bad", "last_reset_at": "2024-01-01T00:00:00"},
    )

    sensor = EnphaseLifetimeEnergySensor(coordinator_factory(), RANDOM_SERIAL)
    sensor.hass = hass
    sensor.async_get_last_sensor_data = AsyncMock(return_value=bad_last)
    sensor.async_get_last_state = AsyncMock(side_effect=ValueError("oops"))

    await sensor.async_added_to_hass()

    sensor.async_get_last_state = AsyncMock(return_value=bad_state)

    await sensor.async_added_to_hass()
    assert sensor._last_reset_at == "2024-01-01T00:00:00"


@pytest.mark.asyncio
async def test_lifetime_restore_sets_last_value(hass, coordinator_factory):
    last = SimpleNamespace(native_value=1.5)
    sensor = EnphaseLifetimeEnergySensor(coordinator_factory(), RANDOM_SERIAL)
    sensor.hass = hass
    sensor.async_get_last_sensor_data = AsyncMock(return_value=last)
    sensor.async_get_last_state = AsyncMock(return_value=None)
    await sensor.async_added_to_hass()
    assert sensor._last_value == 1.5


def test_lifetime_native_value_guard_paths(coordinator_factory):
    sensor = EnphaseLifetimeEnergySensor(coordinator_factory(), RANDOM_SERIAL)
    sensor._last_value = 5.0
    sensor._boot_filter = True
    sensor._attr_native_value = None

    sensor._data["lifetime_kwh"] = object()
    assert sensor.native_value == 5.0

    sensor._data["lifetime_kwh"] = -1
    assert sensor.native_value == 5.0

    sensor._data["lifetime_kwh"] = 0
    assert sensor.native_value == 5.0
