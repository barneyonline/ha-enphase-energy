"""Replay constructed daily lifetime reports at source precision."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev import energy as energy_mod
from custom_components.enphase_ev.directional_battery_power import (
    DIRECTIONAL_BATTERY_POWER_FIELDS,
    DirectionalBatteryPower,
)
from custom_components.enphase_ev.energy import EnergyManager
from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseDirectionalBatteryPowerSensor,
)

_START = datetime(2026, 9, 5, 12, tzinfo=UTC)
_FIELDS = tuple(DIRECTIONAL_BATTERY_POWER_FIELDS.values())


@pytest.fixture
def replay():
    manager = EnergyManager.__new__(EnergyManager)
    power = DirectionalBatteryPower(300)

    def report(
        seconds,
        grid=1000,
        solar=2000,
        home=3000,
        export=4000,
        *,
        charge=None,
        discharge=None,
        delay=0,
        rollover=False,
        overrides=None,
    ):
        """Supply the raw partial bucket and exact lifetime direction sums."""
        values = (grid, solar, home, export)
        payload = {field: [value] for field, value in zip(_FIELDS, values, strict=True)}
        payload.update(
            charge=[grid + solar if charge is None else charge],
            discharge=[home + export if discharge is None else discharge],
        )
        closed_wh = 5000 if rollover else 0
        if rollover:
            payload = {
                field: [closed_wh if field in _FIELDS else 2 * closed_wh, *buckets]
                for field, buckets in payload.items()
            }
        if overrides:
            payload.update(overrides)
        timestamp = _START + timedelta(seconds=seconds)
        power.observe(
            payload,
            timestamp,
            timestamp + timedelta(seconds=delay),
            {
                field: (closed_wh + value, 2 if rollover else 1)
                for field, value in zip(_FIELDS, values, strict=True)
            },
            manager._coerce_lifetime_bucket,
        )
        return power

    return report


def test_source_window_baseline_fractional_wh_and_confirmed_zero(replay):
    power = replay(0, overrides={"charge": [3000, None]})
    assert power.interval is power.average is None
    assert replay(180, grid=1000.2).interval is None
    power = replay(300, grid=1050, solar=2000.4, home=3010, export=4000.1)
    assert power.interval.watts == dict(zip(_FIELDS, (600, 5, 120, 1), strict=True))
    assert power.interval.window_seconds == 300
    assert power.average == power.interval
    assert replay(300, grid=1050, solar=2000.4, home=3010, export=4000.1).interval == (
        power.interval
    )
    power = replay(600, grid=1050, solar=2000.4, home=3010, export=4000.1)
    assert power.interval.watts == dict.fromkeys(_FIELDS, 0)
    assert power.average.watts["grid_battery"] == 300


def test_entity_starts_without_value_or_source_attributes(coordinator_factory):
    entity = EnphaseDirectionalBatteryPowerSensor(
        coordinator_factory(), "battery_power_from_grid", DirectionalBatteryPower(300)
    )
    assert not entity.available
    assert entity.native_value is None
    assert entity.extra_state_attributes == {}


def test_replay_recorded_partial_day_window(replay):
    # The sanitized reports' current-day buckets, 303 source seconds apart.
    replay(0, grid=199, solar=320, home=3375, export=0, discharge=3376)
    power = replay(303, grid=202, solar=320, home=3375, export=0, discharge=3376)
    assert power.interval.window_seconds == 303
    assert power.interval.watts == dict(zip(_FIELDS, (36, 0, 0, 0), strict=True))
    assert power.average == power.interval


async def test_power_reseeds_after_revision_without_rebasing_lifetime_energy(
    coordinator_factory, monkeypatch
):
    monkeypatch.setattr(energy_mod, "DIRECTIONAL_BATTERY_POWER_SOURCE_VALIDATED", True)
    now = _START
    monkeypatch.setattr(energy_mod.dt_util, "utcnow", lambda: now)
    read = AsyncMock()
    manager = coordinator_factory(client=SimpleNamespace(lifetime_energy=read)).energy
    power = manager.directional_battery_power

    async def refresh(seconds, grid_in, home, grid_out, discharge):
        nonlocal now
        now = _START + timedelta(seconds=seconds)
        read.return_value = {
            "start_date": _START.date().isoformat(),
            "last_report_date": now.timestamp(),
            "grid_battery": [grid_in],
            "solar_battery": [100],
            "battery_home": [home],
            "battery_grid": [grid_out],
            "charge": [grid_in + 100],
            "discharge": [discharge],
        }
        await manager._async_refresh_site_energy(force=True)

    await refresh(0, 1000, 100, 0, 101)
    await refresh(300, 1005, 110, 2, 112)
    assert power.interval.watts["battery_grid"] == 24
    assert manager.site_energy["battery_energy_to_grid"].value_kwh == 0.002

    await refresh(607, 1008, 142, 0, 143)
    assert power.interval is power.average is None
    assert "battery_energy_to_grid" not in manager.site_energy
    await refresh(895, 1009, 178, 0, 179)
    assert power.interval is power.average is None

    await refresh(1255, 1010, 217, 1, 218)
    assert "battery_energy_to_grid" not in manager.site_energy
    assert power.interval is not None
    assert power.interval.window_seconds == 648
    assert power.interval.watts == dict(zip(_FIELDS, (11, 0, 417, 6), strict=True))
    assert power.average == power.interval


async def test_failed_lifetime_save_breaks_the_power_window(
    coordinator_factory, monkeypatch
):
    monkeypatch.setattr(energy_mod, "DIRECTIONAL_BATTERY_POWER_SOURCE_VALIDATED", True)
    now = _START
    monkeypatch.setattr(energy_mod.dt_util, "utcnow", lambda: now)
    read = AsyncMock()
    manager = coordinator_factory(client=SimpleNamespace(lifetime_energy=read)).energy
    monkeypatch.setattr(
        manager._directional_battery,
        "async_save",
        AsyncMock(side_effect=[True, True, False, True, True]),
    )
    power = manager.directional_battery_power

    async def refresh(grid):
        nonlocal now
        now += timedelta(minutes=5)
        read.return_value = {
            "start_date": now.date().isoformat(),
            "last_report_date": now.timestamp(),
            "grid_battery": [grid],
            "solar_battery": [0],
            "battery_home": [0],
            "battery_grid": [0],
            "charge": [grid],
            "discharge": [0],
        }
        await manager._async_refresh_site_energy(force=True)

    await refresh(1000)
    await refresh(1050)
    assert power.interval.watts["grid_battery"] == 600

    await refresh(1100)
    assert power.interval is power.average is None

    await refresh(1150)
    assert power.interval is power.average is None
    await refresh(1200)
    assert power.interval.watts["grid_battery"] == 600
    assert power.average == power.interval


def test_smoothing_uses_elapsed_time_and_interpolates_left_edge(replay):
    replay(0)
    replay(300, grid=1050)
    power = replay(1200, grid=1080)
    assert power.interval.watts["grid_battery"] == 120
    assert power.average.window_seconds == 900
    assert power.average.watts["grid_battery"] == 120
    power = replay(1500, grid=1110)
    # Last 900 s contains 20 Wh of the prior 900 s and all 30 Wh of the last 300 s.
    assert power.interval.watts["grid_battery"] == 360
    assert power.average.watts["grid_battery"] == 200


@pytest.mark.parametrize(
    "bad_report",
    [
        {"grid": 1100, "charge": 3050},  # Clock advances before the direct tally.
        {"home": 3050, "discharge": 7000},  # Direct counter is unchanged.
        {"discharge": 7001},  # No confirmed zero while the direct counter moves.
        {"overrides": {"charge": None}},
        {"overrides": {"charge": []}},
        {"overrides": {"charge": [True]}},
        {"overrides": {"charge": [None]}},
    ],
)
def test_bad_window_invalidates_all_directions_and_recovery_reseeds(replay, bad_report):
    replay(0)
    assert replay(300).interval is not None
    power = replay(600, **bad_report)
    assert power.interval is power.average is None
    assert replay(900, grid=1150).interval is None
    assert replay(1200, grid=1200).interval.watts["grid_battery"] == 600


def test_advancing_direct_counter_revision_seeds_new_window(replay):
    replay(0)
    assert replay(300).interval is not None
    power = replay(600, charge=2999)
    assert power.interval is power.average is None
    assert replay(600, charge=2999).interval is None
    power = replay(900, grid=1050)
    assert power.interval.watts["grid_battery"] == 600
    assert power.average == power.interval


@pytest.mark.parametrize("bad_time", [150, 300])
def test_regressed_report_or_same_timestamp_correction(replay, bad_time):
    replay(0)
    replay(300, grid=1050)
    assert replay(bad_time, grid=1100).interval is None
    assert replay(300, grid=1050).interval is None
    assert replay(600, grid=1100).interval is None
    assert replay(900, grid=1150).interval.watts["grid_battery"] == 600


def test_partial_day_rollover_and_delayed_reports_reseed(replay):
    replay(0)
    assert replay(300, grid=1050).interval is not None
    assert replay(600, grid=2, rollover=True).interval is None
    assert replay(900, grid=12, rollover=True).interval.watts["grid_battery"] == 120
    assert replay(1200, grid=50, rollover=True, delay=1500).interval is None
    assert replay(3000, grid=100, rollover=True).interval is None
    assert replay(3300, grid=150, rollover=True).interval.watts["grid_battery"] == 600


def test_batch_at_same_cursor_cannot_publish_and_coherent_batch_uses_elapsed_time(
    replay,
):
    replay(0)
    assert replay(0, grid=1100).interval is None
    assert replay(300, grid=1100).interval is None
    power = replay(1200, grid=1250, home=3090)
    assert power.interval.watts == dict(zip(_FIELDS, (600, 0, 360, 0), strict=True))
    assert power.interval.window_seconds == 900


def test_outliers_and_extreme_confirmation_are_group_wide(replay):
    replay(0)
    # A 10 kWh jump over five minutes is 120 kW and dominates the history.
    power = replay(300, grid=11000)
    assert power.interval is power.average is None
    assert replay(600, grid=11050).interval.watts["grid_battery"] == 600
    power = replay(900, grid=111050)
    assert power.interval is power.average is None
    assert replay(900, grid=111050).interval is None
    power = replay(1200, grid=211050)
    assert power.interval.watts["grid_battery"] == 1200000
    assert power.average == power.interval
    assert replay(1500, grid=211100).average.watts["grid_battery"] == 600


def test_finite_counter_can_still_overflow_conversion(replay):
    replay(0)
    power = replay(300, grid=1e308)
    assert power.interval is power.average is None
