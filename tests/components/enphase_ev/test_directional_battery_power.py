"""Replay constructed daily lifetime reports at source precision."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev import energy as energy_mod
from custom_components.enphase_ev.directional_battery_energy import (
    DirectionalBatteryPairReference,
    DirectionalBatteryRecovery,
)
from custom_components.enphase_ev.directional_battery_power import (
    DIRECTIONAL_BATTERY_POWER_FIELDS,
    DirectionalBatteryPower,
)
from custom_components.enphase_ev.energy import EnergyManager
from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseDirectionalBatteryPowerSensor,
)

_START = datetime(2026, 9, 5, 12, tzinfo=UTC)
_SOURCE = (_START.date(), "UTC")
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
        startup=None,
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
            source=_SOURCE,
            startup=startup,
        )
        power.publish()
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


def test_subwatt_movement_rounds_to_zero_in_both_modes(replay):
    replay(0)
    power = replay(300, grid=1000.02)
    assert power.interval.watts["grid_battery"] == 0
    assert power.average.watts["grid_battery"] == 0


@pytest.mark.parametrize(
    ("delta_wh", "seconds", "expected"), [(1, 480, 8), (0.125, 300, 2)]
)
def test_single_window_smoothing_rounds_from_raw_watt_hours(
    replay, delta_wh, seconds, expected
):
    replay(0)
    power = replay(seconds, grid=1000 + delta_wh)
    assert power.interval.watts["grid_battery"] == expected
    assert power.average == power.interval


def test_published_power_readings_are_read_only(replay):
    replay(0)
    power = replay(300, grid=1050)
    with pytest.raises(FrozenInstanceError):
        power.snapshot.qualified = False
    for reading in (power.interval, power.average):
        with pytest.raises(TypeError):
            reading.watts["grid_battery"] = 0
    recovery = power.recovery_checkpoint(_START + timedelta(minutes=5))
    with pytest.raises(TypeError):
        recovery.pairs["charge"].wh["grid_battery"] = 0


def test_entity_starts_without_value_or_source_attributes(coordinator_factory):
    entity = EnphaseDirectionalBatteryPowerSensor(
        coordinator_factory(), "battery_power_from_grid"
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


@pytest.mark.parametrize("superseded", [False, True])
async def test_interrupted_lifetime_save_breaks_the_power_window(
    coordinator_factory, monkeypatch, superseded
):
    monkeypatch.setattr(energy_mod, "DIRECTIONAL_BATTERY_POWER_SOURCE_VALIDATED", True)
    now = _START
    monkeypatch.setattr(energy_mod.dt_util, "utcnow", lambda: now)
    read = AsyncMock()
    manager = coordinator_factory(client=SimpleNamespace(lifetime_energy=read)).energy

    async def save_history(power_wh):
        if now == _START + timedelta(minutes=15):
            if superseded:
                manager._invalidate_site_energy_cache()
                return True
            return False
        return True

    monkeypatch.setattr(manager._directional_battery, "async_save", save_history)
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


@pytest.mark.parametrize(
    ("save_result", "power_enabled"),
    [("success", True), ("failure", True), ("cancel", True), ("cancel", False)],
)
async def test_power_publication_waits_for_storage_ack(
    coordinator_factory, monkeypatch, save_result, power_enabled
):
    monkeypatch.setattr(
        energy_mod, "DIRECTIONAL_BATTERY_POWER_SOURCE_VALIDATED", power_enabled
    )
    now = _START
    monkeypatch.setattr(energy_mod.dt_util, "utcnow", lambda: now)
    read = AsyncMock()
    manager = coordinator_factory(client=SimpleNamespace(lifetime_energy=read)).energy
    power = manager.directional_battery_power
    saving = asyncio.Event()
    finish_save = asyncio.Event()

    async def save_history(_power_wh):
        if now == _START:
            return True
        saving.set()
        await finish_save.wait()
        return save_result != "failure"

    monkeypatch.setattr(manager._directional_battery, "async_save", save_history)

    def payload(grid):
        return {
            "start_date": now.date().isoformat(),
            "last_report_date": now.timestamp(),
            "grid_battery": [grid],
            "solar_battery": [0],
            "battery_home": [0],
            "battery_grid": [0],
            "charge": [grid],
            "discharge": [0],
        }

    read.return_value = payload(1000)
    await manager._async_refresh_site_energy(force=True)
    now += timedelta(minutes=5)
    read.return_value = payload(1050)
    refresh = asyncio.create_task(manager._async_refresh_site_energy(force=True))
    await asyncio.wait_for(saving.wait(), timeout=5)
    if power_enabled:
        assert power.interval is power.average is None
        assert not power.qualified
    else:
        assert power is None
    if save_result == "cancel":
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
    else:
        finish_save.set()
        await refresh
    if not power_enabled:
        return
    if save_result == "success":
        assert power.interval.watts["grid_battery"] == 600
        assert power.qualified
    else:
        assert power.interval is power.average is None
        assert not power.qualified


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


@pytest.mark.parametrize("solar", [0, 2000])
@pytest.mark.parametrize("lowered_grid", [0, 50])
def test_pair_counter_drop_stays_unavailable_until_fresh_window(
    replay, solar, lowered_grid
):
    replay(0, solar=solar)
    assert replay(300, grid=1050, solar=solar).interval.watts["grid_battery"] == 600

    for seconds, grid in ((600, lowered_grid), (900, lowered_grid), (1200, 750)):
        power = replay(seconds, grid=grid, solar=solar)
        assert power.interval is power.average is None

    power = replay(1500, grid=1050, solar=solar)
    assert power.interval is power.average is None
    power = replay(1800, grid=1100, solar=solar)
    assert power.interval.watts["grid_battery"] == 600
    assert power.average == power.interval


def test_pair_counter_drop_clears_at_site_day_rollover(replay):
    replay(0)
    assert replay(300, grid=50).interval is None
    assert replay(600, grid=10, rollover=True).interval is None
    assert replay(900, grid=60, rollover=True).interval.watts["grid_battery"] == 600


def test_pair_recovery_waits_for_both_directions(replay):
    replay(0, grid=1050)
    assert replay(300, grid=1050).interval is not None
    for seconds, grid, solar in (
        (600, 0, 2000),
        (900, 800, 1800),
        (1200, 1100, 1950),
        (1500, 1100, 2000),
    ):
        power = replay(seconds, grid=grid, solar=solar)
        assert power.interval is power.average is None
    power = replay(1800, grid=1100, solar=2050)
    assert power.interval.watts["solar_battery"] == 600
    assert power.average == power.interval


def test_two_counter_pair_dropouts_recover_independently(replay):
    replay(0)
    assert replay(300).interval is not None
    for seconds, grid, home in (
        (600, 0, 0),
        (900, 1000, 0),
        (1200, 1500, 500),
        (1500, 1000, 3000),
        (1800, 1500, 3000),
    ):
        power = replay(seconds, grid=grid, home=home)
        assert power.interval is power.average is None
    power = replay(2100, grid=1550, home=3000)
    assert power.interval.watts["grid_battery"] == 600
    assert power.average == power.interval


def test_staggered_directional_and_direct_reductions_are_held(replay):
    replay(0, grid=2000, solar=3000)
    assert replay(300, grid=2000, solar=3000).interval is not None
    for seconds, grid, charge in (
        (600, 1800, 5000),
        (900, 1800, 4800),
        (1200, 2000, 5000),
    ):
        power = replay(seconds, grid=grid, solar=3000, charge=charge)
        assert power.interval is power.average is None
    assert replay(1500, grid=2050, solar=3000).interval.watts["grid_battery"] == 600


@pytest.mark.parametrize("linger", [False, True])
@pytest.mark.parametrize("outlier", [11050, 111050])
def test_rejected_outlier_cannot_pin_the_dropout_guard(replay, linger, outlier):
    replay(0)
    assert replay(300, grid=1050).interval.watts["grid_battery"] == 600
    assert replay(600, grid=outlier).interval is None
    seconds = 900
    if linger:
        assert replay(seconds, grid=outlier + 50).interval.watts["grid_battery"] == 600
        seconds += 300
    assert replay(seconds, grid=1100).interval is None
    assert replay(seconds + 300, grid=1150).interval.watts["grid_battery"] == 600


def test_implausible_recovery_preserves_restart_watermark(replay):
    startup_wh = {
        "charge": 3050,
        "grid_battery": 1050,
        "solar_battery": 2000,
    }
    saved_at = _START - timedelta(minutes=5)
    startup = DirectionalBatteryRecovery(
        _SOURCE,
        saved_at,
        1,
        {"charge": DirectionalBatteryPairReference(saved_at, startup_wh)},
    )
    assert replay(0, grid=0, startup=startup).interval is None
    power = replay(300, grid=111050)
    assert power.interval is None
    checkpoint = power.recovery_checkpoint(_START + timedelta(seconds=300))
    assert checkpoint.pairs["charge"].wh["grid_battery"] == 1050
    assert replay(600, grid=850).interval is None
    assert replay(900, grid=900).interval is None
    assert replay(1200, grid=1050).interval is None
    assert replay(1500, grid=1100).interval.watts["grid_battery"] == 600


def test_implausible_startup_report_does_not_poison_recovery_reference(replay):
    saved_at = _START - timedelta(minutes=5)
    startup = DirectionalBatteryRecovery(
        _SOURCE,
        saved_at,
        1,
        {
            "charge": DirectionalBatteryPairReference(
                saved_at, {"charge": 3050, "grid_battery": 1050, "solar_battery": 2000}
            )
        },
    )
    power = replay(0, grid=111050, startup=startup)
    assert power.interval is None
    assert power.recovery_checkpoint(_START).pairs["charge"].wh["grid_battery"] == 1050
    assert replay(300, grid=1100).interval is None
    assert replay(600, grid=1150).interval.watts["grid_battery"] == 600


@pytest.mark.parametrize("grid", [1050, 1100])
def test_legacy_report_at_saved_clock_waits_for_advancing_source(replay, grid):
    startup = DirectionalBatteryRecovery(
        _SOURCE,
        _START,
        1,
        {
            "charge": DirectionalBatteryPairReference(
                _START, {"grid_battery": 1050, "solar_battery": 2000}
            )
        },
    )
    assert replay(0, grid=grid, startup=startup).interval is None
    assert replay(300, grid=1150).interval is None
    assert replay(600, grid=1200).interval.watts["grid_battery"] == 600


def test_rejected_outlier_in_other_pair_cannot_pin_the_dropout_guard(replay):
    replay(0)
    assert replay(300).interval is not None
    assert replay(600, grid=0).interval is None
    assert replay(900, grid=750, home=200000).interval is None
    assert replay(1200).interval is None
    assert replay(1500, grid=1050).interval.watts["grid_battery"] == 600


def test_lagging_direct_counter_cannot_age_a_rejected_directional_jump(replay):
    replay(0, grid=1050, solar=250000)
    assert replay(300, grid=11050, solar=250000, charge=251050).interval is None
    assert replay(600, grid=11050, solar=250000).interval is None
    assert replay(900, grid=1100, solar=250000).interval is None
    assert replay(1200, grid=1150, solar=250000).interval.watts["grid_battery"] == 600


@pytest.mark.parametrize("grid", [1049, 1050])
def test_leading_direct_counter_cannot_age_a_rejected_directional_jump(replay, grid):
    replay(0, grid=1050, solar=250000)
    assert replay(300, grid=grid, solar=250000, charge=261050).interval is None
    assert replay(600, grid=11050, solar=250000).interval is None
    assert replay(900, grid=1100, solar=250000).interval is None
    assert replay(1200, grid=1150, solar=250000).interval.watts["grid_battery"] == 600


@pytest.mark.parametrize("earlier_spike", [False, True])
def test_routing_revision_cannot_approve_a_spike_in_its_pair(replay, earlier_spike):
    replay(0, grid=1050)
    revision_at = 300
    if earlier_spike:
        assert replay(revision_at, grid=11050).interval is None
        revision_at += 300
    assert replay(revision_at, grid=11050, solar=1999).interval is None
    power = replay(revision_at + 300, grid=11100, solar=1999)
    assert power.interval.watts["grid_battery"] == 600
    assert replay(revision_at + 600, grid=1100, solar=1999).interval is None
    power = replay(revision_at + 900, grid=1150, solar=1999)
    assert power.interval.watts["grid_battery"] == 600


def test_recovery_guard_tracks_trusted_subminimum_reports(replay):
    replay(0)
    assert replay(300, grid=1050).interval.watts["grid_battery"] == 600
    assert replay(450, grid=1150).interval.watts["grid_battery"] == 600
    assert replay(600, grid=1100).interval is None
    assert replay(900, grid=1150).interval is None
    assert replay(1200, grid=1200).interval.watts["grid_battery"] == 600


def test_recovery_uses_at_least_the_minimum_power_window(replay):
    replay(0, grid=1050)
    assert replay(1, grid=1350).interval is None
    assert replay(300, grid=1350).interval.watts["grid_battery"] == 3600
    assert replay(600, grid=1075).interval is None
    assert replay(900, grid=1350).interval is None
    assert replay(1200, grid=1400).interval.watts["grid_battery"] == 600


def test_staggered_discharge_drop_during_charge_recovery(replay):
    replay(0, grid=1050)
    assert replay(300, grid=1050).interval is not None
    for seconds, grid, home, discharge in (
        (600, 0, 2900, 7000),
        (900, 0, 2900, 6900),
        (1200, 1050, 2900, 6900),
        (1500, 1050, 3000, 7000),
    ):
        assert (
            replay(seconds, grid=grid, home=home, discharge=discharge).interval is None
        )
    assert replay(1800, grid=1050, home=3050).interval.watts["battery_home"] == 600


def test_dropout_recovery_compares_paired_and_direct_deltas(replay):
    replay(0, grid=1050)
    assert replay(300, grid=0).interval is None
    assert replay(600, grid=1200, charge=3300).interval is None
    assert replay(900, grid=1050).interval is None
    assert replay(1200, grid=1100).interval.watts["grid_battery"] == 600


@pytest.mark.parametrize(
    "history_kind",
    [
        "legacy",
        "corrupt_checkpoint",
        "invalid_pair",
        "future_pair_clock",
        "numeric_clock",
        "bad_clock",
        "future_clock",
        "naive_clock",
        "power",
        "closed_revision",
        "revision_during_reload",
        "routing_revision",
        "known_hold",
        "hold_clock",
        "outlier_clock",
        "invalid_report",
        "invalid_before_reload",
        "invalid_after_reload",
        "invalid_after_legacy_reload",
        "invalid_origin_after_reload",
        "missing_start_across_reloads",
        "blank_start_across_reloads",
        "unpadded_start_across_reloads",
        "legacy_at_saved_clock",
        "origin_shift",
        "regressed_origin",
        "conflicting_start_cursor",
        "invalid_boolean_origin",
    ],
)
async def test_reload_during_dropout_uses_saved_same_day_counters(
    coordinator_factory, monkeypatch, history_kind
):
    monkeypatch.setattr(energy_mod, "DIRECTIONAL_BATTERY_POWER_SOURCE_VALIDATED", True)
    now = _START
    monkeypatch.setattr(energy_mod.dt_util, "utcnow", lambda: now)
    read = AsyncMock()
    saved = None

    async def load_history():
        return saved

    async def save_history(history):
        nonlocal saved
        saved = history

    def new_manager():
        manager = coordinator_factory(
            client=SimpleNamespace(lifetime_energy=read)
        ).energy
        manager._directional_battery._store = SimpleNamespace(
            async_load=load_history, async_save=save_history
        )
        manager._directional_battery._loaded = False
        return manager

    manager = new_manager()
    closed_grid = 500

    async def refresh(
        seconds,
        grid,
        *,
        solar=2000,
        charge=None,
        receipt_seconds=None,
        source_start="known",
        grid_export=0,
    ):
        nonlocal now
        reported_at = _START + timedelta(seconds=seconds)
        now = (
            reported_at
            if receipt_seconds is None
            else _START + timedelta(seconds=receipt_seconds)
        )
        start_day = _START.date() - timedelta(days=1)
        payload = {
            "start_date": {
                "known": start_day.isoformat(),
                "other": (_START.date() - timedelta(days=2)).isoformat(),
                "shifted": _START.date().isoformat(),
                "missing": None,
                "blank": "",
                "unpadded": f"{start_day.year}-{start_day.month}-{start_day.day}",
            }[source_start],
            "last_report_date": reported_at.timestamp(),
            "grid_battery": [closed_grid, grid],
            "solar_battery": [0, solar],
            "battery_home": [0, 0],
            "battery_grid": [0, grid_export],
            "charge": [closed_grid, grid + solar if charge is None else charge],
            "discharge": [0, 0],
        }
        if source_start == "shifted" and reported_at.date() == _START.date():
            payload = {
                key: value[-1:] if isinstance(value, list) else value
                for key, value in payload.items()
            }
        read.return_value = payload
        await manager._async_refresh_site_energy(force=True)
        return manager.directional_battery_power.interval

    assert await refresh(0, 1000) is None
    assert (await refresh(300, 1050)).watts["grid_battery"] == 600
    if history_kind in ("closed_revision", "regressed_origin"):
        closed_grid = 450
        assert await refresh(600, 1100) is None
        assert await refresh(900, 1250) is None
        assert (await refresh(1200, 1300)).watts["grid_battery"] == 600
        assert "battery_energy_from_grid" not in manager.site_energy
        assert saved["totals"]["grid_battery"] == 1550
    elif history_kind in (
        "legacy",
        "invalid_after_legacy_reload",
        "legacy_at_saved_clock",
    ):
        del saved["power"]
    elif history_kind == "corrupt_checkpoint":
        del saved["power"]["pairs"]["charge"]["wh"]["grid_battery"]
    elif history_kind == "invalid_pair":
        saved["power"]["pairs"]["charge"] = []
    elif history_kind == "future_pair_clock":
        saved["power"]["pairs"]["charge"]["reported_at"] = (
            _START + timedelta(minutes=10)
        ).isoformat()
    elif history_kind == "numeric_clock":
        saved["power"]["reported_at"] = 1e300
    elif history_kind == "bad_clock":
        saved["power"]["reported_at"] = "1e300"
    elif history_kind == "future_clock":
        saved["power"]["reported_at"] = "9999-12-31T08:00:00+00:00"
    elif history_kind == "naive_clock":
        saved["power"]["reported_at"] = "2026-09-05T12:05:00"
    elif history_kind in ("known_hold", "hold_clock", "origin_shift"):
        assert await refresh(600, 0) is None
        assert await refresh(900, 0) is None
        assert await refresh(1200, 0) is None
    elif history_kind == "outlier_clock":
        assert await refresh(600, 11050) is None
        assert (await refresh(900, 11100)).watts["grid_battery"] == 600

    if history_kind in (
        "invalid_report",
        "invalid_before_reload",
        "invalid_after_reload",
        "invalid_after_legacy_reload",
        "invalid_origin_after_reload",
        "missing_start_across_reloads",
        "blank_start_across_reloads",
        "unpadded_start_across_reloads",
    ):
        if history_kind not in ("invalid_report", "invalid_before_reload"):
            manager = new_manager()
        assert (
            await refresh(
                600,
                1050,
                charge=13050,
                source_start={
                    "invalid_origin_after_reload": "other",
                    "missing_start_across_reloads": "missing",
                    "blank_start_across_reloads": "blank",
                    "unpadded_start_across_reloads": "unpadded",
                }.get(history_kind, "known"),
            )
            is None
        )
        if history_kind in (
            "missing_start_across_reloads",
            "blank_start_across_reloads",
            "unpadded_start_across_reloads",
        ):
            manager = new_manager()
        else:
            assert await refresh(600, 1050) is None
        if history_kind == "invalid_after_legacy_reload":
            assert saved["power"]["closed_signature"] is None
        if history_kind in ("invalid_before_reload", "invalid_after_legacy_reload"):
            manager = new_manager()
        assert await refresh(900, 11050) is None
        assert await refresh(1200, 1100) is None
        assert (await refresh(1500, 1150)).watts["grid_battery"] == 600
        return

    manager = new_manager()
    if history_kind == "invalid_boolean_origin":
        assert (
            await refresh(1200, 1050, source_start="shifted", grid_export=False) is None
        )
        manager = new_manager()
        assert (
            await refresh(900, 1050, receipt_seconds=1201, source_start="shifted")
            is None
        )
        assert (
            await refresh(1200, 1100, receipt_seconds=1202, source_start="shifted")
            is None
        )
        assert await refresh(1500, 1150, source_start="shifted") is None
        assert (await refresh(1800, 1200, source_start="shifted")).watts[
            "grid_battery"
        ] == 600
        return
    if history_kind == "conflicting_start_cursor":
        assert await refresh(1200, 1050, source_start="other") is None
        manager = new_manager()
        assert await refresh(900, 1050, receipt_seconds=1201) is None
        assert await refresh(1200, 1100, receipt_seconds=1202) is None
        assert await refresh(1500, 1150) is None
        assert (await refresh(1800, 1200)).watts["grid_battery"] == 600
        return
    if history_kind == "origin_shift":
        assert await refresh(86400, 10, source_start="missing") is None
        assert await refresh(86700, 10, source_start="shifted") is None
        assert saved["power"]["start"] == _START.date().isoformat()
        assert (await refresh(87000, 60, source_start="shifted")).watts[
            "grid_battery"
        ] == 600
        return
    if history_kind == "regressed_origin":
        assert (
            await refresh(900, 10, receipt_seconds=1201, source_start="shifted") is None
        )
        assert (
            await refresh(1100, 60, receipt_seconds=1202, source_start="shifted")
            is None
        )
        assert await refresh(1500, 100, source_start="shifted") is None
        assert (await refresh(1800, 150, source_start="shifted")).watts[
            "grid_battery"
        ] == 600
        return
    if history_kind == "legacy_at_saved_clock":
        assert await refresh(300, 1075) is None
        assert await refresh(600, 1100) is None
        assert (await refresh(900, 1150)).watts["grid_battery"] == 600
        return
    if history_kind == "outlier_clock":
        assert await refresh(1200, 11150) is None
        assert await refresh(1500, 1100) is None
        assert (await refresh(1800, 1150)).watts["grid_battery"] == 600
        return
    if history_kind == "hold_clock":
        assert await refresh(1500, 0) is None
        assert await refresh(1800, 11050) is None
        assert (await refresh(2100, 11100)).watts["grid_battery"] == 600
        return
    if history_kind == "known_hold":
        assert await refresh(1500, 950, solar=2100) is None
        assert await refresh(1800, 950, solar=2150) is None
        assert await refresh(2100, 1050, solar=2150) is None
        assert (await refresh(2400, 1100, solar=2150)).watts["grid_battery"] == 600
        return
    if history_kind == "routing_revision":
        assert await refresh(600, 950, solar=2100) is None
        assert (await refresh(900, 950, solar=2150)).watts["solar_battery"] == 600
        return
    latest_clock = 1200 if history_kind == "closed_revision" else 300
    assert await refresh(150, 1000, receipt_seconds=latest_clock + 1) is None
    if history_kind in ("power", "legacy"):
        grid = 1000 if history_kind == "power" else 1050
        assert await refresh(300, grid, receipt_seconds=latest_clock + 2) is None
    if history_kind == "revision_during_reload":
        closed_grid = 450
        assert await refresh(600, 1100) is None
        assert await refresh(900, 1150) is None
        assert (await refresh(1200, 1200)).watts["grid_battery"] == 600
        return
    if history_kind == "closed_revision":
        # This clock is newer than the cumulative baseline and older than the
        # last power checkpoint. It must not lower the restart guard.
        assert await refresh(1050, 850, receipt_seconds=latest_clock + 2) is None
        assert saved["power"]["pairs"]["charge"]["wh"]["grid_battery"] == 1300
    recovery = 1300 if history_kind == "closed_revision" else 1050
    for seconds, grid in (
        (1500, 0),
        (1800, 750),
        (2100, 1000),
        (2400, recovery),
    ):
        assert await refresh(seconds, grid) is None
    assert (await refresh(2700, recovery + 50)).watts["grid_battery"] == 600


@pytest.mark.parametrize("regressed", [False, True])
async def test_closed_day_revision_waits_for_confirmation_then_seeds_power(
    coordinator_factory, monkeypatch, regressed
):
    monkeypatch.setattr(energy_mod, "DIRECTIONAL_BATTERY_POWER_SOURCE_VALIDATED", True)
    now = _START
    monkeypatch.setattr(energy_mod.dt_util, "utcnow", lambda: now)
    read = AsyncMock()
    manager = coordinator_factory(client=SimpleNamespace(lifetime_energy=read)).energy
    power = manager.directional_battery_power

    async def refresh(seconds, closed_grid, active_grid, *, receipt_seconds=None):
        nonlocal now
        reported_at = _START + timedelta(seconds=seconds)
        now = (
            reported_at
            if receipt_seconds is None
            else _START + timedelta(seconds=receipt_seconds)
        )
        read.return_value = {
            "start_date": (_START.date() - timedelta(days=1)).isoformat(),
            "last_report_date": reported_at.timestamp(),
            "grid_battery": [closed_grid, active_grid],
            "solar_battery": [100, 200],
            "battery_home": [0, 0],
            "battery_grid": [0, 0],
            "charge": [closed_grid + 100, active_grid + 200],
            "discharge": [0, 0],
        }
        await manager._async_refresh_site_energy(force=True)

    await refresh(0, 500, 1000)
    await refresh(300, 500, 1050)
    assert power.interval.watts["grid_battery"] == 600
    if regressed:
        now = _START + timedelta(minutes=20)
        read.return_value = {"last_report_date": now.timestamp()}
        await manager._async_refresh_site_energy(force=True)
        assert power.interval is power.average is None
        await refresh(900, 400, 1100, receipt_seconds=1201)
        for seconds, grid in ((1500, 1150), (1800, 1200)):
            await refresh(seconds, 400, grid)
            assert power.interval is power.average is None
        await refresh(2100, 400, 1250)
        assert power.interval.watts["grid_battery"] == 600
        return
    for seconds, closed in ((600, 450), (600, 450), (900, 400), (1200, 400)):
        await refresh(seconds, closed, 1100)
        assert power.interval is power.average is None
        assert "battery_energy_from_grid" not in manager.site_energy

    await refresh(1500, 400, 1150)
    assert power.interval.watts["grid_battery"] == 600
    assert power.average == power.interval
    assert "battery_energy_from_grid" not in manager.site_energy


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


def test_invalid_report_cannot_supply_a_new_recovery_checkpoint(replay):
    power = replay(0, overrides={"charge": [True]})
    assert power.recovery_checkpoint(_START) is None
    assert replay(0).interval is None
    assert replay(300, grid=1050).interval is None
    assert replay(600, grid=1100).interval.watts["grid_battery"] == 600


def test_late_startup_reference_uses_already_observed_rejection(replay):
    assert replay(300, overrides={"charge": [True]}).interval is None
    startup = DirectionalBatteryRecovery(
        _SOURCE,
        _START,
        1,
        {
            "charge": DirectionalBatteryPairReference(
                _START, {"charge": 3050, "grid_battery": 1050, "solar_battery": 2000}
            )
        },
    )
    assert replay(600, grid=11050, startup=startup).interval is None
    assert replay(900, grid=1100).interval is None
    assert replay(1200, grid=1150).interval.watts["grid_battery"] == 600
