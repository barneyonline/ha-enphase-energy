"""Accepted cloud power survives real entity restoration without rebasing clocks."""

from datetime import timedelta
import logging
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import EVENT_STATE_CHANGED
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_restore_state_shutdown_restart,
)

from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.power_restore import PowerHistory, PowerReading
from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseBatteryPowerSensor,
    EnphaseCurrentPowerConsumptionSensor,
    EnphaseGridPowerSensor,
    EnphaseSiteConsumptionPowerSensor,
)

CLASSES = (
    EnphaseCurrentPowerConsumptionSensor,
    EnphaseGridPowerSensor,
    EnphaseBatteryPowerSensor,
    EnphaseSiteConsumptionPowerSensor,
)


def inputs(coord, timestamp, step):
    coord.current_power_runtime.replace_snapshot(
        w=2000 + step * 100,
        sample_utc=timestamp,
        source="test",
        reported_units="W",
    )
    coord.current_power_runtime.received_utc = timestamp
    coord.energy.site_energy = {
        key: {
            "value_kwh": value,
            "fields_used": [key],
            "last_report_date": timestamp,
            "interval_minutes": 5,
        }
        for key, value in (
            ("grid_import", 100 + step * 0.2),
            ("grid_export", 80 + step * 0.1),
            ("battery_charge", 20 + step * 0.1),
            ("battery_discharge", 30 + step * 0.05),
        )
    }
    coord.energy.site_energy["consumption"] = {
        "latest_bucket_wh": 500 + step * 100,
        "raw_bucket_count": 2,
        "start_date": "2026-01-01",
        "last_report_date": timestamp,
        "interval_minutes": 5,
    }


async def add_entities(hass, config_entry, coord, classes=CLASSES):
    coord.update_interval = None
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"] = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="sensor",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    component._platforms["sensor"].config_entry = config_entry
    entities = [cls(coord) for cls in classes]
    for entity in entities:
        entry = er.async_get(hass).async_get_or_create(
            "sensor",
            DOMAIN,
            entity.unique_id,
            suggested_object_id=entity._key,
        )
        entity.entity_id = entry.entity_id
    await component._platforms["sensor"].async_add_entities(entities)
    await hass.async_block_till_done()
    return entities, component


async def remove_entities(component, entities):
    for entity in reversed(entities):
        await component.async_remove_entity(entity.entity_id)


@pytest.mark.parametrize("downtime", [60, 299])
@pytest.mark.parametrize("warmup", ["missing", "placeholder", "partial", "same_bucket"])
async def test_all_power_first_publication_after_restart(
    hass, config_entry, coordinator_factory, monkeypatch, downtime, warmup
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    accepted_at = now[0]
    coord = coordinator_factory()
    inputs(coord, accepted_at - timedelta(minutes=5), 0)
    entities, component = await add_entities(hass, config_entry, coord)
    inputs(coord, accepted_at, 1)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    expected = [2100, 1200, -600, 2700]
    for entity, value in zip(entities, expected, strict=True):
        assert float(hass.states.get(entity.entity_id).state) == value
    saved = [entity.extra_restore_state_data.as_dict() for entity in entities]
    assert all(len(data["power_history"]["samples"]) >= 1 for data in saved)
    await remove_entities(component, entities)
    await async_mock_restore_state_shutdown_restart(hass)

    now[0] += timedelta(seconds=downtime)
    coord = coordinator_factory()
    if warmup == "placeholder":
        coord._restored_site_energy_channels = {
            "grid_import",
            "grid_export",
            "battery_charge",
            "battery_discharge",
        }
    elif warmup == "partial":
        coord.energy.site_energy = {"grid_import": {"value_kwh": None}}
    elif warmup == "same_bucket":
        inputs(coord, accepted_at, 1)
    publications = []
    unsubscribe = hass.bus.async_listen(
        EVENT_STATE_CHANGED,
        lambda event: publications.append(event.data["new_state"]),
    )
    entities, component = await add_entities(hass, config_entry, coord)
    for entity, value in zip(entities, expected, strict=True):
        state = hass.states.get(entity.entity_id)
        assert float(state.state) == value
        assert state.attributes["sampled_at_utc"] == accepted_at.isoformat()
        assert state.attributes["using_cached"] is (
            warmup != "same_bucket" or entity._key != "current_production_power"
        )
        assert all(
            state.state not in {"unavailable", "unknown"}
            for state in publications
            if state is not None and state.entity_id == entity.entity_id
        )
    unsubscribe()
    await remove_entities(component, entities)
    await async_mock_restore_state_shutdown_restart(hass)

    # Repeated restoration and missing payloads preserve the exact original expiry.
    now[0] = accepted_at + timedelta(seconds=1799)
    coord = coordinator_factory()
    entities, component = await add_entities(hass, config_entry, coord)
    for entity, value in zip(entities, expected, strict=True):
        assert float(hass.states.get(entity.entity_id).state) == value
    now[0] += timedelta(seconds=1)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert all(
        hass.states.get(entity.entity_id).state == "unavailable" for entity in entities
    )
    # New live inputs recover even when the accepted reading has expired.
    inputs(coord, now[0], 2)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert all(
        hass.states.get(entity.entity_id).state != "unavailable" for entity in entities
    )
    await remove_entities(component, entities)


async def test_bucket_only_consumption_restores_before_live_validation(
    hass, config_entry, coordinator_factory, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    inputs(coord, now[0] - timedelta(minutes=5), 0)
    entities, component = await add_entities(
        hass, config_entry, coord, (EnphaseSiteConsumptionPowerSensor,)
    )
    inputs(coord, now[0], 1)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    entity = entities[0]
    assert hass.states.get(entity.entity_id).state == "1200"
    await remove_entities(component, entities)
    await async_mock_restore_state_shutdown_restart(hass)
    now[0] += timedelta(minutes=1)
    entities, component = await add_entities(
        hass, config_entry, coordinator_factory(), (EnphaseSiteConsumptionPowerSensor,)
    )
    state = hass.states.get(entities[0].entity_id)
    assert state.state == "1200"
    assert state.attributes["using_cached"] is True
    await remove_entities(component, entities)


@pytest.mark.parametrize("cls", [EnphaseGridPowerSensor, EnphaseBatteryPowerSensor])
async def test_private_baseline_does_not_require_numeric_display_state(
    hass, config_entry, coordinator_factory, monkeypatch, cls
):
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    coord = coordinator_factory()
    inputs(coord, now - timedelta(minutes=5), 0)
    entity = cls(coord)
    entity.native_value
    inputs(coord, now, 1)
    expected = entity.native_value
    extra = entity.extra_restore_state_data
    restored_coord = coordinator_factory()
    restored = cls(restored_coord)
    restored.async_get_last_extra_data = AsyncMock(return_value=extra)
    restored.async_get_last_state = AsyncMock(return_value=None)
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"] = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="sensor",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    component._platforms["sensor"].config_entry = config_entry
    restored.entity_id = "sensor.private_power"
    await component._platforms["sensor"].async_add_entities([restored])
    assert float(hass.states.get(restored.entity_id).state) == expected
    assert restored._live_flow_sample_count == 2
    await component.async_remove_entity(restored.entity_id)


@pytest.mark.parametrize(
    "raw", [None, [], {}, {"version": 2}, {"version": 1, "samples": None}]
)
def test_history_ignores_missing_or_unknown_formats(raw):
    history = PowerHistory()
    history.restore(raw, 10000)
    assert history.latest(10000) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"watts": True},
        {"watts": "1"},
        {"watts": float("nan")},
        {"watts": float("inf")},
        {"watts": 1_000_000},
        {"sampled_ts": None},
        {"sampled_ts": -1},
        {"sampled_ts": 10001},
        {"freshness_ts": 0},
        {"freshness_ts": float("inf")},
        {"freshness_ts": 10001},
        {"freshness_ts": 8200},
        {"attributes": None},
    ],
)
def test_history_falls_back_to_older_valid_reading(changes):
    first = PowerReading(-100, 9900, 9900, {"method": "test"})
    second = PowerReading(200, 9950, 9950, {}).as_dict()
    second.update(changes)
    history = PowerHistory()
    history.restore({"version": 1, "samples": [first.as_dict(), second]}, 10000)
    assert history.latest(10000) == first
    assert history.latest(11700) is None
    assert PowerReading.from_dict([], 10000) is None


def test_history_is_bounded_and_unchanged_reads_do_not_renew_it():
    history = PowerHistory()
    for stamp in range(9997, 10000):
        history.remember(PowerReading(0, stamp, stamp, {}))
    history.remember(PowerReading(0, 9999, 9999, {}))
    assert len(history.samples) == 2
    assert history.latest(10000).freshness_ts == 9999


@pytest.mark.parametrize(
    "changes",
    [
        {"received_ts": float("nan")},
        {"received_ts": 10001},
        {"received_ts": -1},
        {"freshness_ts": 9999},
        {"watts": 10**1000},
    ],
)
def test_history_rejects_untrusted_acquisition_or_extended_freshness(changes):
    raw = PowerReading(100, 9900, 9900, {}, 9950).as_dict()
    raw.update(changes)
    assert PowerReading.from_dict(raw, 10000) is None


def test_history_preserves_overnight_acquisition_and_corrected_values():
    reading = PowerReading(0, 100, 9900, {}, 9900)
    assert PowerReading.from_dict(reading.as_dict(), 10000) == reading
    history = PowerHistory()
    history.remember(reading)
    history.remember(PowerReading(1_000_000, 9999, 9999, {}))
    history.remember(PowerReading(float("nan"), 9999, 9999, {}))
    assert history.latest(10000) == reading
    corrected = PowerReading(1, 100, 9900, {}, 9900)
    history.remember(corrected)
    assert history.samples == [corrected]
    history = PowerHistory()
    history.restore(
        {"version": 1, "samples": [PowerReading(-10, 9900, 9900, {}).as_dict()]},
        10000,
        nonnegative=True,
    )
    assert history.samples == []


@pytest.mark.parametrize("cls", CLASSES)
async def test_zero_and_selected_kw_units_restore_in_native_watts(
    hass, config_entry, coordinator_factory, monkeypatch, cls
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    inputs(coord, now[0] - timedelta(minutes=5), 0)
    entities, component = await add_entities(hass, config_entry, coord, (cls,))
    inputs(coord, now[0], 0)
    coord.current_power_runtime.replace_snapshot(w=0)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    # The display conversion must not change private stored watts.
    entity = entities[0]
    entity._sensor_option_unit_of_measurement = "kW"
    entity.async_write_ha_state()
    assert (
        entity.extra_restore_state_data.as_dict()["power_history"]["samples"][-1][
            "watts"
        ]
        == 0
    )
    await remove_entities(component, entities)
    await async_mock_restore_state_shutdown_restart(hass)
    now[0] += timedelta(minutes=1)
    entities, component = await add_entities(
        hass, config_entry, coordinator_factory(), (cls,)
    )
    assert float(hass.states.get(entities[0].entity_id).state) == 0
    assert entities[0].native_value == 0
    await remove_entities(component, entities)


@pytest.mark.parametrize("baseline_clock", [None, float("nan"), float("inf"), "future"])
async def test_cached_power_expires_even_with_missing_or_invalid_private_baseline(
    hass, config_entry, coordinator_factory, monkeypatch, baseline_clock
):
    from custom_components.enphase_ev.power_restore import PowerStoredData
    from custom_components.enphase_ev.sensor_site_energy import (
        _SiteLifetimePowerRestoreData,
    )

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    original = now[0]
    history = PowerHistory()
    history.remember(
        PowerReading(
            1200,
            original.timestamp(),
            original.timestamp(),
            {"method": "lifetime_energy_window"},
        )
    )
    baseline = (
        None
        if baseline_clock is None
        else {
            "last_flow_kwh": {"grid_import": 1.1},
            "last_sample_ts": (
                (original + timedelta(days=1)).timestamp()
                if baseline_clock == "future"
                else baseline_clock
            ),
        }
    )
    extra = PowerStoredData(
        _SiteLifetimePowerRestoreData({}, None, None, None), history, baseline
    )
    sensor = EnphaseGridPowerSensor(coordinator_factory())
    sensor.entity_id = "sensor.history_only"
    sensor.async_get_last_extra_data = AsyncMock(return_value=extra)
    sensor.async_get_last_state = AsyncMock(return_value=None)
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    assert hass.states.get(sensor.entity_id).state == "1200.0"
    now[0] += timedelta(minutes=30)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    await component.async_remove_entity(sensor.entity_id)


async def test_production_bad_live_candidate_cannot_erase_restored_reading(
    hass, config_entry, coordinator_factory, monkeypatch
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    inputs(coord, now[0], 0)
    entities, component = await add_entities(
        hass, config_entry, coord, (EnphaseCurrentPowerConsumptionSensor,)
    )
    await remove_entities(component, entities)
    await async_mock_restore_state_shutdown_restart(hass)
    now[0] += timedelta(minutes=1)
    coord = coordinator_factory()
    inputs(coord, now[0] - timedelta(hours=1), 1)
    entities, component = await add_entities(
        hass, config_entry, coord, (EnphaseCurrentPowerConsumptionSensor,)
    )
    state = hass.states.get(entities[0].entity_id)
    assert state.state == "2000"
    assert state.attributes["using_cached"] is True
    await remove_entities(component, entities)


async def test_live_balance_wins_when_restore_data_arrives_later(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.sensor_site_energy import (
        _SiteConsumptionPowerRestoreData,
    )

    coord = coordinator_factory()
    inputs(coord, dt_util.utcnow(), 0)
    entities, component = await add_entities(hass, config_entry, coord)
    inputs(coord, dt_util.utcnow(), 1)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    consumption = entities[-1]
    expected = consumption.native_value
    await consumption._async_restore_power_balance(
        _SiteConsumptionPowerRestoreData.from_dict(
            {"balance_power_w": 12, "balance_sample_ts": dt_util.utcnow().timestamp()}
        )
    )
    assert consumption.native_value == expected
    await remove_entities(component, entities)


@pytest.mark.parametrize("cls", CLASSES)
async def test_sensor_restores_older_accepted_record_when_latest_is_corrupt(
    hass, config_entry, coordinator_factory, monkeypatch, cls
):
    from homeassistant.helpers.restore_state import RestoredExtraData

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    coord = coordinator_factory()
    inputs(coord, now[0] - timedelta(minutes=10), 0)
    entities, component = await add_entities(hass, config_entry, coord, (cls,))
    inputs(coord, now[0] - timedelta(minutes=5), 1)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    previous = entities[0].native_value
    inputs(coord, now[0], 2)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    data = entities[0].extra_restore_state_data.as_dict()
    assert len(data["power_history"]["samples"]) == 2
    data["power_history"]["samples"][-1]["sampled_ts"] = now[0].timestamp() + 3600
    await remove_entities(component, entities)
    restored = cls(coordinator_factory())
    restored.entity_id = "sensor.older_record"
    restored.async_get_last_state = AsyncMock(return_value=None)
    restored.async_get_last_extra_data = AsyncMock(return_value=RestoredExtraData(data))
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([restored])
    assert float(hass.states.get(restored.entity_id).state) == previous
    assert (
        restored.extra_state_attributes["sampled_at_utc"]
        == (now[0] - timedelta(minutes=5)).isoformat()
    )
    await component.async_remove_entity(restored.entity_id)


@pytest.mark.parametrize("sample_age,watts", [(300, 2000), (86400, 0)])
async def test_production_retains_original_acquisition_and_overnight_rule(
    hass, config_entry, coordinator_factory, monkeypatch, sample_age, watts
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    received = now[0] - timedelta(seconds=30)
    coord = coordinator_factory()
    coord.current_power_runtime.replace_snapshot(
        w=watts, sample_utc=now[0] - timedelta(seconds=sample_age)
    )
    coord.current_power_runtime.received_utc = received
    entities, component = await add_entities(
        hass, config_entry, coord, (EnphaseCurrentPowerConsumptionSensor,)
    )
    record = entities[0].extra_restore_state_data.as_dict()["power_history"]["samples"][
        -1
    ]
    assert record["received_ts"] == received.timestamp()
    await remove_entities(component, entities)
    await async_mock_restore_state_shutdown_restart(hass)
    now[0] += timedelta(minutes=1)
    entities, component = await add_entities(
        hass,
        config_entry,
        coordinator_factory(),
        (EnphaseCurrentPowerConsumptionSensor,),
    )
    state = hass.states.get(entities[0].entity_id)
    assert float(state.state) == watts
    assert state.attributes["cached_at_utc"] == received.isoformat()
    assert (
        entities[0].extra_restore_state_data.as_dict()["power_history"]["samples"][-1]
        == record
    )
    await remove_entities(component, entities)


async def test_unknown_private_production_format_does_not_resurrect_legacy_watts(
    hass, config_entry, coordinator_factory
):
    from homeassistant.helpers.restore_state import RestoredExtraData

    sensor = EnphaseCurrentPowerConsumptionSensor(coordinator_factory())
    sensor.entity_id = "sensor.unknown_power_format"
    sensor.async_get_last_extra_data = AsyncMock(
        return_value=RestoredExtraData(
            {
                "native_value": 2000,
                "native_unit_of_measurement": "W",
                "power_history": {"version": 2, "samples": []},
            }
        )
    )
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    await component.async_remove_entity(sensor.entity_id)


@pytest.mark.parametrize("cls", [EnphaseGridPowerSensor, EnphaseBatteryPowerSensor])
async def test_valid_legacy_two_sample_history_bridges_first_upgrade_restart(
    hass, config_entry, coordinator_factory, monkeypatch, cls
):
    from homeassistant.core import State

    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    coord = coordinator_factory()
    inputs(coord, now - timedelta(minutes=5), 0)
    original = cls(coord)
    original.native_value
    inputs(coord, now, 1)
    expected = original.native_value
    # Only legacy display attributes and previous-sample data existed before upgrade.
    extra = original.extra_restore_state_data.legacy
    state = State("sensor.legacy_power", str(expected), original.extra_state_attributes)
    restored = cls(coordinator_factory())
    restored.entity_id = state.entity_id
    restored.async_get_last_extra_data = AsyncMock(return_value=extra)
    restored.async_get_last_state = AsyncMock(return_value=state)
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([restored])
    assert float(hass.states.get(restored.entity_id).state) == expected
    assert restored.extra_state_attributes["using_cached"] is True
    assert (
        restored.extra_restore_state_data.as_dict()["power_history"]["samples"][-1][
            "watts"
        ]
        == expected
    )
    await component.async_remove_entity(restored.entity_id)


@pytest.mark.parametrize("watts", [0, 2000])
async def test_production_restore_cannot_extend_a_more_conservative_saved_deadline(
    hass, config_entry, coordinator_factory, monkeypatch, watts
):
    from homeassistant.helpers.restore_state import RestoredExtraData

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    original = now[0] - timedelta(minutes=20)
    record = PowerReading(
        watts,
        (now[0] - timedelta(minutes=5)).timestamp(),
        original.timestamp(),
        {},
        now[0].timestamp(),
    )
    extra = RestoredExtraData(
        {"power_history": {"version": 1, "samples": [record.as_dict()]}}
    )
    sensor = EnphaseCurrentPowerConsumptionSensor(coordinator_factory())
    sensor.entity_id = "sensor.conservative_production"
    sensor.async_get_last_extra_data = AsyncMock(return_value=extra)
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    component._platforms["sensor"].config_entry = config_entry
    await component.async_add_entities([sensor])
    assert float(hass.states.get(sensor.entity_id).state) == watts
    now[0] = original + timedelta(minutes=30)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == "unavailable"
    await component.async_remove_entity(sensor.entity_id)


def restored_power_extra(watts, sampled_at, *, freshness=None, received=None, **attrs):
    from homeassistant.helpers.restore_state import RestoredExtraData

    record = PowerReading(
        watts,
        sampled_at.timestamp(),
        (freshness or sampled_at).timestamp(),
        attrs,
        received.timestamp() if received is not None else None,
    )
    return RestoredExtraData(
        {"power_history": {"version": 1, "samples": [record.as_dict()]}}
    )


@pytest.mark.parametrize("production_w", [0, 2000])
async def test_restored_production_deadline_bounds_published_consumption(
    hass, config_entry, coordinator_factory, monkeypatch, production_w
):
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    accepted = now[0] - timedelta(minutes=20)
    extras = {
        EnphaseCurrentPowerConsumptionSensor: restored_power_extra(
            production_w,
            now[0] - timedelta(minutes=5),
            freshness=accepted,
            received=now[0],
        ),
        EnphaseGridPowerSensor: restored_power_extra(
            1200, now[0], method="lifetime_energy_window"
        ),
        EnphaseBatteryPowerSensor: restored_power_extra(
            -600, now[0], method="lifetime_energy_window"
        ),
    }
    for cls, extra in extras.items():
        monkeypatch.setattr(
            cls, "async_get_last_extra_data", AsyncMock(return_value=extra)
        )
    coord = coordinator_factory()
    entities, component = await add_entities(hass, config_entry, coord)
    consumption = entities[-1]
    assert float(hass.states.get(consumption.entity_id).state) == production_w + 600
    assert consumption.extra_state_attributes["sampled_at_utc"] == accepted.isoformat()
    assert (
        coord.energy.consumption_power_diagnostics["power_balance_source_freshness"][
            "current_production_power"
        ]
        == accepted.isoformat()
    )
    now[0] = accepted + timedelta(minutes=30)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entities[0].entity_id).state == "unavailable"
    assert hass.states.get(consumption.entity_id).state == "unavailable"
    await remove_entities(component, entities)


@pytest.mark.parametrize("cls", [EnphaseGridPowerSensor, EnphaseBatteryPowerSensor])
@pytest.mark.parametrize("with_baseline", [False, True])
async def test_valid_cached_flow_power_can_supply_balance_while_baseline_seeds(
    hass, config_entry, coordinator_factory, monkeypatch, cls, with_baseline
):
    from homeassistant.helpers.restore_state import RestoredExtraData

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    sampled = now[0] - timedelta(minutes=5)
    extras = {
        EnphaseCurrentPowerConsumptionSensor: restored_power_extra(2000, now[0]),
        EnphaseGridPowerSensor: restored_power_extra(
            1200, sampled, method="lifetime_energy_window", last_window_seconds=300
        ),
        EnphaseBatteryPowerSensor: restored_power_extra(
            -600, sampled, method="lifetime_energy_window", last_window_seconds=300
        ),
    }
    if with_baseline:
        data = extras[cls].as_dict()
        data["power_baseline"] = {"method": "source_changed_reseed"}
        extras[cls] = RestoredExtraData(data)
    for sensor_cls, extra in extras.items():
        monkeypatch.setattr(
            sensor_cls, "async_get_last_extra_data", AsyncMock(return_value=extra)
        )
    coord = coordinator_factory()
    inputs(coord, now[0], 0)
    coord.current_power_runtime.clear()
    coord.energy.site_energy.pop("consumption")
    entities, component = await add_entities(hass, config_entry, coord)
    entity = next(entity for entity in entities if type(entity) is cls)
    assert entity._last_method == "seeded"
    assert entity.extra_state_attributes["using_cached"] is True
    assert entity.extra_state_attributes["method"] == "lifetime_energy_window"
    assert entity.extra_state_attributes["sampled_at_utc"] == sampled.isoformat()
    assert (
        entity.extra_state_attributes["freshness_reference_utc"] == sampled.isoformat()
    )
    assert hass.states.get(entities[-1].entity_id).state == "2600"
    now[0] = sampled + timedelta(minutes=30)
    async_fire_time_changed(hass, now[0])
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unavailable"
    assert hass.states.get(entities[-1].entity_id).state == "unavailable"
    # A compatible newer delta supersedes the cached display normally.
    inputs(coord, now[0], 1)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert entity.extra_state_attributes["using_cached"] is False
    assert hass.states.get(entities[-1].entity_id).state != "unavailable"
    await remove_entities(component, entities)


@pytest.mark.parametrize("method", ["power_balance", "consumption_bucket_delta"])
async def test_restored_consumption_diagnostics_match_displayed_reading(
    hass, config_entry, coordinator_factory, monkeypatch, method
):
    now = dt_util.utcnow()
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    sampled = now - timedelta(minutes=1)
    extra = restored_power_extra(1200, sampled, method=method, last_window_seconds=300)
    monkeypatch.setattr(
        EnphaseSiteConsumptionPowerSensor,
        "async_get_last_extra_data",
        AsyncMock(return_value=extra),
    )
    coord = coordinator_factory()
    entities, component = await add_entities(
        hass, config_entry, coord, (EnphaseSiteConsumptionPowerSensor,)
    )
    assert hass.states.get(entities[0].entity_id).state == "1200"
    diagnostics = coord.energy.consumption_power_diagnostics
    assert diagnostics["last_valid_power_w"] == 1200
    assert diagnostics["last_valid_sample_timestamp"] == sampled.timestamp()
    assert diagnostics["last_window_seconds"] == 300
    assert diagnostics["method"] == method
    assert diagnostics["using_cached"] is True
    assert diagnostics["power_balance_blocker"] is not None
    await remove_entities(component, entities)


@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("raw_extra", [["damaged"], "damaged", 17])
async def test_malformed_restore_envelope_does_not_block_live_recovery(
    hass, config_entry, coordinator_factory, monkeypatch, cls, raw_extra
):
    from homeassistant.helpers.restore_state import RestoredExtraData

    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    monkeypatch.setattr(
        cls,
        "async_get_last_extra_data",
        AsyncMock(return_value=RestoredExtraData(raw_extra)),
    )
    coord = coordinator_factory()
    inputs(coord, now[0] - timedelta(minutes=5), 0)
    entities, component = await add_entities(hass, config_entry, coord, (cls,))
    entity = entities[0]
    assert hass.states.get(entity.entity_id) is not None
    inputs(coord, now[0], 1)
    coord.async_update_listeners()
    await hass.async_block_till_done()
    assert (
        float(hass.states.get(entity.entity_id).state)
        == {
            EnphaseCurrentPowerConsumptionSensor: 2100,
            EnphaseGridPowerSensor: 1200,
            EnphaseBatteryPowerSensor: -600,
            EnphaseSiteConsumptionPowerSensor: 1200,
        }[cls]
    )
    await remove_entities(component, entities)
