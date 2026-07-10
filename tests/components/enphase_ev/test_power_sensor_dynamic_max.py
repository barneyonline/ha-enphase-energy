import datetime as _dt

import pytest


def _build_sensor(sn: str = "555555555555"):
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.sensor import EnphasePowerSensor

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.data = {sn: {"sn": sn, "name": "Garage EV", "charging": True}}
    coord.serials = {sn}
    coord.site_id = "1234567"
    coord.last_update_success = True
    coord.last_set_amps = {}

    sensor = EnphasePowerSensor(coord, sn)
    # Seed the lifetime baseline so native_value performs delta math
    base_ts = _dt.datetime(2025, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
    sensor._last_lifetime_kwh = 1.0
    sensor._last_energy_ts = base_ts.timestamp()
    sensor._last_sample_ts = base_ts.timestamp()
    sensor._last_power_w = 0
    sensor._last_window_s = None
    sensor._last_method = "seeded"
    return coord, sensor, base_ts


@pytest.mark.asyncio
async def test_power_sensor_clamps_to_session_amps():
    coord, sensor, base_ts = _build_sensor()
    sn = next(iter(coord.data.keys()))
    coord.data[sn].update(
        {
            "lifetime_kwh": 1.15,
            "last_reported_at": (base_ts + _dt.timedelta(seconds=60)).isoformat(),
            "operating_v": 240,
            "session_charge_level": 32,
        }
    )

    assert sensor.native_value == 7680

    assert sensor._max_throughput_w == 7680
    assert sensor._max_throughput_source == "session_charge_level"
    assert sensor._max_throughput_amps == 32
    assert sensor._max_throughput_voltage == 240


@pytest.mark.asyncio
async def test_power_sensor_falls_back_to_max_amp():
    coord, sensor, base_ts = _build_sensor()
    sn = next(iter(coord.data.keys()))
    coord.data[sn].update(
        {
            "lifetime_kwh": 1.15,
            "last_reported_at": (base_ts + _dt.timedelta(seconds=60)).isoformat(),
            "operating_v": 208,
            "max_amp": "40",
        }
    )

    assert sensor.native_value == 8320

    assert sensor._max_throughput_w == 8320
    assert sensor._max_throughput_source == "max_amp"
    assert sensor._max_throughput_amps == 40
    assert sensor._max_throughput_voltage == 208


@pytest.mark.asyncio
async def test_power_sensor_three_phase_cap_uses_phase_count():
    coord, sensor, base_ts = _build_sensor()
    sn = next(iter(coord.data.keys()))
    coord.data[sn].update(
        {
            "lifetime_kwh": 1.25,
            "last_reported_at": (base_ts + _dt.timedelta(seconds=60)).isoformat(),
            "operating_v": 230,
            "charging_level": 16,
            "phase_count": 3,
        }
    )

    assert sensor.native_value == 6374

    assert sensor._max_throughput_w == 6374
    assert sensor._max_throughput_unbounded_w == 6374
    assert sensor._max_throughput_source == "charging_level"
    assert sensor._max_throughput_amps == 16
    assert sensor._max_throughput_voltage == 230
    assert sensor._max_throughput_topology == "three_phase"
    assert sensor._max_throughput_phase_multiplier == pytest.approx(1.7320508075688772)


@pytest.mark.asyncio
async def test_power_sensor_three_phase_neutral_wiring_uses_phase_voltage():
    coord, sensor, base_ts = _build_sensor()
    sn = next(iter(coord.data.keys()))
    coord.data[sn].update(
        {
            "lifetime_kwh": 1.25,
            "last_reported_at": (base_ts + _dt.timedelta(seconds=60)).isoformat(),
            "operating_v": 230,
            "charging_level": 16,
            "phase_count": 3,
            "wiring_configuration": {"L1": "L1", "L2": "L2", "L3": "L3", "N": "N"},
        }
    )

    assert sensor.native_value == 11040

    assert sensor._max_throughput_w == 11040
    assert sensor._max_throughput_unbounded_w == 11040
    assert sensor._max_throughput_topology == "three_phase"
    assert sensor._max_throughput_phase_multiplier == 3.0


@pytest.mark.asyncio
async def test_power_sensor_split_phase_does_not_apply_phase_multiplier():
    coord, sensor, base_ts = _build_sensor()
    sn = next(iter(coord.data.keys()))
    coord.data[sn].update(
        {
            "lifetime_kwh": 1.25,
            "last_reported_at": (base_ts + _dt.timedelta(seconds=60)).isoformat(),
            "operating_v": 240,
            "charging_level": 32,
            "phase_mode": "split",
            "phase_count": 3,
        }
    )

    assert sensor.native_value == 7680

    assert sensor._max_throughput_w == 7680
    assert sensor._max_throughput_source == "charging_level"
    assert sensor._max_throughput_topology == "split_phase"
    assert sensor._max_throughput_phase_multiplier == 1.0
