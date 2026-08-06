"""Tests for VPP calendar and next-event sensors."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.util import dt as dt_util
from homeassistant.helpers import entity_registry as er

from custom_components.enphase_ev import calendar as calendar_module
from custom_components.enphase_ev import sensor_vpp as sensor_vpp_module
from custom_components.enphase_ev.calendar import (
    VppEventsCalendarEntity,
    async_setup_entry as async_setup_calendar,
)
from custom_components.enphase_ev.const import DOMAIN, OPT_VPP_EVENTS_ENABLED
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.sensor import async_setup_entry as async_setup_sensors
from custom_components.enphase_ev.sensor_vpp import (
    VPP_SENSOR_KEYS,
    EnphaseVppNextEventEndSensor,
    EnphaseVppNextEventStartSensor,
    EnphaseVppNextEventStatusSensor,
    EnphaseVppNextEventSubtypeSensor,
    EnphaseVppNextEventTypeSensor,
)
from custom_components.enphase_ev.vpp_runtime import VppEvent


def _enable_with_events(coord, events: tuple[VppEvent, ...]) -> None:
    if coord.config_entry is None:
        coord.config_entry = SimpleNamespace(options={OPT_VPP_EVENTS_ENABLED: True})
    runtime = coord.vpp_runtime
    runtime._enrollment_state = "enrolled"  # noqa: SLF001
    runtime._events = events  # noqa: SLF001
    runtime._events_last_success_mono = time.monotonic()  # noqa: SLF001
    runtime._events_last_success_utc = datetime.now(UTC)  # noqa: SLF001


def _events() -> tuple[VppEvent, ...]:
    now = datetime.now(UTC)
    return (
        VppEvent(
            fingerprint="terminal",
            start=now + timedelta(minutes=10),
            end=now + timedelta(minutes=20),
            event_type="battery_charge",
            subtype="Charge_From_PV_Grid",
            status="cancelled",
            cancelled=True,
        ),
        VppEvent(
            fingerprint="next",
            start=now + timedelta(minutes=30),
            end=now + timedelta(hours=2),
            event_type="battery_discharge",
            subtype="Discharge_To_Load_Grid",
            status="scheduled",
        ),
        VppEvent(
            fingerprint="history",
            start=now - timedelta(hours=2),
            end=now - timedelta(hours=1),
            event_type="idle",
            subtype="Idle",
            status="completed",
        ),
    )


def test_vpp_calendar_exposes_all_records_and_next_actionable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    events = _events()
    _enable_with_events(coord, events)
    entity = VppEventsCalendarEntity(coord)

    assert entity.available is True
    assert entity.unique_id == f"{DOMAIN}_site_{coord.site_id}_vpp_events"
    assert entity.translation_key == "vpp_events"
    assert entity.event is not None
    assert entity.event.start == events[1].start
    assert entity.event.summary == "Discharge_To_Load_Grid (scheduled)"
    assert entity.event.description == (
        "type=battery_discharge\n" "subtype=Discharge_To_Load_Grid\n" "status=scheduled"
    )
    assert entity.device_info["identifiers"] == {
        (DOMAIN, f"type:{coord.site_id}:cloud")
    }


@pytest.mark.asyncio
async def test_vpp_calendar_range_includes_terminal_statuses_and_naive_dates(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    events = _events()
    _enable_with_events(coord, events)
    entity = VppEventsCalendarEntity(coord)

    result = await entity.async_get_events(
        None,
        (events[2].start - timedelta(minutes=1))
        .astimezone(dt_util.DEFAULT_TIME_ZONE)
        .replace(tzinfo=None),
        (events[1].end + timedelta(minutes=1))
        .astimezone(dt_util.DEFAULT_TIME_ZONE)
        .replace(tzinfo=None),
    )

    assert {item.summary for item in result} == {
        "Charge_From_PV_Grid (cancelled)",
        "Discharge_To_Load_Grid (scheduled)",
        "Idle (completed)",
    }
    assert await entity.async_get_events(None, events[1].end, events[1].start) == []


def test_vpp_sensors_share_next_actionable_event(coordinator_factory) -> None:
    coord = coordinator_factory()
    events = _events()
    _enable_with_events(coord, events)
    sensors = [
        EnphaseVppNextEventStartSensor(coord),
        EnphaseVppNextEventEndSensor(coord),
        EnphaseVppNextEventTypeSensor(coord),
        EnphaseVppNextEventSubtypeSensor(coord),
        EnphaseVppNextEventStatusSensor(coord),
    ]

    assert [sensor.native_value for sensor in sensors] == [
        events[1].start,
        events[1].end,
        "battery_discharge",
        "Discharge_To_Load_Grid",
        "scheduled",
    ]
    assert all(sensor.available for sensor in sensors)
    assert all(
        sensor.device_info["identifiers"] == {(DOMAIN, f"type:{coord.site_id}:cloud")}
        for sensor in sensors
    )

    coord.vpp_runtime._events = ()  # noqa: SLF001
    assert [sensor.native_value for sensor in sensors] == [None] * 5
    assert all(sensor.available for sensor in sensors)


def test_vpp_entities_fall_back_to_cloud_device_info(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    _enable_with_events(coord, _events())
    monkeypatch.setattr(calendar_module, "_type_device_info", lambda *_args: None)
    monkeypatch.setattr(
        sensor_vpp_module,
        "inventory_type_device_info",
        lambda *_args: None,
    )

    calendar = VppEventsCalendarEntity(coord)
    sensor = EnphaseVppNextEventStartSensor(coord)

    assert calendar.device_info["model"] == "Cloud Service"
    assert sensor.device_info["identifiers"] == {
        (DOMAIN, f"type:{coord.site_id}:cloud")
    }


@pytest.mark.asyncio
async def test_vpp_entities_are_discovered_only_when_enabled_and_supported(
    hass,
    config_entry,
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.config_entry = config_entry
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, OPT_VPP_EVENTS_ENABLED: True},
    )
    _enable_with_events(coord, _events())
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    calendars: list[Any] = []
    sensors: list[Any] = []

    await async_setup_calendar(
        hass,
        config_entry,
        lambda entities, update_before_add=False: calendars.extend(entities),
    )
    await async_setup_sensors(
        hass,
        config_entry,
        lambda entities, update_before_add=False: sensors.extend(entities),
    )

    assert (
        len([item for item in calendars if isinstance(item, VppEventsCalendarEntity)])
        == 1
    )
    expected_sensor_ids = {
        f"{DOMAIN}_site_{coord.site_id}_{key}" for key in VPP_SENSOR_KEYS
    }
    assert {
        item.unique_id for item in sensors
    } & expected_sensor_ids == expected_sensor_ids

    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, OPT_VPP_EVENTS_ENABLED: False},
    )
    coord.vpp_runtime.clear()
    assert VppEventsCalendarEntity(coord).available is False


@pytest.mark.asyncio
async def test_disabled_vpp_removes_registered_calendar(
    hass,
    config_entry,
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.config_entry = config_entry
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    registry = er.async_get(hass)
    unique_id = f"{DOMAIN}_site_{coord.site_id}_vpp_events"
    registered = registry.async_get_or_create(
        "calendar",
        DOMAIN,
        unique_id,
        config_entry=config_entry,
    )

    await async_setup_calendar(
        hass,
        config_entry,
        lambda _entities, update_before_add=False: None,
    )

    assert registry.async_get(registered.entity_id) is None
