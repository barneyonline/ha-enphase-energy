from __future__ import annotations

from datetime import datetime, time as dt_time, timezone
from types import MappingProxyType
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.helpers.entity import EntityCategory
from homeassistant.exceptions import ServiceValidationError

from custom_components.enphase_ev.button import (
    EvseScheduleDeleteButton,
    EvseScheduleRefreshButton,
    EvseScheduleSaveButton,
)
from custom_components.enphase_ev.const import OPT_SCHEDULE_SYNC_ENABLED
from custom_components.enphase_ev.evse_schedule_editor import (
    DAY_ORDER,
    EvseScheduleEditorEntity,
    EvseScheduleEditorManager,
    NEW_SCHEDULE_OPTION,
    _slot_limit,
    _normalize_days,
    _time_to_text,
    days_list_from_editor,
    editor_days_from_list,
    evse_schedule_create_label,
    evse_schedule_editor_active,
    evse_schedule_inventory,
    evse_schedule_option_label,
    evse_scheduler_enabled,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.schedule_sync import ScheduleSync
from custom_components.enphase_ev.select import EvseScheduleSelect
from custom_components.enphase_ev.switch import EvseScheduleEditorDaySwitch
from custom_components.enphase_ev.time import (
    EvseScheduleEditEndTimeEntity,
    EvseScheduleEditStartTimeEntity,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _slot_cache() -> dict[str, dict[str, dict[str, object]]]:
    return {
        RANDOM_SERIAL: {
            "slot-1": {
                "id": "slot-1",
                "startTime": "08:00:00",
                "endTime": "09:30",
                "days": [1, 3, 5, 5, 9],
                "scheduleType": "CUSTOM",
                "enabled": True,
                "chargingLevelAmp": 24,
            },
            "slot-2": {
                "id": "slot-2",
                "startTime": dt_time(10, 0),
                "endTime": 690,
                "days": [2, 4],
                "scheduleType": "custom",
                "enabled": False,
                "chargingLevel": "18",
            },
            "slot-off-peak": {
                "id": "slot-off-peak",
                "startTime": None,
                "endTime": None,
                "days": [1],
                "scheduleType": "OFF_PEAK",
                "enabled": False,
            },
            "slot-bad": {
                "id": "slot-bad",
                "startTime": None,
                "endTime": "12:00",
                "days": "bad",
                "scheduleType": "CUSTOM",
            },
            "slot-empty-id": {
                "id": "",
                "startTime": "13:00",
                "endTime": "14:00",
                "days": [1],
                "scheduleType": "CUSTOM",
            },
            "slot-not-dict": "bad",
        }
    }


def _prepare_evse_schedule_coord(coord) -> dict[str, dict[str, dict[str, object]]]:
    slot_cache = _slot_cache()
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.last_success_utc = datetime.now(timezone.utc)
    coord.last_update_success = True
    coord.data = {
        RANDOM_SERIAL: {
            "display_name": "Garage Charger",
            "name": "Garage EV",
            "min_amp": "6",
            "max_amp": "40",
            "charging_level": "20",
        }
    }
    coord.iter_serials = lambda: [RANDOM_SERIAL]
    coord.pick_start_amps = lambda _sn: 28
    coord.client.get_schedules = AsyncMock()
    coord.client.patch_schedule = AsyncMock()
    coord.client.create_schedule = AsyncMock()
    coord.client.delete_schedule = AsyncMock()
    schedule_sync = SimpleNamespace(
        _slot_cache=slot_cache,
        async_refresh=AsyncMock(),
        async_upsert_slot=AsyncMock(),
        async_delete_slot=AsyncMock(),
    )
    schedule_sync.get_slot = lambda sn, slot_id: schedule_sync._slot_cache.get(
        sn, {}
    ).get(slot_id)
    coord.schedule_sync = schedule_sync
    return slot_cache


def _attach_editor_runtime(config_entry, coord) -> EvseScheduleEditorManager:
    editor = EvseScheduleEditorManager(coord)
    editor.sync_from_coordinator()
    config_entry.__dict__["options"] = MappingProxyType(
        {**config_entry.options, OPT_SCHEDULE_SYNC_ENABLED: True}
    )
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        evse_schedule_editor=editor,
    )
    return editor


def test_evse_schedule_inventory_normalizes_payload_and_helpers(config_entry) -> None:
    coord = SimpleNamespace(
        site_id="site-123",
        client=SimpleNamespace(
            get_schedules=AsyncMock(),
            patch_schedule=AsyncMock(),
            create_schedule=AsyncMock(),
            delete_schedule=AsyncMock(),
        ),
        schedule_sync=SimpleNamespace(_slot_cache=_slot_cache()),
        data={RANDOM_SERIAL: {"charging_level": "20"}},
        pick_start_amps=lambda _sn: 28,
    )

    records = evse_schedule_inventory(coord, RANDOM_SERIAL)

    assert _time_to_text(dt_time(2, 15)) == "02:15"
    assert _time_to_text(None, default="09:00") == "09:00"
    assert _time_to_text(75) == "01:15"
    assert _time_to_text("09:45:00") == "09:45"
    assert _time_to_text("bad") == "00:00"
    assert _normalize_days("bad") == []
    assert _normalize_days([3, "4", 4, 8, "bad"]) == [3, 4]
    assert editor_days_from_list([1, 3, 7]) == {
        "mon": True,
        "tue": False,
        "wed": True,
        "thu": False,
        "fri": False,
        "sat": False,
        "sun": True,
    }
    assert days_list_from_editor({"mon": True, "wed": True, "sun": True}) == [1, 3, 7]
    assert [day_key for day_key, _ in DAY_ORDER] == [
        "mon",
        "tue",
        "wed",
        "thu",
        "fri",
        "sat",
        "sun",
    ]

    assert [record.slot_id for record in records] == ["slot-1", "slot-2"]
    assert records[0].start_time == "08:00"
    assert records[0].end_time == "09:30"
    assert records[0].days == [1, 3, 5]
    assert records[1].start_time == "10:00"
    assert records[1].end_time == "11:30"
    assert records[1].limit == 18
    assert evse_schedule_option_label(records[0]) == "08:00-09:30 (24 A)"

    config_entry.__dict__["options"] = MappingProxyType({})
    assert evse_scheduler_enabled(config_entry) is True
    config_entry.__dict__["options"] = MappingProxyType(
        {OPT_SCHEDULE_SYNC_ENABLED: True}
    )
    assert evse_scheduler_enabled(config_entry) is True
    assert evse_scheduler_enabled(None) is False
    assert evse_schedule_editor_active(coord, config_entry) is True
    coord.client.create_schedule = None
    assert evse_schedule_editor_active(coord, config_entry) is False
    assert (
        _slot_limit({"chargingLevelAmp": True, "chargingLevel": "bad"}, default=21)
        == 21
    )


def test_evse_schedule_editor_default_limit_and_inventory_edge_paths() -> None:
    class BadData:
        def get(self, _sn):
            raise RuntimeError("boom")

    coord = SimpleNamespace(
        site_id="site-123",
        client=SimpleNamespace(),
        schedule_sync=SimpleNamespace(_slot_cache={RANDOM_SERIAL: {}}),
        data=BadData(),
        pick_start_amps=lambda _sn: (_ for _ in ()).throw(RuntimeError("boom")),
        iter_serials=lambda: [RANDOM_SERIAL],
    )
    manager = EvseScheduleEditorManager(coord)

    manager.sync_from_coordinator()
    assert manager.form_state(RANDOM_SERIAL).limit == 32
    assert manager.get_schedule(RANDOM_SERIAL, None) is None

    coord.schedule_sync._slot_cache = {}
    assert evse_schedule_inventory(coord, RANDOM_SERIAL) == []

    coord.schedule_sync = None
    assert evse_schedule_inventory(coord, RANDOM_SERIAL) == []

    assert evse_schedule_create_label() == "Create new schedule"


def test_evse_schedule_editor_manager_syncs_and_promotes_selection() -> None:
    coord = SimpleNamespace(
        site_id="site-123",
        client=SimpleNamespace(),
        schedule_sync=SimpleNamespace(_slot_cache=_slot_cache()),
        data={RANDOM_SERIAL: {"charging_level": 20}},
        pick_start_amps=lambda _sn: 28,
        iter_serials=lambda: [RANDOM_SERIAL],
    )
    manager = EvseScheduleEditorManager(coord)
    notifications: list[str] = []
    unsub = manager.async_add_listener(lambda: notifications.append("updated"))

    manager.sync_from_coordinator()
    assert [item["slot_id"] for item in manager.as_dicts(RANDOM_SERIAL)] == [
        "slot-1",
        "slot-2",
    ]
    assert manager.current_selection(RANDOM_SERIAL) == "slot-1"
    assert manager.form_state(RANDOM_SERIAL).limit == 24

    manager.select_schedule(RANDOM_SERIAL, "slot-2")
    assert manager.form_state(RANDOM_SERIAL).selected_slot_id == "slot-2"
    assert manager.form_state(RANDOM_SERIAL).limit == 18

    manager.set_edit_time(RANDOM_SERIAL, "start_time", dt_time(7, 45))
    manager.set_edit_time(RANDOM_SERIAL, "end_time", dt_time(9, 15))
    manager.set_edit_limit(RANDOM_SERIAL, 30)
    manager.set_edit_day(RANDOM_SERIAL, "mon", False)
    assert manager.form_state(RANDOM_SERIAL).start_time == "07:45"
    assert manager.form_state(RANDOM_SERIAL).end_time == "09:15"
    assert manager.form_state(RANDOM_SERIAL).limit == 30
    assert manager.form_state(RANDOM_SERIAL).days["mon"] is False

    manager.select_schedule(RANDOM_SERIAL, NEW_SCHEDULE_OPTION)
    assert manager.is_creating(RANDOM_SERIAL) is True
    assert manager.current_selection(RANDOM_SERIAL) == NEW_SCHEDULE_OPTION
    manager.set_edit_time(RANDOM_SERIAL, "start_time", dt_time(6, 0))
    manager.set_edit_time(RANDOM_SERIAL, "end_time", dt_time(7, 0))
    manager.set_edit_limit(RANDOM_SERIAL, 16)
    manager.set_edit_day(RANDOM_SERIAL, "tue", True)
    coord.schedule_sync._slot_cache[RANDOM_SERIAL]["slot-new"] = {
        "id": "slot-new",
        "startTime": "06:00",
        "endTime": "07:00",
        "days": [2],
        "scheduleType": "CUSTOM",
        "enabled": True,
        "chargingLevelAmp": 32,
    }
    manager.sync_from_coordinator()
    assert manager.current_selection(RANDOM_SERIAL) == "slot-new"
    assert manager.is_creating(RANDOM_SERIAL) is False

    coord.schedule_sync._slot_cache[RANDOM_SERIAL]["slot-dup-a"] = {
        "id": "abc123456",
        "startTime": "12:00",
        "endTime": "13:00",
        "days": [1],
        "scheduleType": "CUSTOM",
        "enabled": True,
        "chargingLevelAmp": 10,
    }
    coord.schedule_sync._slot_cache[RANDOM_SERIAL]["slot-dup-b"] = {
        "id": "zzz123456",
        "startTime": "12:00",
        "endTime": "13:00",
        "days": [1],
        "scheduleType": "CUSTOM",
        "enabled": True,
        "chargingLevelAmp": 10,
    }
    manager.sync_from_coordinator()
    labels = manager.option_label_by_slot_id(RANDOM_SERIAL)
    assert labels["abc123456"].endswith("[123456]")
    assert labels["zzz123456"].endswith("[123456]")
    assert manager.slot_id_for_option_label(RANDOM_SERIAL, "missing") is None
    assert manager.get_schedule(RANDOM_SERIAL, "missing") is None

    unsub()
    assert notifications


def test_evse_schedule_editor_manager_auto_create_and_reset_paths() -> None:
    coord = SimpleNamespace(
        site_id="site-123",
        client=SimpleNamespace(),
        schedule_sync=SimpleNamespace(_slot_cache={RANDOM_SERIAL: {}}),
        data={
            RANDOM_SERIAL: {"charging_level": True, "max_amp": "bad", "min_amp": "bad"}
        },
        pick_start_amps=lambda _sn: 28,
        iter_serials=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    manager = EvseScheduleEditorManager(coord)

    manager.sync_from_coordinator()
    assert manager.is_creating(RANDOM_SERIAL) is True
    assert manager.current_selection(RANDOM_SERIAL) == NEW_SCHEDULE_OPTION
    assert manager.form_state(RANDOM_SERIAL).limit == 28

    coord.schedule_sync._slot_cache = _slot_cache()
    manager.sync_from_coordinator()
    assert manager.current_selection(RANDOM_SERIAL) == "slot-1"

    manager.select_schedule(RANDOM_SERIAL, NEW_SCHEDULE_OPTION)
    manager._auto_create_mode[RANDOM_SERIAL] = True  # noqa: SLF001
    manager.sync_from_coordinator()
    assert manager.current_selection(RANDOM_SERIAL) == "slot-1"

    previous = manager.form_state(RANDOM_SERIAL).start_time
    manager.sync_from_coordinator()
    assert manager.form_state(RANDOM_SERIAL).start_time == previous

    manager.select_schedule(RANDOM_SERIAL, "missing")
    assert manager.form_state(RANDOM_SERIAL).selected_slot_id is None
    assert manager.form_state(RANDOM_SERIAL).limit == 28


def test_evse_schedule_editor_build_slot_payload_preserves_existing_fields() -> None:
    coord = SimpleNamespace(
        site_id="site-123",
        client=SimpleNamespace(),
        schedule_sync=SimpleNamespace(_slot_cache=_slot_cache()),
        data={RANDOM_SERIAL: {"charging_level": 20}},
        pick_start_amps=lambda _sn: 28,
        iter_serials=lambda: [RANDOM_SERIAL],
    )
    coord.schedule_sync.get_slot = lambda sn, slot_id: coord.schedule_sync._slot_cache[
        sn
    ].get(slot_id)
    manager = EvseScheduleEditorManager(coord)
    manager.sync_from_coordinator()

    manager.select_schedule(RANDOM_SERIAL, "slot-1")
    manager.set_edit_limit(RANDOM_SERIAL, 26)
    existing = manager.build_slot_payload(RANDOM_SERIAL)
    assert existing["id"] == "slot-1"
    assert existing["chargingLevelAmp"] == 26
    assert existing["remindFlag"] is False
    assert existing["sourceType"] == "SYSTEM"

    manager.select_schedule(RANDOM_SERIAL, NEW_SCHEDULE_OPTION)
    manager.set_edit_time(RANDOM_SERIAL, "start_time", dt_time(5, 0))
    manager.set_edit_time(RANDOM_SERIAL, "end_time", dt_time(6, 30))
    manager.set_edit_limit(RANDOM_SERIAL, 12)
    manager.set_edit_day(RANDOM_SERIAL, "fri", True)
    created = manager.build_slot_payload(RANDOM_SERIAL)
    assert str(created["id"]).startswith("site-123:")
    assert created["startTime"] == "05:00"
    assert created["endTime"] == "06:30"
    assert created["days"] == [5]
    assert created["enabled"] is True


@pytest.mark.asyncio
async def test_evse_schedule_editor_entity_listens_for_manager_updates(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    _prepare_evse_schedule_coord(coord)
    editor = _attach_editor_runtime(config_entry, coord)

    async def _noop_async_added(self) -> None:
        return None

    async def _noop_async_removed(self) -> None:
        return None

    monkeypatch.setattr(
        "custom_components.enphase_ev.entity.CoordinatorEntity.async_added_to_hass",
        _noop_async_added,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.entity.CoordinatorEntity.async_will_remove_from_hass",
        _noop_async_removed,
    )

    class DummyEntity(EvseScheduleEditorEntity):
        pass

    entity = DummyEntity(coord, config_entry, RANDOM_SERIAL)
    entity.hass = hass
    entity.async_write_ha_state = Mock()

    await entity.async_added_to_hass()
    editor.set_edit_limit(RANDOM_SERIAL, 22)
    entity.async_write_ha_state.assert_called_once()

    await entity.async_will_remove_from_hass()
    entity.async_write_ha_state.reset_mock()
    editor.set_edit_limit(RANDOM_SERIAL, 23)
    entity.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_evse_schedule_platform_setup_adds_editor_entities(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import button, number, select, switch, time

    coord = coordinator_factory()
    _prepare_evse_schedule_coord(coord)
    callbacks: list[object] = []
    monkeypatch.setattr(
        coord,
        "async_add_listener",
        lambda callback: callbacks.append(callback) or (lambda: None),
    )
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: callbacks.append(callback) or (lambda: None),
        raising=False,
    )
    _attach_editor_runtime(config_entry, coord)

    added: list[object] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await select.async_setup_entry(hass, config_entry, _capture)
    await time.async_setup_entry(hass, config_entry, _capture)
    await number.async_setup_entry(hass, config_entry, _capture)
    await switch.async_setup_entry(hass, config_entry, _capture)
    await button.async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, EvseScheduleSelect) for entity in added)
    assert any(isinstance(entity, EvseScheduleEditStartTimeEntity) for entity in added)
    assert any(isinstance(entity, EvseScheduleEditEndTimeEntity) for entity in added)
    assert any(isinstance(entity, EvseScheduleEditorDaySwitch) for entity in added)
    assert any(isinstance(entity, EvseScheduleRefreshButton) for entity in added)
    assert any(isinstance(entity, EvseScheduleSaveButton) for entity in added)
    assert any(isinstance(entity, EvseScheduleDeleteButton) for entity in added)
    editor_entities = [
        entity
        for entity in added
        if isinstance(
            entity,
            (
                EvseScheduleSelect,
                EvseScheduleEditStartTimeEntity,
                EvseScheduleEditEndTimeEntity,
                EvseScheduleEditorDaySwitch,
                EvseScheduleRefreshButton,
                EvseScheduleSaveButton,
                EvseScheduleDeleteButton,
            ),
        )
    ]
    assert all(
        getattr(entity, "entity_category", None) is EntityCategory.CONFIG
        for entity in editor_entities
    )
    assert all(
        entity.entity_registry_enabled_default is True
        for entity in added
        if isinstance(entity, EvseScheduleEditorDaySwitch)
    )
    assert callbacks


@pytest.mark.asyncio
async def test_evse_schedule_platform_setup_skips_editor_when_unavailable(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import button, number, select, switch, time

    coord = coordinator_factory()
    _prepare_evse_schedule_coord(coord)
    coord.client.create_schedule = None
    monkeypatch.setattr(
        coord, "async_add_listener", lambda callback: (lambda: None), raising=False
    )
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    config_entry.__dict__["options"] = MappingProxyType(
        {OPT_SCHEDULE_SYNC_ENABLED: False}
    )
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        evse_schedule_editor=EvseScheduleEditorManager(coord),
    )

    added: list[object] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await select.async_setup_entry(hass, config_entry, _capture)
    await time.async_setup_entry(hass, config_entry, _capture)
    await number.async_setup_entry(hass, config_entry, _capture)
    await switch.async_setup_entry(hass, config_entry, _capture)
    await button.async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, EvseScheduleSelect) for entity in added)
    assert not any(
        isinstance(entity, EvseScheduleEditStartTimeEntity) for entity in added
    )
    assert not any(isinstance(entity, EvseScheduleEditorDaySwitch) for entity in added)
    assert not any(isinstance(entity, EvseScheduleRefreshButton) for entity in added)


@pytest.mark.asyncio
async def test_evse_schedule_editor_entities_update_state_and_call_schedule_sync(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _prepare_evse_schedule_coord(coord)
    coord.schedule_sync._slot_cache[RANDOM_SERIAL].pop("slot-2")
    editor = _attach_editor_runtime(config_entry, coord)
    schedule_select = EvseScheduleSelect(coord, config_entry, RANDOM_SERIAL)
    current_label = evse_schedule_option_label(
        editor.get_schedule(RANDOM_SERIAL, "slot-1")
    )
    assert schedule_select.options == [
        current_label,
        evse_schedule_create_label(),
    ]
    assert schedule_select.current_option == current_label

    await schedule_select.async_select_option(evse_schedule_create_label())
    assert editor.is_creating(RANDOM_SERIAL) is True
    assert schedule_select.current_option == evse_schedule_create_label()

    start = EvseScheduleEditStartTimeEntity(coord, config_entry, RANDOM_SERIAL)
    end = EvseScheduleEditEndTimeEntity(coord, config_entry, RANDOM_SERIAL)
    day = EvseScheduleEditorDaySwitch(coord, config_entry, RANDOM_SERIAL, "mon")
    refresh = EvseScheduleRefreshButton(coord, config_entry, RANDOM_SERIAL)
    save = EvseScheduleSaveButton(coord, config_entry, RANDOM_SERIAL)
    delete = EvseScheduleDeleteButton(coord, config_entry, RANDOM_SERIAL)

    await start.async_set_value(dt_time(6, 0))
    await end.async_set_value(dt_time(7, 0))
    await day.async_turn_on()
    assert start.native_value == dt_time(6, 0)
    assert end.native_value == dt_time(7, 0)
    assert day.is_on is True
    assert save.available is True

    await refresh.async_press()
    coord.schedule_sync.async_refresh.assert_awaited_once_with(
        reason="editor_refresh", serials=[RANDOM_SERIAL]
    )

    await save.async_press()
    coord.schedule_sync.async_upsert_slot.assert_awaited_once()
    saved_slot = coord.schedule_sync.async_upsert_slot.await_args.args[1]
    assert saved_slot["startTime"] == "06:00"
    assert saved_slot["days"] == [1]
    assert saved_slot["chargingLevelAmp"] == 20

    await schedule_select.async_select_option(current_label)
    assert delete.available is True
    await delete.async_press()
    coord.schedule_sync.async_delete_slot.assert_awaited_once_with(
        RANDOM_SERIAL, "slot-1"
    )

    editor.select_schedule(RANDOM_SERIAL, NEW_SCHEDULE_OPTION)
    editor.set_edit_day(RANDOM_SERIAL, "mon", False)
    with pytest.raises(Exception, match="Select at least one weekday"):
        await save.async_press()

    editor.set_edit_day(RANDOM_SERIAL, "mon", True)
    editor.set_edit_time(RANDOM_SERIAL, "start_time", dt_time(8, 0))
    editor.set_edit_time(RANDOM_SERIAL, "end_time", dt_time(8, 0))
    with pytest.raises(Exception, match="must be different"):
        await save.async_press()

    assert schedule_select.device_info["name"] == "Garage Charger"
    assert start.device_info["name"] == "Garage Charger"


@pytest.mark.asyncio
async def test_evse_schedule_select_always_offers_create_option(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _prepare_evse_schedule_coord(coord)
    editor = _attach_editor_runtime(config_entry, coord)
    schedule_select = EvseScheduleSelect(coord, config_entry, RANDOM_SERIAL)

    assert schedule_select.options == [
        evse_schedule_option_label(editor.get_schedule(RANDOM_SERIAL, "slot-1")),
        evse_schedule_option_label(editor.get_schedule(RANDOM_SERIAL, "slot-2")),
        evse_schedule_create_label(),
    ]


@pytest.mark.asyncio
async def test_evse_schedule_editor_entities_handle_missing_runtime(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _prepare_evse_schedule_coord(coord)
    config_entry.__dict__["options"] = MappingProxyType(
        {**config_entry.options, OPT_SCHEDULE_SYNC_ENABLED: True}
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    schedule_select = EvseScheduleSelect(coord, config_entry, RANDOM_SERIAL)
    start = EvseScheduleEditStartTimeEntity(coord, config_entry, RANDOM_SERIAL)
    day = EvseScheduleEditorDaySwitch(coord, config_entry, RANDOM_SERIAL, "mon")
    save = EvseScheduleSaveButton(coord, config_entry, RANDOM_SERIAL)
    delete = EvseScheduleDeleteButton(coord, config_entry, RANDOM_SERIAL)

    coord.data[RANDOM_SERIAL]["min_amp"] = "bad"
    coord.data[RANDOM_SERIAL]["max_amp"] = object()

    assert schedule_select.available is False
    assert schedule_select.options == []
    assert schedule_select.current_option is None
    await schedule_select.async_select_option("ignored")

    assert start.available is False
    assert start.native_value is None
    assert day.available is False
    assert day.is_on is False
    await day.async_turn_off()

    await save.async_press()
    await delete.async_press()

    editor = _attach_editor_runtime(config_entry, coord)
    select_with_editor = EvseScheduleSelect(coord, config_entry, RANDOM_SERIAL)
    editor.select_schedule(RANDOM_SERIAL, NEW_SCHEDULE_OPTION)
    editor.set_edit_day(RANDOM_SERIAL, "mon", True)
    editor.set_edit_time(RANDOM_SERIAL, "start_time", dt_time(6, 0))
    editor.set_edit_time(RANDOM_SERIAL, "end_time", dt_time(7, 0))
    coord.schedule_sync = None
    save_with_editor = EvseScheduleSaveButton(coord, config_entry, RANDOM_SERIAL)
    await save_with_editor.async_press()
    delete_new = EvseScheduleDeleteButton(coord, config_entry, RANDOM_SERIAL)
    await delete_new.async_press()

    editor.select_schedule(RANDOM_SERIAL, "missing")
    assert select_with_editor.current_option is None
    day_with_editor = EvseScheduleEditorDaySwitch(
        coord, config_entry, RANDOM_SERIAL, "mon"
    )
    await day_with_editor.async_turn_off()
    editor.select_schedule(RANDOM_SERIAL, "slot-1")
    delete_with_editor = EvseScheduleDeleteButton(coord, config_entry, RANDOM_SERIAL)
    await delete_with_editor.async_press()


@pytest.mark.asyncio
async def test_evse_schedule_save_raises_when_backend_rejects_create(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _prepare_evse_schedule_coord(coord)
    _attach_editor_runtime(config_entry, coord)

    coord.schedule_sync.async_upsert_slot = AsyncMock(return_value=False)

    save = EvseScheduleSaveButton(coord, config_entry, RANDOM_SERIAL)
    editor = config_entry.runtime_data.evse_schedule_editor
    editor.select_schedule(RANDOM_SERIAL, NEW_SCHEDULE_OPTION)
    editor.set_edit_day(RANDOM_SERIAL, "mon", True)
    editor.set_edit_time(RANDOM_SERIAL, "start_time", dt_time(6, 0))
    editor.set_edit_time(RANDOM_SERIAL, "end_time", dt_time(7, 0))

    with pytest.raises(ServiceValidationError, match="rejected the schedule change"):
        await save.async_press()


async def test_evse_schedule_delete_raises_when_backend_rejects(
    config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    _prepare_evse_schedule_coord(coord)
    _attach_editor_runtime(config_entry, coord)
    coord.schedule_sync.async_delete_slot = AsyncMock(return_value=False)
    editor = config_entry.runtime_data.evse_schedule_editor
    editor.select_schedule(RANDOM_SERIAL, "slot-1")
    button = EvseScheduleDeleteButton(coord, config_entry, RANDOM_SERIAL)

    with pytest.raises(ServiceValidationError, match="rejected the schedule change"):
        await button.async_press()

    coord.schedule_sync.async_delete_slot.assert_awaited_once_with(
        RANDOM_SERIAL, "slot-1"
    )
    assert editor.current_selection(RANDOM_SERIAL) == "slot-1"


@pytest.mark.parametrize("action", ["save", "delete"])
async def test_evse_schedule_buttons_surface_existing_schedule_write_failure(
    hass, config_entry, coordinator_factory, action
) -> None:
    """Exercise the real editor-to-scheduler path with a failing cloud request."""
    coord = coordinator_factory()
    slots = _prepare_evse_schedule_coord(coord)
    coord.schedule_sync = ScheduleSync(hass, coord, config_entry)
    coord.schedule_sync._slot_cache = slots
    editor = _attach_editor_runtime(config_entry, coord)
    editor.select_schedule(RANDOM_SERIAL, "slot-1")
    editor.set_edit_time(RANDOM_SERIAL, "start_time", dt_time(6, 0))
    editor.set_edit_time(RANDOM_SERIAL, "end_time", dt_time(7, 0))
    request = AsyncMock(side_effect=TimeoutError("schedule request timed out"))
    if action == "save":
        coord.client.patch_schedule = request
        button = EvseScheduleSaveButton(coord, config_entry, RANDOM_SERIAL)
    else:
        coord.client.delete_schedule = request
        button = EvseScheduleDeleteButton(coord, config_entry, RANDOM_SERIAL)

    with pytest.raises(ServiceValidationError) as exc:
        await button.async_press()

    assert exc.value.translation_key == "evse_schedule_change_rejected"
    request.assert_awaited_once()
    assert coord.schedule_sync._slot_cache == _slot_cache()
    assert editor.current_selection(RANDOM_SERIAL) == "slot-1"
