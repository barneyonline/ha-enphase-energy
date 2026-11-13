"""The opt-in averaging setting affects only the four site power sensors."""

from datetime import timedelta
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util
from homeassistant.helpers.restore_state import RestoredExtraData
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_restore_state_shutdown_restart,
)

from custom_components.enphase_ev.const import OPT_USE_15_MINUTE_POWER_AVERAGE
from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseBatteryPowerSensor,
    EnphaseCurrentPowerConsumptionSensor,
    EnphaseGridPowerSensor,
    EnphaseSiteConsumptionPowerSensor,
)
from tests.components.enphase_ev.test_consumption_power_balance import source
from custom_components.enphase_ev.options_flow import OptionsFlowHandler
from custom_components.enphase_ev import _async_update_listener
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.power_restore import PowerHistory, PowerReading
from homeassistant.config_entries import ConfigEntryState


def read(sensor):
    """Advance the production update seam, then inspect its published value."""
    sensor._update_averaged_power()
    return sensor.native_value


@pytest.fixture
def averaged(coordinator_factory, config_entry, hass):
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_USE_15_MINUTE_POWER_AVERAGE: True}
    )
    coord = coordinator_factory()
    coord.config_entry = config_entry
    return coord


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize(
    ("cls", "flow"),
    [
        (EnphaseGridPowerSensor, "grid_import"),
        (EnphaseBatteryPowerSensor, "battery_discharge"),
        (EnphaseCurrentPowerConsumptionSensor, "solar_production"),
    ],
)
async def test_reset_and_reseed_preserve_first_interval(
    averaged, monkeypatch, cls, flow, reset
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = cls(averaged)
    energies = [100, 100.5, 0, 1, 1.1] if reset else [100, 100.5, 100.5, 101.5, 101.6]
    for i, energy in enumerate(energies):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            flow: {
                "value_kwh": energy,
                "fields_used": ["changed" if i >= 2 and not reset else flow],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        value = read(sensor)
        if i == 3:
            assert value == 12000
        if i == 4:
            assert value == 6600
        now[0] += timedelta(seconds=300)


@pytest.mark.parametrize(
    ("cls", "flow"),
    [
        (EnphaseCurrentPowerConsumptionSensor, "solar_production"),
        (EnphaseGridPowerSensor, "grid_import"),
        (EnphaseBatteryPowerSensor, "battery_discharge"),
    ],
)
async def test_reported_batching_is_averaged(averaged, monkeypatch, cls, flow):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = cls(averaged)
    total = 100
    readings = []
    for delta in [0, 1.3, 0.1, 0.1, 1.3, 0.1, 0.1, 0, 0, 0]:
        total += delta
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            flow: {
                "value_kwh": total,
                "fields_used": [flow],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        readings.append(read(sensor))
        assert read(sensor) == readings[-1]
        now[0] += timedelta(seconds=300)
    assert readings[0] is None
    assert readings[3:7] == [6000] * 4
    assert readings[-3:] == [800, 400, 0]
    assert sensor.extra_state_attributes["last_window_seconds"] == 900


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("cls", "positive", "negative"),
    [
        (EnphaseGridPowerSensor, "grid_import", "grid_export"),
        (EnphaseBatteryPowerSensor, "battery_discharge", "battery_charge"),
    ],
)
async def test_new_flow_does_not_inject_lifetime_total(
    averaged, config_entry, hass, monkeypatch, cls, positive, negative, enabled
):
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_USE_15_MINUTE_POWER_AVERAGE: enabled}
    )
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = cls(averaged)
    for i in range(4):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            positive: {
                "value_kwh": 100 + i * 0.5,
                "fields_used": [positive],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        if i >= 2:
            averaged.energy.site_energy[negative] = {
                "value_kwh": 20 + (i - 2) * 0.25,
                "fields_used": [negative],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        value = read(sensor)
        if i in (1, 2):
            assert value == 6000
        if i == 3:
            assert value == 3000
        now[0] += timedelta(seconds=300)


async def test_consumption_requires_same_start_and_end(averaged, hass):
    sensor = EnphaseSiteConsumptionPowerSensor(averaged)
    sensor.hass = hass
    now = dt_util.utcnow()
    ids = [
        source(hass, averaged, key, value)
        for key, value in (
            ("current_production_power", 6000),
            ("grid_power", -1000),
            ("battery_power", -2000),
        )
    ]
    for entity_id, value in zip(ids, (6000, -1000, -2000), strict=True):
        hass.states.async_set(
            entity_id,
            value,
            {
                "unit_of_measurement": "W",
                "sampled_at_utc": now.isoformat(),
                "last_window_seconds": 900,
            },
        )
    assert read(sensor) == 3000
    hass.states.async_set(
        ids[-1],
        1000,
        {
            "unit_of_measurement": "W",
            "sampled_at_utc": now.isoformat(),
            "last_window_seconds": 300,
        },
    )
    assert read(sensor) == 3000
    assert sensor._balance_using_cached
    assert sensor._balance_blocker["reason"] == "unaligned_window"
    hass.states.async_set(
        ids[-1],
        1000,
        {
            "unit_of_measurement": "W",
            "sampled_at_utc": (now - timedelta(seconds=1)).isoformat(),
            "last_window_seconds": 900,
        },
    )
    assert read(sensor) == 3000
    assert sensor._balance_blocker["reason"] == "unaligned_window"


async def test_consumption_energy_fallback_is_averaged(averaged, monkeypatch):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseSiteConsumptionPowerSensor(averaged)
    for i, wh in enumerate([100, 1400, 1500, 1600, 2900]):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "consumption": {
                "latest_bucket_wh": wh,
                "raw_bucket_count": 1,
                "start_date": "2026-01-01",
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        value = read(sensor)
        if i >= 3:
            assert value == 6000
        now[0] += timedelta(seconds=300)
    assert sensor.extra_state_attributes["last_window_seconds"] == 900


@pytest.mark.parametrize("corrupt", [False, True])
async def test_restore_keeps_full_rolling_history(averaged, monkeypatch, corrupt):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseGridPowerSensor(averaged)
    for i, energy in enumerate([100, 101.3, 101.4, 101.5]):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "grid_import": {
                "value_kwh": energy,
                "fields_used": ["import"],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        value = read(sensor)
        now[0] += timedelta(seconds=300)
    assert value == 6000
    data = sensor.extra_restore_state_data.as_dict()
    if corrupt:
        data["power_baseline"]["averaging_history"]["samples"][-1] = (
            now[0].timestamp(),
            101.5,
        )
    restored = EnphaseGridPowerSensor(averaged)
    restored.async_get_last_state = AsyncMock(return_value=None)
    restored.async_get_last_extra_data = AsyncMock(return_value=RestoredExtraData(data))
    await restored._async_restore_power()
    assert read(restored) == 6000
    if corrupt:
        assert not restored._average.samples
        return
    averaged.energy.site_energy["grid_import"]["value_kwh"] = 102.8
    averaged.energy.site_energy["grid_import"]["last_report_date"] = now[0]
    assert read(restored) == 6000


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize(
    ("cls", "flow"),
    [
        (EnphaseGridPowerSensor, "grid_import"),
        (EnphaseBatteryPowerSensor, "battery_discharge"),
        (EnphaseCurrentPowerConsumptionSensor, "solar_production"),
    ],
)
async def test_restart_after_reseed_keeps_first_interval(
    averaged, monkeypatch, cls, flow, reset
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = cls(averaged)

    def sample(energy, changed=False):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            flow: {
                "value_kwh": energy,
                "fields_used": ["new_source" if changed else flow],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }

    for i, energy in enumerate([100, 100.5, 0 if reset else 100.5]):
        sample(energy, changed=(i == 2 and not reset))
        read(sensor)
        now[0] += timedelta(seconds=300)
    saved = sensor.extra_restore_state_data.as_dict()
    # Startup already has a report newer than the saved reset/reseed baseline.
    sample(1 if reset else 101.5, changed=not reset)
    restored = cls(averaged)
    restored.async_get_last_state = AsyncMock(return_value=None)
    restored.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData(saved)
    )
    await restored._async_restore_power()
    assert read(restored) == 12000
    now[0] += timedelta(seconds=300)
    sample(1.1 if reset else 101.6, changed=not reset)
    assert read(restored) == 6600


async def test_restart_does_not_trust_synthetic_reseed_baseline(averaged, monkeypatch):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseGridPowerSensor(averaged)

    def sample(imported, exported):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "grid_import": {
                "value_kwh": imported,
                "fields_used": ["import"],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        if exported is not None:
            averaged.energy.site_energy["grid_export"] = {
                "value_kwh": exported,
                "fields_used": ["export"],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }

    for imported, exported in [(100, 50), (100.5, 50.25), (101, None)]:
        sample(imported, exported)
        read(sensor)
        now[0] += timedelta(seconds=300)
    saved = sensor.extra_restore_state_data.as_dict()
    sample(101.5, 50.5)
    restored = EnphaseGridPowerSensor(averaged)
    restored.async_get_last_state = AsyncMock(return_value=None)
    restored.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData(saved)
    )
    await restored._async_restore_power()
    assert read(restored) == 3000


async def test_restored_reseed_baseline_still_checks_counter_decreases(
    averaged, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseGridPowerSensor(averaged)
    for i, energy in enumerate([100, 100.5, 100.5]):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "grid_import": {
                "value_kwh": energy,
                "fields_used": ["new_source" if i == 2 else "import"],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        read(sensor)
        now[0] += timedelta(seconds=300)
    saved = sensor.extra_restore_state_data.as_dict()
    averaged.last_success_utc = now[0]
    averaged.energy.site_energy["grid_import"].update(
        value_kwh=100, last_report_date=now[0]
    )
    restored = EnphaseGridPowerSensor(averaged)
    restored.async_get_last_state = AsyncMock(return_value=None)
    restored.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData(saved)
    )
    await restored._async_restore_power()
    assert read(restored) == 0
    assert restored._last_method == "lifetime_reset"


async def test_restore_does_not_trust_a_point_without_source_identity(
    averaged, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseGridPowerSensor(averaged)
    averaged.last_success_utc = now[0]
    averaged.energy.site_energy = {
        "grid_import": {
            "value_kwh": 100,
            "fields_used": ["import"],
            "last_report_date": now[0],
            "interval_minutes": 5,
        }
    }
    read(sensor)
    saved = sensor.extra_restore_state_data.as_dict()
    saved["last_live_flow_sources"] = {}
    now[0] += timedelta(seconds=300)
    averaged.last_success_utc = now[0]
    averaged.energy.site_energy["grid_import"].update(
        value_kwh=99, last_report_date=now[0]
    )
    restored = EnphaseGridPowerSensor(averaged)
    restored.async_get_last_state = AsyncMock(return_value=None)
    restored.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData(saved)
    )
    await restored._async_restore_power()
    assert not restored._average.samples
    assert read(restored) is None


@pytest.mark.parametrize("enabled", [False, True])
async def test_polling_checkbox_default_and_saved_value(hass, config_entry, enabled):
    handler = OptionsFlowHandler(config_entry)
    handler.hass = hass
    form = await handler.async_step_settings()
    values = form["data_schema"]({})
    assert values[OPT_USE_15_MINUTE_POWER_AVERAGE] is False
    values[OPT_USE_15_MINUTE_POWER_AVERAGE] = enabled
    result = await handler.async_step_settings(values)
    assert result["data"][OPT_USE_15_MINUTE_POWER_AVERAGE] is enabled
    hass.config_entries.async_update_entry(config_entry, options=result["data"])
    form = await handler.async_step_settings()
    assert form["data_schema"]({})[OPT_USE_15_MINUTE_POWER_AVERAGE] is enabled


async def test_clock_changes_missing_data_reset_and_expiry(averaged, monkeypatch):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseGridPowerSensor(averaged)

    def sample(value, clock=True):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "grid_import": {
                "value_kwh": value,
                "fields_used": ["import"],
                "last_report_date": now[0] if clock else None,
                "interval_minutes": 5,
            }
        }
        return read(sensor)

    assert sample(100) is None
    now[0] += timedelta(seconds=300)
    assert sample(100.5) == 6000
    now[0] += timedelta(seconds=300)
    assert sample(101, False) == 6000  # retained, never join clock domains
    assert sensor._average.value() is None
    now[0] += timedelta(seconds=300)
    assert sample(101.5, False) == 6000
    now[0] += timedelta(seconds=300)
    assert sample(1, False) == 0
    assert sensor._average.value() is None
    now[0] += timedelta(seconds=300)
    averaged.energy.site_energy = {}
    read(sensor)
    # A retained display may bridge missing inputs without inventing samples.
    assert len(sensor._average.samples) == 1
    averaged.energy.site_energy = {"grid_import": {"value_kwh": None}}
    read(sensor)
    now[0] += timedelta(seconds=300)
    sample(2)
    before = sensor._last_sample_ts
    averaged.energy.site_energy["grid_import"]["last_report_date"] = now[0] + timedelta(
        minutes=5
    )
    read(sensor)
    assert sensor._last_sample_ts == before
    now[0] += timedelta(minutes=40)
    read(sensor)
    assert sensor._last_sample_ts == before


@pytest.mark.parametrize(
    "cls", [EnphaseGridPowerSensor, EnphaseSiteConsumptionPowerSensor]
)
async def test_mode_change_discards_old_restore(averaged, hass, cls):
    sensor = cls(averaged)
    sensor.hass = hass
    sensor.async_get_last_state = AsyncMock(return_value=None)
    sensor.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData({"power_baseline": {"averaging_enabled": False}})
    )
    await sensor._async_restore_power()
    assert sensor._power_history.latest(dt_util.utcnow().timestamp()) is None
    assert not sensor._average.samples


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "cls",
    [
        EnphaseGridPowerSensor,
        EnphaseBatteryPowerSensor,
        EnphaseCurrentPowerConsumptionSensor,
        EnphaseSiteConsumptionPowerSensor,
    ],
)
async def test_untagged_legacy_cache_is_only_used_without_smoothing(
    averaged, hass, enabled, cls
):
    hass.config_entries.async_update_entry(
        averaged.config_entry, options={OPT_USE_15_MINUTE_POWER_AVERAGE: enabled}
    )
    sensor = cls(averaged)
    sensor.hass = hass
    now = dt_util.utcnow()
    sample = now - timedelta(seconds=60)
    history = PowerHistory()
    history.remember(
        PowerReading(
            1234,
            sample.timestamp(),
            sample.timestamp(),
            {
                "method": (
                    "power_balance"
                    if cls is EnphaseSiteConsumptionPowerSensor
                    else "lifetime_energy_window"
                ),
                "sampled_at_utc": sample.isoformat(),
                "last_window_seconds": 1800,
            },
        )
    )
    # Earlier beta restore data did not identify an averaging mode.
    sensor.async_get_last_state = AsyncMock(return_value=None)
    sensor.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData({"power_history": history.as_dict()})
    )
    try:
        await sensor._async_restore_power()
        assert read(sensor) == (None if enabled else 1234)
    finally:
        await sensor.async_will_remove_from_hass()


@pytest.mark.parametrize(
    "attrs", [{}, {"last_window_seconds": 901}, {"last_window_seconds": 900}]
)
async def test_consumption_rejects_missing_or_invalid_windows(averaged, hass, attrs):
    sensor = EnphaseSiteConsumptionPowerSensor(averaged)
    sensor.hass = hass
    entity_id = source(hass, averaged, "current_production_power", 500)
    hass.states.async_set(entity_id, 500, {"unit_of_measurement": "W", **attrs})
    assert read(sensor) is None
    assert sensor._balance_blocker["reason"] == "unaligned_window"


@pytest.mark.parametrize("corrupt", [False, True])
async def test_consumption_fallback_restore_buffer(
    averaged, hass, monkeypatch, corrupt
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseSiteConsumptionPowerSensor(averaged)
    for wh in [100, 1400, 1500, 1600]:
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "consumption": {
                "latest_bucket_wh": wh,
                "raw_bucket_count": 1,
                "start_date": "2026-01-01",
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        read(sensor)
        now[0] += timedelta(seconds=300)
    data = sensor.extra_restore_state_data.as_dict()
    if corrupt:
        stamp, energy = data["power_baseline"]["averaging_history"]["samples"][-1]
        data["power_baseline"]["averaging_history"]["samples"][-1] = (stamp - 1, energy)
    restored = EnphaseSiteConsumptionPowerSensor(averaged)
    restored.hass = hass
    restored.async_get_last_extra_data = AsyncMock(return_value=RestoredExtraData(data))
    await restored._async_restore_power()
    assert bool(restored._average.samples) is not corrupt
    assert read(restored) == 6000
    await restored.async_will_remove_from_hass()


@pytest.mark.parametrize("enabled", [False, True])
async def test_changing_average_mode_reloads(hass, config_entry, monkeypatch, enabled):
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_USE_15_MINUTE_POWER_AVERAGE: enabled}
    )
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=SimpleNamespace(),
        applied_data=dict(config_entry.data),
        applied_options={OPT_USE_15_MINUTE_POWER_AVERAGE: not enabled},
    )
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)
    reload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    await _async_update_listener(hass, config_entry)
    reload.assert_awaited_once_with(config_entry.entry_id)


async def test_saving_default_off_does_not_reload(hass, config_entry, monkeypatch):
    hass.config_entries.async_update_entry(
        config_entry, options={OPT_USE_15_MINUTE_POWER_AVERAGE: False}
    )
    coord = SimpleNamespace(
        apply_auth_storage_config=Mock(), async_apply_config_entry_options=AsyncMock()
    )
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord, applied_data=dict(config_entry.data), applied_options={}
    )
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)
    reload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    await _async_update_listener(hass, config_entry)
    reload.assert_not_awaited()
    coord.async_apply_config_entry_options.assert_awaited_once_with({})


async def test_short_average_preserves_extreme_confirmation(averaged, monkeypatch):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseGridPowerSensor(averaged)
    for i, energy in enumerate([1000, 1010, 1020]):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "grid_import": {
                "value_kwh": energy,
                "fields_used": ["import"],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        value = read(sensor)
        if i == 1:
            assert value is None
            assert sensor._last_method == "extreme_pending"
            assert read(sensor) is None
        if i == 2:
            assert value == 36_000_000
            assert sensor._last_method == "extreme_confirmed"
        now[0] += timedelta(seconds=1)


@pytest.mark.parametrize("restart", [False, True])
async def test_confirmed_extreme_seeds_next_average(averaged, monkeypatch, restart):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseCurrentPowerConsumptionSensor(averaged)
    readings = []
    for i, energy in enumerate([100_000, 100_100, 100_200, 100_250, 100_275]):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "solar_production": {
                "value_kwh": energy,
                "fields_used": ["production"],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        readings.append(read(sensor))
        assert read(sensor) == readings[-1]
        if restart and i == 2:
            data = sensor.extra_restore_state_data.as_dict()
            sensor = EnphaseCurrentPowerConsumptionSensor(averaged)
            sensor.async_get_last_state = AsyncMock(return_value=None)
            sensor.async_get_last_extra_data = AsyncMock(
                return_value=RestoredExtraData(data)
            )
            await sensor._async_restore_power()
        now[0] += timedelta(seconds=300)
    assert readings == [None, None, 1_200_000, 600_000, 450_000]


@pytest.mark.parametrize("flows", [{}, {"grid_import": {"value_kwh": None}}])
async def test_averaging_starts_without_placeholder_energy(averaged, flows):
    averaged.last_success_utc = dt_util.utcnow()
    averaged.energy.site_energy = flows
    sensor = EnphaseGridPowerSensor(averaged)
    assert read(sensor) is None
    assert not sensor._average.samples


async def test_enabled_average_properties_do_not_advance_history(averaged, monkeypatch):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseGridPowerSensor(averaged)
    for energy in [100, 100.5]:
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "grid_import": {
                "value_kwh": energy,
                "fields_used": ["import"],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        read(sensor)
        now[0] += timedelta(seconds=300)
    before = sensor._average.as_dict()
    averaged.energy.site_energy["grid_import"].update(
        value_kwh=101.5, last_report_date=now[0]
    )
    assert sensor.native_value == 6000
    sensor.available
    sensor.extra_state_attributes
    sensor.extra_restore_state_data
    assert sensor._average.as_dict() == before
    assert read(sensor) == 9000


async def test_consumption_properties_do_not_advance_history(averaged, monkeypatch):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseSiteConsumptionPowerSensor(averaged)
    for wh in [100, 600]:
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "consumption": {
                "latest_bucket_wh": wh,
                "raw_bucket_count": 1,
                "start_date": "2026-01-01",
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        read(sensor)
        now[0] += timedelta(seconds=300)
    before = sensor._average.as_dict()
    averaged.energy.site_energy["consumption"].update(
        latest_bucket_wh=1600, last_report_date=now[0]
    )
    assert sensor.native_value == 6000
    sensor.available
    sensor.extra_state_attributes
    sensor.extra_restore_state_data
    assert sensor._average.as_dict() == before
    assert read(sensor) == 9000


async def test_enabled_average_advances_on_real_publication(
    averaged, hass, config_entry, monkeypatch
):
    from tests.components.enphase_ev.test_site_power_restart import (
        inputs,
        add_entities,
        remove_entities,
    )

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    averaged.last_success_utc = now[0]
    inputs(averaged, now[0] - timedelta(seconds=300), 0)
    entities, component = await add_entities(hass, config_entry, averaged)
    try:
        inputs(averaged, now[0], 1)
        averaged.async_update_listeners()
        await hass.async_block_till_done()
        assert [
            float(hass.states.get(entity.entity_id).state) for entity in entities
        ] == [2100, 1200, -600, 2700]
        consumption = entities[-1]
        attrs = consumption.extra_state_attributes
        saved = consumption.extra_restore_state_data.as_dict()["power_history"][
            "samples"
        ][-1]
        assert saved["attributes"]["sampled_at_utc"] == attrs["sampled_at_utc"]
        assert (
            saved["attributes"]["freshness_reference_utc"]
            == attrs["freshness_reference_utc"]
        )
        assert (
            saved["attributes"]["last_window_seconds"] == attrs["last_window_seconds"]
        )
    finally:
        await remove_entities(component, entities)


@pytest.fixture
def consumption_average(averaged, monkeypatch):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])

    def sample(sensor, wh, *, seconds=0, **metadata):
        now[0] += timedelta(seconds=seconds)
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "consumption": {
                "latest_bucket_wh": wh,
                "raw_bucket_count": 1,
                "start_date": "2026-01-01",
                "last_report_date": now[0],
                "interval_minutes": 5,
                **metadata,
            }
        }
        return read(sensor)

    return averaged, now, sample


@pytest.mark.parametrize("enabled", [False, True])
async def test_consumption_average_confirms_extremes_and_ignores_duplicates(
    consumption_average, hass, enabled
):
    coord, now, sample = consumption_average
    hass.config_entries.async_update_entry(
        coord.config_entry, options={OPT_USE_15_MINUTE_POWER_AVERAGE: enabled}
    )
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    assert sample(sensor, 100) is None
    assert sample(sensor, 700, seconds=1) == (None if enabled else 7200)
    for _ in range(3):
        assert sample(sensor, 700) == (None if enabled else 7200)
    if enabled:
        assert sensor.extra_state_attributes["method"] == "extreme_pending"
        assert sensor.extra_state_attributes["sampled_at_utc"] is None
    assert sample(sensor, 1300, seconds=1) == (2_160_000 if enabled else 7200)
    assert sensor.extra_state_attributes["method"] == (
        "extreme_confirmed" if enabled else "consumption_bucket_delta"
    )


async def test_consumption_average_retains_accepted_clock_and_recovers(
    consumption_average,
):
    coord, now, sample = consumption_average
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    assert sample(sensor, 100) is None
    assert sample(sensor, 101, seconds=1) == 3600
    accepted = now[0].isoformat()
    assert sample(sensor, 10_101, seconds=1) == 3600
    for _ in range(3):
        assert sample(sensor, 10_101) == 3600
        assert sensor.extra_state_attributes["sampled_at_utc"] == accepted
        assert sensor.extra_state_attributes["last_window_seconds"] == 1
        assert coord.energy.consumption_power_diagnostics["using_cached"]
    # Recover from the newest interval without retaining the suspect jump.
    assert sample(sensor, 10_201, seconds=300) == 1200
    assert sensor.extra_state_attributes["sampled_at_utc"] == now[0].isoformat()
    assert not coord.energy.consumption_power_diagnostics["using_cached"]
    assert sensor.extra_state_attributes["method"] == "consumption_bucket_delta"


async def test_consumption_average_pending_does_not_extend_expiry(consumption_average):
    coord, now, sample = consumption_average
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sample(sensor, 100)
    assert sample(sensor, 101, seconds=1) == 3600
    accepted = now[0]
    assert sample(sensor, 10_101, seconds=1) == 3600
    now[0] = accepted + timedelta(seconds=1800)
    coord.last_success_utc = now[0]
    assert read(sensor) == 3600
    assert not sensor.available
    assert sensor.extra_state_attributes["sampled_at_utc"] == accepted.isoformat()


@pytest.mark.parametrize("boundary", ["gap", "start_date", "interval"])
async def test_consumption_average_reseed_clears_extreme_confirmation(
    consumption_average, boundary
):
    coord, now, sample = consumption_average
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    assert sample(sensor, 100) is None
    assert sample(sensor, 700, seconds=1) is None
    metadata = (
        {"start_date": "2026-01-02"}
        if boundary == "start_date"
        else {"interval_minutes": 10} if boundary == "interval" else {}
    )
    assert (
        sample(sensor, 1000, seconds=1801 if boundary == "gap" else 1, **metadata)
        is None
    )
    # A different source window must not confirm the old pending extreme.
    assert sample(sensor, 1600, seconds=1, **metadata) is None
    assert sample(sensor, 2200, seconds=1, **metadata) == 2_160_000


async def test_consumption_average_restart_retains_only_accepted_power(
    consumption_average, hass
):
    coord, now, sample = consumption_average
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    assert sample(sensor, 100) is None
    assert sample(sensor, 101, seconds=1) == 3600
    accepted = now[0].isoformat()
    assert sample(sensor, 10_101, seconds=1) == 3600
    saved = sensor.extra_restore_state_data.as_dict()
    restored = EnphaseSiteConsumptionPowerSensor(coord)
    restored.hass = hass
    restored.async_get_last_state = AsyncMock(return_value=None)
    restored.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData(saved)
    )
    try:
        await restored._async_restore_power()
        assert read(restored) == 3600
        assert restored.extra_state_attributes["sampled_at_utc"] == accepted
        assert sample(restored, 20_101, seconds=1) == 3600
        assert sample(restored, 20_101) == 3600
        assert restored.extra_state_attributes["sampled_at_utc"] == accepted
        assert sample(restored, 30_101, seconds=1) == 36_000_000
        assert restored.extra_state_attributes["method"] == "extreme_confirmed"
    finally:
        await restored.async_will_remove_from_hass()


@pytest.mark.parametrize("method", ["consumption_bucket_delta", "extreme_confirmed"])
async def test_consumption_average_restore_does_not_authorize_extreme_watts(
    consumption_average, hass, method
):
    coord, now, sample = consumption_average
    sensor = EnphaseSiteConsumptionPowerSensor(coord)
    sample(sensor, 100)
    sample(sensor, 700, seconds=1)
    saved = sensor.extra_restore_state_data.as_dict()
    # The original opt-in implementation could save an unconfirmed short
    # average here. Even a previously confirmed extreme needs new live proof.
    saved.update(
        last_power_w=2_160_000,
        power_sample_ts=now[0].timestamp(),
        last_window_seconds=1,
        method=method,
    )
    restored = EnphaseSiteConsumptionPowerSensor(coord)
    restored.hass = hass
    restored.async_get_last_state = AsyncMock(return_value=None)
    restored.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData(saved)
    )
    try:
        await restored._async_restore_power()
        assert read(restored) is None
        assert sample(restored, 1300, seconds=1) is None
        assert sample(restored, 1900, seconds=1) == 2_160_000
    finally:
        await restored.async_will_remove_from_hass()


async def test_rolling_extreme_requires_new_energy(averaged, monkeypatch):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseGridPowerSensor(averaged)
    results = []
    for energy in (1000, 1010, 1010, 1010):
        averaged.last_success_utc = now[0]
        averaged.energy.site_energy = {
            "grid_import": {
                "value_kwh": energy,
                "fields_used": ["import"],
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        results.append(read(sensor))
        now[0] += timedelta(seconds=1)
    assert results == [None, None, 0, 0]


async def test_first_consumption_average_cannot_publish_extreme(averaged, monkeypatch):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseSiteConsumptionPowerSensor(averaged)
    for wh in (0, 1000):
        averaged.energy.site_energy = {
            "consumption": {
                "latest_bucket_wh": wh,
                "raw_bucket_count": 1,
                "start_date": "2026-01-01",
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        assert read(sensor) is None
        assert sensor._last_power_ts is None
        now[0] += timedelta(seconds=1)


@pytest.mark.parametrize(
    ("increments", "expected"),
    [
        ((1, 1001, 1001), (3600, 3600, 0)),
        ((1, 1001, 2001), (3600, 3600, 3_600_000)),
    ],
)
async def test_consumption_fallback_quarantines_rolling_extremes(
    averaged, monkeypatch, increments, expected
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sensor = EnphaseSiteConsumptionPowerSensor(averaged)
    accepted_at = now[0] + timedelta(seconds=1)
    stamps = tuple(now[0] + timedelta(seconds=i) for i in range(4))
    for index, (wh, stamp) in enumerate(zip((0, *increments), stamps, strict=True)):
        now[0] = stamp
        averaged.last_success_utc = stamp
        averaged.energy.site_energy = {
            "consumption": {
                "latest_bucket_wh": wh,
                "raw_bucket_count": 1,
                "start_date": "2026-01-01",
                "last_report_date": stamp,
                "interval_minutes": 5,
            }
        }
        assert read(sensor) == (None if index == 0 else expected[index - 1])
        if index == 2:
            assert sensor._last_power_ts == accepted_at.timestamp()
            assert sensor._last_rejection["reason"] == "extreme_pending"


@pytest.mark.parametrize(
    "rejection", [None, "extreme_pending", "interval_discontinuity"]
)
async def test_averaged_balance_expiry_publishes_newer_fallback(
    averaged, hass, config_entry, monkeypatch, rejection
):
    from tests.components.enphase_ev.test_site_power_restart import (
        add_entities,
        inputs,
        remove_entities,
    )

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    averaged.last_success_utc = now[0]
    # These published source states have no timers of their own. The
    # consumption timer must transition to the fallback without a source event.
    for key, value in (
        ("current_production_power", 6000),
        ("grid_power", -1000),
        ("battery_power", -2000),
    ):
        entity_id = source(hass, averaged, key, None)
        hass.states.async_set(
            entity_id,
            value,
            {
                "unit_of_measurement": "W",
                "sampled_at_utc": now[0].isoformat(),
                "last_window_seconds": 900,
            },
        )
    inputs(averaged, now[0], 0)
    entities, component = await add_entities(
        hass, config_entry, averaged, (EnphaseSiteConsumptionPowerSensor,)
    )
    consumption = entities[0]
    try:
        assert hass.states.get(consumption.entity_id).state == "3000"

        if rejection is not None:
            await remove_entities(component, entities)
            await async_mock_restore_state_shutdown_restart(hass)
            entities, component = await add_entities(
                hass, config_entry, averaged, (EnphaseSiteConsumptionPowerSensor,)
            )
            consumption = entities[0]
            assert consumption._restored_reading is not None

        now[0] += timedelta(minutes=5)
        averaged.last_success_utc = now[0]
        inputs(averaged, now[0], 1)
        averaged.async_update_listeners()
        await hass.async_block_till_done()
        assert hass.states.get(consumption.entity_id).state == "3000"

        expiry = now[0] + timedelta(minutes=25)
        fallback_clock = now[0].isoformat()
        if rejection is not None:
            now[0] += timedelta(seconds=1)
            averaged.last_success_utc = now[0]
            fallback = averaged.energy.site_energy["consumption"]
            fallback["last_report_date"] = now[0]
            if rejection == "extreme_pending":
                fallback["latest_bucket_wh"] = 100_600
            else:
                fallback["interval_minutes"] = 10
            averaged.async_update_listeners()
            await hass.async_block_till_done()
            assert consumption._last_rejection["reason"] == rejection

        now[0] = expiry
        async_fire_time_changed(hass, now[0])
        await hass.async_block_till_done()
        state = hass.states.get(consumption.entity_id)
        assert state.state == "1200"
        assert state.attributes["method"] == "consumption_bucket_delta"
        assert state.attributes["sampled_at_utc"] == fallback_clock
        diagnostics = averaged.energy.consumption_power_diagnostics
        assert diagnostics["using_cached"] is (rejection is not None)
        assert diagnostics["method"] == "consumption_bucket_delta"
        assert diagnostics["last_valid_power_w"] == 1200
        assert consumption._restored_reading is None

        now[0] += timedelta(minutes=5)
        async_fire_time_changed(hass, now[0])
        await hass.async_block_till_done()
        assert hass.states.get(consumption.entity_id).state == "unavailable"
    finally:
        await remove_entities(component, entities)


async def test_rejected_advancing_sample_cannot_validate_private_restore(
    averaged, hass, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    averaged.last_success_utc = now[0]
    sensor = EnphaseSiteConsumptionPowerSensor(averaged)
    sensor.hass = hass
    sensor.async_get_last_state = AsyncMock(return_value=None)
    saved = sensor.extra_restore_state_data.as_dict()
    saved.update(
        latest_bucket_wh=600,
        raw_bucket_count=1,
        start_date="2026-01-01",
        energy_ts=now[0].timestamp(),
        interval_minutes=5,
        last_power_w=1200,
        last_window_seconds=300,
        method="consumption_bucket_delta",
        power_sample_ts=now[0].timestamp(),
    )
    sensor.async_get_last_extra_data = AsyncMock(return_value=RestoredExtraData(saved))
    try:
        await sensor._async_restore_power()
        assert not sensor.available
        now[0] += timedelta(seconds=1)
        averaged.energy.site_energy = {
            "consumption": {
                "latest_bucket_wh": 1600,
                "raw_bucket_count": 1,
                "start_date": "2026-01-01",
                "last_report_date": now[0],
                "interval_minutes": 5,
            }
        }
        for _ in range(2):
            assert read(sensor) is None
            assert not sensor.available
            assert sensor._last_power_ts is None
        assert sensor._last_rejection["reason"] == "extreme_pending"

        now[0] += timedelta(seconds=1)
        averaged.energy.site_energy["consumption"]["last_report_date"] = now[0]
        assert read(sensor) == 0
        assert sensor.available
    finally:
        await sensor.async_will_remove_from_hass()
