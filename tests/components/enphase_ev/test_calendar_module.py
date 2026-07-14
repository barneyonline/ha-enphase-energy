from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.enphase_ev import DOMAIN
from custom_components.enphase_ev.calendar import (
    BackupHistoryCalendarEntity,
    SystemEventHistoryCalendarEntity,
    async_setup_entry,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.system_events import SystemEventHistoryEntry


def test_site_has_battery_helper_defaults_and_strict() -> None:
    from custom_components.enphase_ev import calendar as calendar_mod

    coord = SimpleNamespace()
    assert calendar_mod._site_has_battery(coord) is True
    assert calendar_mod._site_has_battery(coord, strict=True) is False

    coord.battery_has_encharge = False
    assert calendar_mod._site_has_battery(coord) is False
    assert calendar_mod._site_has_battery(coord, strict=True) is False

    coord.battery_has_encharge = True
    assert calendar_mod._site_has_battery(coord) is True
    assert calendar_mod._site_has_battery(coord, strict=True) is True


def test_calendar_type_available_uses_inventory_view() -> None:
    from custom_components.enphase_ev import calendar as calendar_mod

    coord = SimpleNamespace(
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "encharge"
        )
    )
    assert calendar_mod._type_available(coord, "encharge") is True
    assert calendar_mod._type_available(coord, "envoy") is False


@pytest.mark.asyncio
async def test_async_setup_entry_adds_backup_history_calendar(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert (
        len([ent for ent in added if isinstance(ent, BackupHistoryCalendarEntity)]) == 1
    )


@pytest.mark.asyncio
async def test_async_setup_entry_does_not_duplicate_backup_history_calendar(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    callbacks: list = []

    def _capture_listener(callback):
        callbacks.append(callback)
        return lambda: None

    coord.async_add_listener = _capture_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)
    assert (
        len([ent for ent in added if isinstance(ent, BackupHistoryCalendarEntity)]) == 1
    )
    assert callbacks

    callbacks[0]()
    assert (
        len([ent for ent in added if isinstance(ent, BackupHistoryCalendarEntity)]) == 1
    )


@pytest.mark.asyncio
async def test_async_setup_entry_waits_for_explicit_battery_detection(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = None  # noqa: SLF001
    callbacks: list = []

    def _capture_listener(callback):
        callbacks.append(callback)
        return lambda: None

    coord.async_add_listener = _capture_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)
    assert not any(isinstance(ent, BackupHistoryCalendarEntity) for ent in added)
    assert callbacks

    coord._battery_has_encharge = True  # noqa: SLF001
    callbacks[0]()
    assert (
        len([ent for ent in added if isinstance(ent, BackupHistoryCalendarEntity)]) == 1
    )


@pytest.mark.asyncio
async def test_async_setup_entry_skips_backup_history_calendar_without_battery(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(ent, BackupHistoryCalendarEntity) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_system_event_history_after_first_success(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = False  # noqa: SLF001
    coord.system_events_runtime._history_last_success_utc = datetime.now(
        timezone.utc
    )  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added: list[object] = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert (
        len(
            [
                entity
                for entity in added
                if isinstance(entity, SystemEventHistoryCalendarEntity)
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_system_event_history_calendar_retains_registry_then_suppresses(
    hass, config_entry, coordinator_factory
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    coord._battery_has_encharge = False  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.system_events_runtime._history_last_success_utc = datetime.now(
        timezone.utc
    )  # noqa: SLF001
    callbacks: list = []
    coord.async_add_listener = lambda callback: (  # type: ignore[method-assign]
        callbacks.append(callback) or (lambda: None)
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    existing = ent_reg.async_get_or_create(
        "calendar",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_system_event_history",
        config_entry=config_entry,
    )
    added: list[object] = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert ent_reg.async_get(existing.entity_id) is not None
    history_entity = next(
        entity
        for entity in added
        if isinstance(entity, SystemEventHistoryCalendarEntity)
    )
    assert history_entity.available is True
    coord._endpoint_family_state("system_event_history").support_state = "suppressed"
    callbacks[0]()
    assert ent_reg.async_get(existing.entity_id) is None
    assert history_entity.available is False


def test_system_event_history_calendar_metadata_and_availability(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    entity = SystemEventHistoryCalendarEntity(coord)

    assert entity.translation_key == "system_event_history"
    assert entity.unique_id == f"{DOMAIN}_site_{coord.site_id}_system_event_history"
    assert entity.available is False
    assert entity.event is None
    assert entity.device_info["identifiers"] == {
        (DOMAIN, f"type:{coord.site_id}:cloud")
    }

    coord.system_events_runtime._history_last_success_utc = datetime.now(
        timezone.utc
    )  # noqa: SLF001
    assert entity.available is True

    coord._endpoint_family_state("system_event_history").support_state = "suppressed"
    assert entity.available is False


def test_system_event_history_calendar_device_info_fallback(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import calendar as calendar_mod

    coord = coordinator_factory()
    monkeypatch.setattr(calendar_mod, "_type_device_info", lambda *_args: None)

    assert SystemEventHistoryCalendarEntity(coord).device_info == {
        "identifiers": {(DOMAIN, f"type:{coord.site_id}:cloud")},
        "manufacturer": "Enphase",
    }


@pytest.mark.asyncio
async def test_system_event_history_calendar_get_events_and_naive_range(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    entry = SystemEventHistoryEntry(
        fingerprint="safe",
        summary="Charging started",
        description="No action is required.",
        start=datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, 8, 1, tzinfo=timezone.utc),
    )
    coord.system_events_runtime.async_history_events = AsyncMock(return_value=(entry,))
    entity = SystemEventHistoryCalendarEntity(coord)

    events = await entity.async_get_events(
        None,
        datetime(2026, 2, 1, 0, 0),
        datetime(2026, 2, 2, 0, 0),
    )

    assert len(events) == 1
    assert events[0].summary == "Charging started"
    assert events[0].description == "No action is required."
    assert events[0].start == entry.start
    assert events[0].end == entry.end
    coord.system_events_runtime.async_history_events.assert_awaited_once()

    assert (
        await entity.async_get_events(
            None,
            datetime(2026, 2, 2, tzinfo=timezone.utc),
            datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        == []
    )


def test_system_event_history_calendar_summary_fallbacks(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    entity = SystemEventHistoryCalendarEntity(coord)
    entry = SystemEventHistoryEntry(
        fingerprint="safe",
        summary=None,
        description=None,
        start=datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, 8, 1, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        SystemEventHistoryCalendarEntity,
        "name",
        property(lambda self: " System Event History "),
    )
    assert (
        entity._to_calendar_event(entry).summary == "System Event History"
    )  # noqa: SLF001

    monkeypatch.setattr(
        SystemEventHistoryCalendarEntity,
        "name",
        property(lambda self: "   "),
    )
    monkeypatch.setattr(
        SystemEventHistoryCalendarEntity,
        "entity_id",
        property(lambda self: " calendar.system_event_history "),
        raising=False,
    )
    assert (  # noqa: SLF001
        entity._to_calendar_event(entry).summary == "calendar.system_event_history"
    )

    def _raise_name(_self):
        raise RuntimeError

    monkeypatch.setattr(
        SystemEventHistoryCalendarEntity,
        "name",
        property(_raise_name),
    )
    monkeypatch.setattr(
        SystemEventHistoryCalendarEntity,
        "entity_id",
        property(lambda self: None),
        raising=False,
    )
    assert entity._to_calendar_event(entry).summary == entity.unique_id  # noqa: SLF001


def test_backup_history_calendar_available_gating(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.last_update_success = True
    coord._battery_has_encharge = True  # noqa: SLF001
    entity = BackupHistoryCalendarEntity(coord)

    assert entity.available is True

    coord.last_update_success = False
    assert entity.available is False

    coord.last_update_success = True
    coord.inventory_view.has_type_for_entities = lambda _type_key: False
    assert entity.available is False

    coord.inventory_view.has_type_for_entities = lambda _type_key: True
    coord._battery_has_encharge = False  # noqa: SLF001
    assert entity.available is False


def test_backup_history_calendar_device_info_uses_encharge(coordinator_factory) -> None:
    coord = coordinator_factory()
    entity = BackupHistoryCalendarEntity(coord)

    info = entity.device_info
    assert (DOMAIN, f"type:{coord.site_id}:encharge") in info["identifiers"]


def test_backup_history_calendar_device_info_fallback(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.inventory_view.type_device_info = lambda _type_key: None
    entity = BackupHistoryCalendarEntity(coord)

    info = entity.device_info
    assert info["manufacturer"] == "Enphase"
    assert (DOMAIN, f"type:{coord.site_id}:encharge") in info["identifiers"]


def test_backup_history_calendar_iter_history_events_filters_invalid_rows(
    coordinator_factory,
    monkeypatch,
) -> None:
    coord = coordinator_factory()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        type(coord),
        "battery_backup_history_events",
        property(
            lambda self: [  # noqa: ARG005
                "bad-row",
                {
                    "start": "bad",
                    "end": now + timedelta(minutes=1),
                    "duration_seconds": 60,
                },
                {
                    "start": datetime(2026, 2, 1, 10, 0),
                    "end": now,
                    "duration_seconds": 60,
                },
                {
                    "start": now + timedelta(minutes=5),
                    "end": now + timedelta(minutes=4),
                    "duration_seconds": 60,
                },
                {
                    "start": now + timedelta(minutes=1),
                    "end": now + timedelta(minutes=2),
                    "duration_seconds": 60,
                },
            ]
        ),
    )
    entity = BackupHistoryCalendarEntity(coord)

    events = entity._iter_history_events()  # noqa: SLF001
    assert len(events) == 1
    assert events[0][0] == now + timedelta(minutes=1)
    assert events[0][1] == now + timedelta(minutes=2)


def test_backup_history_calendar_to_calendar_event_summary_prefers_name(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    entity = BackupHistoryCalendarEntity(coord)
    monkeypatch.setattr(
        BackupHistoryCalendarEntity,
        "name",
        property(lambda self: " Backup History "),
    )

    event = entity._to_calendar_event(  # noqa: SLF001
        datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 2, 1, 8, 1, tzinfo=timezone.utc),
    )
    assert event.summary == "Backup History"


def test_backup_history_calendar_to_calendar_event_summary_uses_entity_id(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    entity = BackupHistoryCalendarEntity(coord)
    monkeypatch.setattr(
        BackupHistoryCalendarEntity,
        "name",
        property(lambda self: "   "),
    )
    monkeypatch.setattr(
        BackupHistoryCalendarEntity,
        "entity_id",
        property(lambda self: " calendar.backup_history "),
        raising=False,
    )

    event = entity._to_calendar_event(  # noqa: SLF001
        datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 2, 1, 8, 1, tzinfo=timezone.utc),
    )
    assert event.summary == "calendar.backup_history"


def test_backup_history_calendar_event_current_next_none(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.last_update_success = True
    now = datetime.now(timezone.utc)
    coord._battery_backup_history_events = [  # noqa: SLF001
        {
            "start": now - timedelta(minutes=3),
            "end": now - timedelta(minutes=1),
            "duration_seconds": 120,
        },
        {
            "start": now - timedelta(minutes=1),
            "end": now + timedelta(minutes=1),
            "duration_seconds": 120,
        },
        {
            "start": now + timedelta(minutes=3),
            "end": now + timedelta(minutes=4),
            "duration_seconds": 60,
        },
    ]
    entity = BackupHistoryCalendarEntity(coord)

    current = entity.event
    assert current is not None
    assert current.start <= now <= current.end

    coord._battery_backup_history_events = [  # noqa: SLF001
        {
            "start": now + timedelta(minutes=2),
            "end": now + timedelta(minutes=4),
            "duration_seconds": 120,
        }
    ]
    upcoming = entity.event
    assert upcoming is not None
    assert upcoming.start > now

    coord._battery_backup_history_events = []  # noqa: SLF001
    assert entity.event is None


@pytest.mark.asyncio
async def test_backup_history_calendar_get_events_range_filter(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.last_update_success = True
    start = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)
    coord._battery_backup_history_events = [  # noqa: SLF001
        {
            "start": datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 2, 1, 8, 2, tzinfo=timezone.utc),
            "duration_seconds": 120,
        },
        {
            "start": datetime(2026, 2, 2, 8, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 2, 2, 8, 1, tzinfo=timezone.utc),
            "duration_seconds": 60,
        },
        {
            "start": datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 2, 10, 8, 1, tzinfo=timezone.utc),
            "duration_seconds": 60,
        },
    ]
    entity = BackupHistoryCalendarEntity(coord)

    events = await entity.async_get_events(
        None, start, datetime(2026, 2, 3, 0, 0, tzinfo=timezone.utc)
    )
    assert len(events) == 2
    assert all(
        event.start < datetime(2026, 2, 3, 0, 0, tzinfo=timezone.utc)
        for event in events
    )


@pytest.mark.asyncio
async def test_backup_history_calendar_get_events_accepts_naive_range(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.last_update_success = True
    coord._battery_backup_history_events = [  # noqa: SLF001
        {
            "start": datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 2, 1, 8, 2, tzinfo=timezone.utc),
            "duration_seconds": 120,
        }
    ]
    entity = BackupHistoryCalendarEntity(coord)

    events = await entity.async_get_events(
        None,
        datetime(2026, 2, 1, 0, 0),
        datetime(2026, 2, 2, 0, 0),
    )
    assert len(events) == 1
