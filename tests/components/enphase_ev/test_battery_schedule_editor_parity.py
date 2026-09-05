from __future__ import annotations

from datetime import datetime, time as dt_time, timezone
from types import MappingProxyType
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.entity import EntityCategory

from custom_components.enphase_ev.battery_schedule_editor import (
    BatteryScheduleEditorEntity,
    BatteryScheduleEditorManager,
    BatteryScheduleRecord,
    NEW_SCHEDULE_OPTION,
    _minutes_of_day,
    battery_schedule_overlap_message,
    battery_schedule_overlap_record,
    battery_scheduler_enabled,
    editor_days_from_list,
    _normalize_days,
    _time_to_text,
    battery_schedule_option_label,
    battery_schedule_type_options,
    battery_schedule_inventory,
)
from custom_components.enphase_ev.const import DOMAIN, OPT_BATTERY_SCHEDULES_ENABLED
from custom_components.enphase_ev.labels import battery_schedule_create_label
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData


@pytest.fixture(autouse=True)
def _expose_admin_service_handlers_for_unit_tests(monkeypatch) -> None:
    """Keep direct handler tests focused on schedule behavior, not HA authorization."""

    def fake_register_admin(
        hass, domain, service, handler, schema=None, supports_response=None, **kwargs
    ) -> None:
        hass.services.async_register(
            domain,
            service,
            handler,
            schema,
            supports_response,
            **kwargs,
        )

    monkeypatch.setattr(
        "custom_components.enphase_ev.services.async_register_admin_service",
        fake_register_admin,
    )


def _schedule_payload() -> dict[str, object]:
    return {
        "cfg": {
            "scheduleStatus": "active",
            "details": [
                {
                    "scheduleId": "abc123",
                    "startTime": "01:00:00",
                    "endTime": "03:30:00",
                    "limit": 90,
                    "days": [1, 3, 5, 5, 9],
                    "timezone": "Australia/Melbourne",
                    "isEnabled": True,
                }
            ],
        },
        "dtg": {
            "scheduleStatus": "pending",
            "details": [
                {
                    "scheduleId": "def456",
                    "startTime": "18:00",
                    "endTime": "21:00",
                    "limit": 40,
                    "days": [2, 4],
                    "timezone": "UTC",
                    "isEnabled": False,
                    "scheduleStatus": "pending",
                }
            ],
        },
        "rbd": {
            "scheduleStatus": "none",
            "details": [],
        },
    }


def _prepare_battery_schedule_coord(coord) -> dict[str, object]:
    payload = _schedule_payload()
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_has_enpower = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_write_access_confirmed = True  # noqa: SLF001
    coord.last_success_utc = datetime.now(timezone.utc)
    coord.last_update_success = True
    coord.client.battery_schedules = AsyncMock(return_value=payload)
    coord.client.create_battery_schedule = AsyncMock(return_value={"status": "ok"})
    coord.client.update_battery_schedule = AsyncMock(return_value={"status": "ok"})
    coord.client.delete_battery_schedule = AsyncMock(return_value={"status": "ok"})
    coord.client.validate_battery_schedule = AsyncMock(return_value={"valid": True})
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord._battery_schedules_payload = payload  # noqa: SLF001
    coord.parse_battery_schedules_payload(payload)
    return payload


def _attach_editor_runtime(config_entry, coord) -> BatteryScheduleEditorManager:
    editor = BatteryScheduleEditorManager(coord)
    editor.sync_from_coordinator()
    config_entry.__dict__["options"] = MappingProxyType(
        {**config_entry.options, OPT_BATTERY_SCHEDULES_ENABLED: True}
    )
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        battery_schedule_editor=editor,
    )
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)
    return editor


def test_battery_schedule_inventory_normalizes_payload_and_fallbacks() -> None:
    records = battery_schedule_inventory(
        SimpleNamespace(_battery_schedules_payload=_schedule_payload())
    )

    assert _time_to_text(dt_time(2, 15)) == "02:15"
    assert _time_to_text(None, default="09:00") == "09:00"
    assert _time_to_text(75) == "01:15"
    assert _time_to_text("09:45:00") == "09:45"
    assert _time_to_text("bad") == "00:00"
    assert _normalize_days("bad") == []
    assert _normalize_days([3, "4", 4, 8, "bad"]) == [3, 4]

    assert [record.schedule_id for record in records] == ["abc123", "def456"]
    assert records[0].start_time == "01:00"
    assert records[0].end_time == "03:30"
    assert records[0].days == [1, 3, 5]
    assert records[0].enabled is True
    assert records[1].schedule_status == "pending"

    invalid_records = battery_schedule_inventory(
        SimpleNamespace(
            _battery_schedules_payload={
                "cfg": {"scheduleStatus": "active", "details": "bad"},
                "dtg": {
                    "scheduleStatus": "pending",
                    "details": [None, {"startTime": "02:00"}],
                },
                "rbd": {"scheduleStatus": "active", "details": []},
            }
        )
    )
    assert invalid_records == []

    fallback_records = battery_schedule_inventory(
        SimpleNamespace(
            _battery_schedules_payload=None,
            _battery_cfg_schedule_id="789abc",
            battery_charge_from_grid_start_time=dt_time(4, 0),
            battery_charge_from_grid_end_time=dt_time(6, 0),
            _battery_cfg_schedule_limit=95,
            _battery_cfg_schedule_days=[1, 7],
            _battery_cfg_schedule_timezone="Australia/Sydney",
            _battery_charge_from_grid_schedule_enabled=True,
            _battery_cfg_schedule_status="active",
            _battery_dtg_schedule_id="fedcba",
            battery_discharge_to_grid_start_time=1080,
            battery_discharge_to_grid_end_time=1320,
            _battery_dtg_schedule_limit=35,
            _battery_dtg_schedule_days=[2, 4],
            _battery_dtg_schedule_timezone="UTC",
            _battery_dtg_schedule_enabled=False,
            _battery_dtg_schedule_status="pending",
            _battery_rbd_schedule_id=None,
            battery_restrict_battery_discharge_start_time=None,
            battery_restrict_battery_discharge_end_time=None,
            _battery_rbd_schedule_limit=None,
            _battery_rbd_schedule_days=None,
            _battery_rbd_schedule_timezone=None,
            _battery_rbd_schedule_enabled=None,
            _battery_rbd_schedule_status=None,
        )
    )

    assert [record.schedule_id for record in fallback_records] == ["789abc", "fedcba"]
    assert fallback_records[1].start_time == "18:00"
    assert fallback_records[1].end_time == "22:00"


def test_battery_schedule_overlap_record_detects_same_day_and_wraparound() -> None:
    coord = SimpleNamespace(_battery_schedules_payload=_schedule_payload())

    conflict = battery_schedule_overlap_record(
        coord,
        start_time="02:30",
        end_time="04:00",
        days=[1],
    )

    assert conflict is not None
    assert conflict.schedule_id == "abc123"
    assert "charge from grid schedule" in battery_schedule_overlap_message(conflict)

    assert (
        battery_schedule_overlap_record(
            coord,
            start_time="03:30",
            end_time="04:30",
            days=[1],
        )
        is None
    )

    wraparound_coord = SimpleNamespace(
        _battery_schedules_payload={
            "dtg": {
                "scheduleStatus": "pending",
                "details": [
                    {
                        "scheduleId": "wrap",
                        "startTime": "23:00",
                        "endTime": "01:00",
                        "limit": 30,
                        "days": [7],
                        "timezone": "UTC",
                        "isEnabled": False,
                    }
                ],
            }
        }
    )

    wrap_conflict = battery_schedule_overlap_record(
        wraparound_coord,
        start_time="00:30",
        end_time="00:45",
        days=[1],
    )

    assert wrap_conflict is not None
    assert wrap_conflict.schedule_id == "wrap"


def test_battery_schedule_overlap_helpers_cover_invalid_and_fallback_paths() -> None:
    coord = SimpleNamespace(
        _battery_schedules_payload={
            "cfg": {
                "details": [
                    {
                        "scheduleId": "invalid",
                        "startTime": "bad",
                        "endTime": "02:00",
                        "limit": 90,
                        "days": [1],
                        "timezone": "UTC",
                        "isEnabled": True,
                    },
                    {
                        "scheduleId": "invalid-end",
                        "startTime": "01:00",
                        "endTime": "bad",
                        "limit": 90,
                        "days": [1],
                        "timezone": "UTC",
                        "isEnabled": True,
                    },
                ]
            }
        }
    )

    assert _minutes_of_day(dt_time(2, 15)) == 135
    assert _minutes_of_day(75) == 75
    assert _minutes_of_day(-1) is None
    assert _minutes_of_day(24 * 60) is None
    assert _minutes_of_day(object()) is None
    assert _minutes_of_day("") is None
    assert _minutes_of_day("nope") is None
    assert _minutes_of_day("ab:cd") is None
    assert _minutes_of_day("24:00") is None

    assert (
        battery_schedule_overlap_record(
            coord,
            start_time=None,
            end_time="02:00",
            days=[1],
        )
        is None
    )
    assert (
        battery_schedule_overlap_record(
            coord,
            start_time="01:00",
            end_time="01:00",
            days=[1],
        )
        is None
    )
    assert (
        battery_schedule_overlap_record(
            coord,
            start_time="01:00",
            end_time="02:00",
            days=[],
        )
        is None
    )

    message = battery_schedule_overlap_message(
        BatteryScheduleRecord(
            schedule_id="fallback",
            schedule_type="",
            start_time="00:00",
            end_time="01:00",
            limit=None,
            days=[1],
            timezone="UTC",
            enabled=None,
            schedule_status=None,
        )
    )
    assert message == (
        "Schedule overlaps with the existing battery schedule. "
        "Adjust or disable that schedule first."
    )

    assert (
        battery_schedule_overlap_message(
            BatteryScheduleRecord(
                schedule_id="already",
                schedule_type="schedule",
                start_time="00:00",
                end_time="01:00",
                limit=None,
                days=[1],
                timezone="UTC",
                enabled=None,
                schedule_status=None,
            )
        )
        == "Schedule overlaps with the existing schedule. Adjust or disable that schedule first."
    )


def test_battery_schedule_overlap_record_skips_existing_records_with_invalid_times(
    monkeypatch,
) -> None:
    invalid_record = BatteryScheduleRecord(
        schedule_id="bad-record",
        schedule_type="cfg",
        start_time="01:00",
        end_time="bad",
        limit=90,
        days=[1],
        timezone="UTC",
        enabled=True,
        schedule_status="active",
    )

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_schedule_editor.battery_schedule_inventory",
        lambda _coord: [invalid_record],
    )

    assert (
        battery_schedule_overlap_record(
            SimpleNamespace(),
            start_time="01:30",
            end_time="02:00",
            days=[1],
        )
        is None
    )


def test_battery_schedule_inventory_prefers_control_enabled_state_when_known() -> None:
    records = battery_schedule_inventory(
        SimpleNamespace(
            _battery_schedules_payload=_schedule_payload(),
            battery_charge_from_grid_schedule_enabled=False,
            battery_dtg_control_enabled=True,
        )
    )

    assert records[0].enabled is False
    assert records[1].enabled is True


def test_battery_scheduler_enabled_defaults_true_when_option_missing() -> None:
    config_entry = SimpleNamespace(options={})

    assert battery_scheduler_enabled(config_entry) is True
    assert battery_scheduler_enabled(None) is False


def test_battery_schedule_editor_manager_syncs_and_resets_selection() -> None:
    coord = SimpleNamespace(_battery_schedules_payload=_schedule_payload())
    manager = BatteryScheduleEditorManager(coord)
    notifications: list[str] = []
    unsub = manager.async_add_listener(lambda: notifications.append("updated"))

    manager.sync_from_coordinator()
    assert len(notifications) == 1
    assert [item["schedule_id"] for item in manager.as_dicts()] == ["abc123", "def456"]
    assert manager.get_schedule("abc123") is not None
    assert manager.edit.selected_schedule_id == "abc123"
    assert manager.current_selection == "abc123"

    manager.select_schedule("abc123")
    assert manager.edit.selected_schedule_id == "abc123"
    assert manager.edit.schedule_type == "cfg"
    assert manager.edit.start_time == "01:00"
    assert manager.edit.limit == 90

    coord._battery_schedules_payload["cfg"]["details"][0]["startTime"] = "04:15"
    manager.sync_from_coordinator()
    assert manager.edit.start_time == "04:15"

    manager.set_edit_time("start_time", dt_time(5, 45))
    manager.set_edit_time("end_time", dt_time(9, 15))
    manager.set_edit_limit(66)
    manager.set_edit_day("mon", False)
    manager.set_new_schedule_type("rbd")

    assert manager.edit.start_time == "05:45"
    assert manager.edit.end_time == "09:15"
    assert manager.edit.limit == 66
    assert manager.edit.days["mon"] is False
    assert manager.edit.schedule_type == "rbd"

    manager.sync_from_coordinator()
    assert manager.edit.start_time == "05:45"
    assert manager.edit.limit == 66

    manager.select_schedule(NEW_SCHEDULE_OPTION)
    assert manager.is_creating is True
    assert manager.current_selection == NEW_SCHEDULE_OPTION
    assert manager.edit.selected_schedule_id is None
    assert manager.edit.limit == 100
    assert manager.edit.days["sun"] is True

    manager.set_edit_time("start_time", dt_time(7, 30))
    coord._battery_schedules_payload["cfg"]["details"][0]["startTime"] = "06:00"
    manager.sync_from_coordinator()
    assert manager.edit.start_time == "07:30"

    manager.select_schedule("missing")
    assert manager.edit.selected_schedule_id is None
    assert manager.edit.limit == 100

    manager.edit.selected_schedule_id = "ghost"
    manager.edit.limit = 12
    manager.sync_from_coordinator()
    assert manager.edit.selected_schedule_id == "abc123"
    assert manager.edit.limit == 90

    coord._battery_schedules_payload = {
        "cfg": {"scheduleStatus": "active", "details": []}
    }
    manager.sync_from_coordinator()

    assert manager.edit.selected_schedule_id is None
    assert manager.edit.limit == 100
    assert manager.edit.days["tue"] is True
    assert manager.is_creating is True

    coord._battery_schedules_payload = _schedule_payload()
    manager.sync_from_coordinator()
    assert manager.edit.selected_schedule_id == "abc123"
    assert manager.is_creating is False

    unsub()
    manager.set_edit_limit(55)
    assert notifications


def test_battery_schedule_editor_manager_promotes_created_match_and_duplicate_labels() -> (
    None
):
    coord = SimpleNamespace(_battery_schedules_payload=_schedule_payload())
    manager = BatteryScheduleEditorManager(coord)
    manager.sync_from_coordinator()
    manager.select_schedule(NEW_SCHEDULE_OPTION)
    manager.set_new_schedule_type("cfg")
    manager.set_edit_time("start_time", dt_time(1, 0))
    manager.set_edit_time("end_time", dt_time(3, 30))
    manager.set_edit_limit(90)
    manager.set_edit_day("sun", False)
    manager.set_edit_day("mon", True)
    manager.set_edit_day("tue", False)
    manager.set_edit_day("wed", True)
    manager.set_edit_day("thu", False)
    manager.set_edit_day("fri", True)
    manager.set_edit_day("sat", False)

    manager.sync_from_coordinator()

    assert manager.edit.selected_schedule_id == "abc123"
    assert manager.is_creating is False

    coord._battery_schedules_payload = {
        "cfg": {
            "scheduleStatus": "active",
            "details": [
                {
                    "scheduleId": "abc123",
                    "startTime": "01:00:00",
                    "endTime": "03:30:00",
                    "limit": 90,
                    "days": [1, 3, 5],
                    "timezone": "Australia/Melbourne",
                    "isEnabled": True,
                },
                {
                    "scheduleId": "dup99999",
                    "startTime": "01:00:00",
                    "endTime": "03:30:00",
                    "limit": 90,
                    "days": [1, 3, 5],
                    "timezone": "Australia/Melbourne",
                    "isEnabled": True,
                },
            ],
        }
    }
    manager.sync_from_coordinator()

    labels = manager.option_label_by_schedule_id()
    assert labels["abc123"].endswith("[abc123]")
    assert labels["dup99999"].endswith("[dup99999]")
    assert manager.schedule_id_for_option_label("missing") is None


def test_editor_days_from_list_marks_only_selected_days_true() -> None:
    assert editor_days_from_list([1, 2, 3, 4, 5]) == {
        "mon": True,
        "tue": True,
        "wed": True,
        "thu": True,
        "fri": True,
        "sat": False,
        "sun": False,
    }


def test_battery_schedule_editor_manager_handles_missing_selection_without_fallback() -> (
    None
):
    coord = SimpleNamespace(_battery_schedules_payload=_schedule_payload())
    manager = BatteryScheduleEditorManager(coord)
    notifications: list[str] = []
    manager.async_add_listener(lambda: notifications.append("updated"))
    manager.sync_from_coordinator()

    manager.edit.create_mode = False
    manager.edit.selected_schedule_id = "ghost"
    coord._battery_schedules_payload = {
        "cfg": {"scheduleStatus": "active", "details": []}
    }
    manager.sync_from_coordinator()

    assert manager.is_creating is True
    assert manager.edit.selected_schedule_id is None
    assert notifications

    notifications.clear()
    coord._battery_schedules_payload = {
        "cfg": {
            "scheduleStatus": "active",
            "details": [
                {
                    "scheduleId": "abc123",
                    "startTime": "01:00:00",
                    "endTime": "03:30:00",
                    "limit": 90,
                    "days": [1, 3, 5],
                    "timezone": "Australia/Melbourne",
                    "isEnabled": True,
                }
            ],
        }
    }
    manager.edit.create_mode = False
    manager.edit.selected_schedule_id = None
    coord._battery_schedules_payload = {
        "cfg": {"scheduleStatus": "active", "details": []}
    }
    manager.sync_from_coordinator()

    assert notifications == ["updated"]


def test_battery_schedule_option_label_falls_back_for_unknown_type() -> None:
    label = battery_schedule_option_label(
        BatteryScheduleRecord(
            schedule_id="mystery",
            schedule_type=" ",
            start_time="09:00",
            end_time="10:00",
            limit=None,
            days=[],
            timezone=None,
            enabled=None,
            schedule_status=None,
        )
    )

    assert label == "Schedule 09:00-10:00"


@pytest.mark.asyncio
async def test_battery_schedule_editor_entity_listens_for_manager_updates(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    editor = _attach_editor_runtime(config_entry, coord)

    async def _noop_async_added(self) -> None:
        return None

    async def _noop_async_removed(self) -> None:
        return None

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_schedule_editor.CoordinatorEntity.async_added_to_hass",
        _noop_async_added,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_schedule_editor.CoordinatorEntity.async_will_remove_from_hass",
        _noop_async_removed,
    )

    class DummyEntity(BatteryScheduleEditorEntity):
        pass

    entity = DummyEntity(coord, config_entry)
    entity.hass = hass
    entity.async_write_ha_state = Mock()

    await entity.async_added_to_hass()
    editor.set_edit_limit(72)
    entity.async_write_ha_state.assert_called_once()

    await entity.async_will_remove_from_hass()
    entity.async_write_ha_state.reset_mock()
    editor.set_edit_limit(73)
    entity.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_battery_schedule_platform_setup_adds_editor_entities(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.button import (
        BatteryForceRefreshButton,
        BatteryScheduleDeleteButton,
        BatteryScheduleSaveButton,
    )
    from custom_components.enphase_ev.number import BatteryScheduleEditLimitNumber
    from custom_components.enphase_ev.select import (
        BatteryNewScheduleTypeSelect,
        BatteryScheduleSelect,
    )
    from custom_components.enphase_ev.sensor import EnphaseBatteryScheduleModeSensor
    from custom_components.enphase_ev.switch import BatteryScheduleEditorDaySwitch
    from custom_components.enphase_ev.time import (
        BatteryScheduleEditEndTimeEntity,
        BatteryScheduleEditStartTimeEntity,
    )

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
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

    from custom_components.enphase_ev import (
        button,
        number,
        select,
        sensor,
        switch,
        time,
    )

    await select.async_setup_entry(hass, config_entry, _capture)
    await time.async_setup_entry(hass, config_entry, _capture)
    await number.async_setup_entry(hass, config_entry, _capture)
    await switch.async_setup_entry(hass, config_entry, _capture)
    await button.async_setup_entry(hass, config_entry, _capture)
    await sensor.async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(entity, BatteryScheduleSelect) for entity in added)
    assert any(isinstance(entity, BatteryNewScheduleTypeSelect) for entity in added)
    assert any(
        isinstance(entity, BatteryScheduleEditStartTimeEntity) for entity in added
    )
    assert any(isinstance(entity, BatteryScheduleEditEndTimeEntity) for entity in added)
    assert any(isinstance(entity, BatteryScheduleEditLimitNumber) for entity in added)
    assert any(isinstance(entity, BatteryScheduleEditorDaySwitch) for entity in added)
    assert any(isinstance(entity, BatteryForceRefreshButton) for entity in added)
    assert any(isinstance(entity, BatteryScheduleSaveButton) for entity in added)
    assert any(isinstance(entity, BatteryScheduleDeleteButton) for entity in added)
    assert not any(
        "battery_new_schedule_" in entity.unique_id
        and not isinstance(entity, BatteryNewScheduleTypeSelect)
        for entity in added
    )
    assert not any(
        entity.unique_id.endswith("battery_schedule_add") for entity in added
    )
    assert (
        sum(isinstance(entity, EnphaseBatteryScheduleModeSensor) for entity in added)
        == 3
    )
    editor_entities = [
        entity
        for entity in added
        if isinstance(
            entity,
            (
                BatteryScheduleSelect,
                BatteryNewScheduleTypeSelect,
                BatteryScheduleEditStartTimeEntity,
                BatteryScheduleEditEndTimeEntity,
                BatteryScheduleEditLimitNumber,
                BatteryScheduleEditorDaySwitch,
                BatteryScheduleSaveButton,
                BatteryScheduleDeleteButton,
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
        if isinstance(entity, BatteryScheduleEditorDaySwitch)
    )
    assert callbacks


@pytest.mark.asyncio
async def test_sensor_setup_removes_schedule_sensors_when_scheduler_disabled(
    hass, config_entry, coordinator_factory
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryCfgScheduleStatusSensor,
        EnphaseBatteryScheduleModeSensor,
        async_setup_entry,
    )

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    coord._devices_inventory_ready = False  # noqa: SLF001
    object.__setattr__(
        config_entry,
        "options",
        {OPT_BATTERY_SCHEDULES_ENABLED: False},
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)
    ent_reg = er.async_get(hass)
    unique_ids = [
        f"{DOMAIN}_site_{coord.site_id}_{key}"
        for key in (
            "battery_cfg_schedule_status",
            "battery_schedule_summary",
            "battery_cfg_schedules",
            "battery_dtg_schedules",
            "battery_rbd_schedules",
        )
    ]
    entity_ids = [
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            config_entry=config_entry,
        ).entity_id
        for unique_id in unique_ids
    ]
    added: list[object] = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert all(ent_reg.async_get(entity_id) is None for entity_id in entity_ids)
    assert not any(
        isinstance(
            entity,
            (EnphaseBatteryCfgScheduleStatusSensor, EnphaseBatteryScheduleModeSensor),
        )
        for entity in added
    )


@pytest.mark.asyncio
async def test_battery_schedule_platform_setup_skips_write_editor_when_crud_missing(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import button, number, select, switch, time
    from custom_components.enphase_ev.button import BatteryForceRefreshButton
    from custom_components.enphase_ev.number import BatteryScheduleEditLimitNumber
    from custom_components.enphase_ev.select import BatteryScheduleSelect
    from custom_components.enphase_ev.switch import BatteryScheduleEditorDaySwitch
    from custom_components.enphase_ev.time import BatteryScheduleEditStartTimeEntity

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    coord.client.create_battery_schedule = None
    coord.client.update_battery_schedule = None
    coord.client.delete_battery_schedule = None
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
        {OPT_BATTERY_SCHEDULES_ENABLED: True}
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

    assert any(isinstance(entity, BatteryForceRefreshButton) for entity in added)
    assert not any(isinstance(entity, BatteryScheduleSelect) for entity in added)
    assert not any(
        isinstance(entity, BatteryScheduleEditStartTimeEntity) for entity in added
    )
    assert not any(
        isinstance(entity, BatteryScheduleEditLimitNumber) for entity in added
    )
    assert not any(
        isinstance(entity, BatteryScheduleEditorDaySwitch) for entity in added
    )


@pytest.mark.asyncio
async def test_battery_schedule_platform_setup_skips_editor_when_option_disabled(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import button, number, select, switch, time
    from custom_components.enphase_ev.button import BatteryForceRefreshButton
    from custom_components.enphase_ev.number import BatteryScheduleEditLimitNumber
    from custom_components.enphase_ev.select import (
        BatteryNewScheduleTypeSelect,
        BatteryScheduleSelect,
    )
    from custom_components.enphase_ev.switch import BatteryScheduleEditorDaySwitch
    from custom_components.enphase_ev.time import BatteryScheduleEditStartTimeEntity

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
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
        {OPT_BATTERY_SCHEDULES_ENABLED: False}
    )
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        battery_schedule_editor=BatteryScheduleEditorManager(coord),
    )
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    added: list[object] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await select.async_setup_entry(hass, config_entry, _capture)
    await time.async_setup_entry(hass, config_entry, _capture)
    await number.async_setup_entry(hass, config_entry, _capture)
    await switch.async_setup_entry(hass, config_entry, _capture)
    await button.async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(entity, BatteryScheduleSelect) for entity in added)
    assert not any(isinstance(entity, BatteryNewScheduleTypeSelect) for entity in added)
    assert not any(
        isinstance(entity, BatteryScheduleEditStartTimeEntity) for entity in added
    )
    assert not any(
        isinstance(entity, BatteryScheduleEditLimitNumber) for entity in added
    )
    assert not any(
        isinstance(entity, BatteryScheduleEditorDaySwitch) for entity in added
    )
    assert not any(isinstance(entity, BatteryForceRefreshButton) for entity in added)


@pytest.mark.asyncio
async def test_battery_schedule_select_setup_does_not_prune_site_selector(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.select import async_setup_entry

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    monkeypatch.setattr(
        coord, "async_add_listener", lambda callback: (lambda: None), raising=False
    )
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    _attach_editor_runtime(config_entry, coord)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "select",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_battery_schedule_selected",
        config_entry=config_entry,
    )
    assert stale is not None

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert (
        ent_reg.async_get_entity_id(
            "select",
            DOMAIN,
            f"{DOMAIN}_site_{coord.site_id}_battery_schedule_selected",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_battery_schedule_switch_setup_does_not_prune_site_day_switch(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.switch import async_setup_entry

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    monkeypatch.setattr(
        coord, "async_add_listener", lambda callback: (lambda: None), raising=False
    )
    _attach_editor_runtime(config_entry, coord)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "switch",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_mon",
        config_entry=config_entry,
    )
    assert stale is not None

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert (
        ent_reg.async_get_entity_id(
            "switch",
            DOMAIN,
            f"{DOMAIN}_site_{coord.site_id}_battery_schedule_edit_mon",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_battery_schedule_switch_setup_adds_day_switches_after_topology_ready(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.switch import BatteryScheduleEditorDaySwitch
    from custom_components.enphase_ev.switch import async_setup_entry

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    coord._devices_inventory_ready = False  # noqa: SLF001

    generic_callbacks: list[object] = []
    topology_callbacks: list[object] = []

    monkeypatch.setattr(
        coord,
        "async_add_listener",
        lambda callback: generic_callbacks.append(callback) or (lambda: None),
        raising=False,
    )
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: topology_callbacks.append(callback) or (lambda: None),
        raising=False,
    )
    monkeypatch.setattr(
        coord.inventory_view,
        "has_type_for_entities",
        lambda type_key: coord._devices_inventory_ready and type_key == "encharge",
        raising=False,
    )
    _attach_editor_runtime(config_entry, coord)

    added: list[object] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(
        isinstance(entity, BatteryScheduleEditorDaySwitch) for entity in added
    )
    assert topology_callbacks

    coord._devices_inventory_ready = True  # noqa: SLF001
    for callback in topology_callbacks:
        callback()

    assert any(isinstance(entity, BatteryScheduleEditorDaySwitch) for entity in added)


@pytest.mark.asyncio
async def test_battery_schedule_platform_setup_keeps_entities_when_write_access_unconfirmed(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import button, number, select, switch, time
    from custom_components.enphase_ev.button import (
        BatteryForceRefreshButton,
        BatteryScheduleDeleteButton,
        BatteryScheduleSaveButton,
    )
    from custom_components.enphase_ev.number import BatteryScheduleEditLimitNumber
    from custom_components.enphase_ev.select import (
        BatteryNewScheduleTypeSelect,
        BatteryScheduleSelect,
    )
    from custom_components.enphase_ev.switch import BatteryScheduleEditorDaySwitch
    from custom_components.enphase_ev.time import BatteryScheduleEditStartTimeEntity

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    monkeypatch.setattr(
        coord, "async_add_listener", lambda callback: (lambda: None), raising=False
    )
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
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

    schedule_select = next(
        entity for entity in added if isinstance(entity, BatteryScheduleSelect)
    )
    type_select = next(
        entity for entity in added if isinstance(entity, BatteryNewScheduleTypeSelect)
    )
    edit_time = next(
        entity
        for entity in added
        if isinstance(entity, BatteryScheduleEditStartTimeEntity)
    )
    edit_limit = next(
        entity for entity in added if isinstance(entity, BatteryScheduleEditLimitNumber)
    )
    day_switch = next(
        entity for entity in added if isinstance(entity, BatteryScheduleEditorDaySwitch)
    )
    refresh_button = next(
        entity for entity in added if isinstance(entity, BatteryForceRefreshButton)
    )
    save_button = next(
        entity for entity in added if isinstance(entity, BatteryScheduleSaveButton)
    )
    delete_button = next(
        entity for entity in added if isinstance(entity, BatteryScheduleDeleteButton)
    )

    assert schedule_select is not None
    assert type_select is not None
    assert edit_time is not None
    assert edit_limit is not None
    assert day_switch is not None
    assert refresh_button.available is True
    assert save_button.available is False
    assert delete_button.available is False


@pytest.mark.asyncio
async def test_battery_schedule_editor_entities_update_state_and_call_services(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.button import (
        BatteryForceRefreshButton,
        BatteryScheduleDeleteButton,
        BatteryScheduleSaveButton,
    )
    from custom_components.enphase_ev.number import BatteryScheduleEditLimitNumber
    from custom_components.enphase_ev.select import (
        BatteryNewScheduleTypeSelect,
        BatteryScheduleSelect,
    )
    from custom_components.enphase_ev.switch import BatteryScheduleEditorDaySwitch
    from custom_components.enphase_ev.time import (
        BatteryScheduleEditEndTimeEntity,
        BatteryScheduleEditStartTimeEntity,
    )

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    editor = _attach_editor_runtime(config_entry, coord)
    custom_info = {"name": "Battery Device"}
    coord.inventory_view.type_device_info = lambda *_args: custom_info

    create_label = battery_schedule_create_label()
    cfg_label = battery_schedule_option_label(editor.schedules[0])
    dtg_label = battery_schedule_option_label(editor.schedules[1])

    schedule_select = BatteryScheduleSelect(coord, config_entry)
    assert schedule_select.options == [
        cfg_label,
        dtg_label,
        create_label,
    ]
    assert schedule_select.current_option == cfg_label

    save_button = BatteryScheduleSaveButton(coord, config_entry)
    delete_button = BatteryScheduleDeleteButton(coord, config_entry)
    assert save_button.available is True
    assert delete_button.available is True

    await schedule_select.async_select_option(cfg_label)
    assert editor.edit.selected_schedule_id == "abc123"
    assert save_button.available is True
    assert delete_button.available is True

    new_type_select = BatteryNewScheduleTypeSelect(coord, config_entry)
    cfg_type_label = [
        label for key, label in battery_schedule_type_options() if key == "cfg"
    ][0]
    assert new_type_select.available is True
    assert new_type_select.options == [cfg_type_label]
    assert new_type_select.current_option == cfg_type_label
    await schedule_select.async_select_option(create_label)
    assert editor.is_creating is True
    assert schedule_select.current_option == create_label
    assert save_button.available is True
    assert delete_button.available is False
    assert new_type_select.available is True
    rbd_label = [
        label for key, label in battery_schedule_type_options() if key == "rbd"
    ][0]
    assert new_type_select.options == [
        label for _key, label in battery_schedule_type_options()
    ]
    await new_type_select.async_select_option(rbd_label)
    assert new_type_select.current_option == rbd_label
    assert editor.edit.schedule_type == "rbd"

    edit_start = BatteryScheduleEditStartTimeEntity(coord, config_entry)
    assert edit_start.entity_category is EntityCategory.CONFIG
    await edit_start.async_set_value(dt_time(2, 45))
    assert edit_start.native_value == dt_time(2, 45)
    await BatteryScheduleEditEndTimeEntity(coord, config_entry).async_set_value(
        dt_time(4, 15)
    )
    limit = BatteryScheduleEditLimitNumber(coord, config_entry)
    assert limit.entity_category is EntityCategory.CONFIG
    await limit.async_set_native_value(65)
    assert limit.native_value == 65.0

    day_switch = BatteryScheduleEditorDaySwitch(coord, config_entry, day_key="sun")
    edit_day_switch = BatteryScheduleEditorDaySwitch(coord, config_entry, day_key="mon")
    assert day_switch.entity_category is EntityCategory.CONFIG
    assert day_switch.entity_registry_enabled_default is True
    await day_switch.async_turn_on()
    assert day_switch.is_on is True
    await day_switch.async_turn_off()
    assert day_switch.is_on is False
    await edit_day_switch.async_turn_on()
    assert editor.edit.days["mon"] is True
    await edit_day_switch.async_turn_off()
    assert editor.edit.days["mon"] is False

    service_calls: list[tuple[str, str, dict[str, object], bool]] = []

    async def fake_async_call(
        self, domain, service, service_data=None, blocking=False, **kwargs
    ):
        service_calls.append((domain, service, service_data or {}, blocking))

    monkeypatch.setattr(hass.services.__class__, "async_call", fake_async_call)
    refresh_button = BatteryForceRefreshButton(coord, config_entry)
    for entity in (refresh_button, save_button, delete_button):
        entity.hass = hass

    await refresh_button.async_press()
    await save_button.async_press()
    await schedule_select.async_select_option(cfg_label)
    await delete_button.async_press()
    await save_button.async_press()

    assert service_calls[0][:2] == (DOMAIN, "force_refresh")
    assert service_calls[1][:2] == (DOMAIN, "add_schedule")
    assert service_calls[1][2]["schedule_type"] == "rbd"
    assert service_calls[1][2]["limit"] == 65
    assert service_calls[2][:2] == (DOMAIN, "delete_schedule")
    assert service_calls[3][:2] == (DOMAIN, "update_schedule")
    assert service_calls[3][2]["schedule_id"] == "abc123"
    assert save_button.device_info == custom_info
    assert new_type_select.device_info == custom_info
    assert edit_start.device_info == custom_info
    assert limit.device_info == custom_info
    assert edit_day_switch.device_info == custom_info


@pytest.mark.asyncio
async def test_battery_schedule_save_button_updates_selected_schedule_by_default(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.button import BatteryScheduleSaveButton

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    _attach_editor_runtime(config_entry, coord)

    service_calls: list[tuple[str, str]] = []

    async def fake_async_call(
        self, domain, service, service_data=None, blocking=False, **kwargs
    ):
        service_calls.append((domain, service))

    monkeypatch.setattr(hass.services.__class__, "async_call", fake_async_call)

    save_button = BatteryScheduleSaveButton(coord, config_entry)
    save_button.hass = hass

    await save_button.async_press()

    assert service_calls == [(DOMAIN, "update_schedule")]


@pytest.mark.asyncio
async def test_battery_schedule_buttons_use_selected_schedule_family(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.button import (
        BatteryScheduleDeleteButton,
        BatteryScheduleSaveButton,
    )
    from custom_components.enphase_ev.select import BatteryScheduleSelect

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    editor = _attach_editor_runtime(config_entry, coord)

    service_calls: list[tuple[str, str, dict[str, object]]] = []

    async def fake_async_call(
        self, domain, service, service_data=None, blocking=False, **kwargs
    ):
        service_calls.append((domain, service, service_data or {}))

    monkeypatch.setattr(hass.services.__class__, "async_call", fake_async_call)

    schedule_select = BatteryScheduleSelect(coord, config_entry)
    schedule_select.hass = hass
    await schedule_select.async_select_option(
        battery_schedule_option_label(editor.schedules[1])
    )

    editor.edit.schedule_type = "cfg"

    save_button = BatteryScheduleSaveButton(coord, config_entry)
    delete_button = BatteryScheduleDeleteButton(coord, config_entry)
    save_button.hass = hass
    delete_button.hass = hass

    await save_button.async_press()
    await delete_button.async_press()

    assert service_calls[0][0:2] == (DOMAIN, "update_schedule")
    assert service_calls[0][2]["schedule_id"] == "def456"
    assert service_calls[0][2]["schedule_type"] == "dtg"
    assert service_calls[1][0:2] == (DOMAIN, "delete_schedule")
    assert service_calls[1][2]["schedule_id"] == "def456"
    assert service_calls[1][2]["schedule_type"] == "dtg"


@pytest.mark.asyncio
async def test_battery_schedule_select_and_buttons_cover_missing_selection_paths(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.button import BatteryScheduleSaveButton
    from custom_components.enphase_ev.select import (
        BatteryNewScheduleTypeSelect,
        BatteryScheduleSelect,
    )

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    editor = _attach_editor_runtime(config_entry, coord)

    service_calls: list[tuple[str, str]] = []

    async def fake_async_call(
        self, domain, service, service_data=None, blocking=False, **kwargs
    ):
        service_calls.append((domain, service))

    monkeypatch.setattr(hass.services.__class__, "async_call", fake_async_call)

    schedule_select = BatteryScheduleSelect(coord, config_entry)
    schedule_select.hass = hass
    editor.edit.selected_schedule_id = "ghost"
    assert schedule_select.current_option is None

    save_button = BatteryScheduleSaveButton(coord, config_entry)
    save_button.hass = hass
    editor.edit.create_mode = False
    editor.edit.selected_schedule_id = None
    await save_button.async_press()
    assert service_calls == []

    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)
    no_editor_type_select = BatteryNewScheduleTypeSelect(coord, config_entry)
    await no_editor_type_select.async_select_option("Charge From Grid Schedule")


@pytest.mark.asyncio
async def test_battery_schedule_editor_guard_paths_cover_missing_editor_and_fallbacks(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.button import (
        BatteryScheduleDeleteButton,
        BatteryScheduleSaveButton,
    )
    from custom_components.enphase_ev.number import BatteryScheduleEditLimitNumber
    from custom_components.enphase_ev.select import (
        BatteryNewScheduleTypeSelect,
        BatteryScheduleSelect,
    )
    from custom_components.enphase_ev.switch import BatteryScheduleEditorDaySwitch
    from custom_components.enphase_ev.time import (
        BatteryScheduleEditStartTimeEntity,
        _parse_editor_time,
    )

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    coord.inventory_view.type_device_info = lambda *_args: None
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    schedule_select = BatteryScheduleSelect(coord, config_entry)
    new_type_select = BatteryNewScheduleTypeSelect(coord, config_entry)
    edit_limit = BatteryScheduleEditLimitNumber(coord, config_entry)
    edit_time = BatteryScheduleEditStartTimeEntity(coord, config_entry)
    edit_day = BatteryScheduleEditorDaySwitch(coord, config_entry, day_key="mon")
    save_button = BatteryScheduleSaveButton(coord, config_entry)
    delete_button = BatteryScheduleDeleteButton(coord, config_entry)

    save_button.hass = hass
    delete_button.hass = hass

    assert schedule_select.available is False
    assert schedule_select.options == []
    assert schedule_select.current_option is None
    assert new_type_select.options == []
    assert new_type_select.current_option is None
    assert edit_limit.available is False
    assert edit_limit.native_value is None
    assert edit_time.available is False
    assert edit_time.native_value is None
    assert edit_day.available is False
    assert edit_day.is_on is False
    assert _parse_editor_time(None) is None
    assert _parse_editor_time("bad") is None

    await save_button.async_press()
    await delete_button.async_press()
    await edit_day.async_turn_on()
    await edit_day.async_turn_off()

    assert save_button.device_info["manufacturer"] == "Enphase"
    assert schedule_select.device_info["manufacturer"] == "Enphase"
    assert edit_limit.device_info["manufacturer"] == "Enphase"
    assert edit_time.device_info["manufacturer"] == "Enphase"
    assert edit_day.device_info["manufacturer"] == "Enphase"


def test_battery_schedule_inventory_sensors_expose_per_mode_counts(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryScheduleModeSensor,
        _battery_schedule_inventory_supported,
    )

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)

    cfg_sensor = EnphaseBatteryScheduleModeSensor(coord, "cfg")
    rbd_sensor = EnphaseBatteryScheduleModeSensor(coord, "rbd")

    assert cfg_sensor.available is True
    assert cfg_sensor.native_value == "1"
    assert cfg_sensor.extra_state_attributes["schedule_type"] == "cfg"
    assert cfg_sensor.extra_state_attributes["schedule_ids"] == ["abc123"]
    assert rbd_sensor.native_value == "0"
    assert rbd_sensor.extra_state_attributes["schedule_ids"] == []
    assert _battery_schedule_inventory_supported(coord) is True
    coord._battery_has_encharge = False  # noqa: SLF001
    assert _battery_schedule_inventory_supported(coord) is False
    coord._battery_has_encharge = True  # noqa: SLF001
    coord.client = SimpleNamespace()
    coord._battery_schedules_payload = {"cfg": {"details": []}}  # noqa: SLF001
    assert _battery_schedule_inventory_supported(coord) is True


@pytest.mark.asyncio
async def test_battery_schedule_sensor_setup_prunes_stale_inventory_entities_when_unsupported(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory()
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord.client = SimpleNamespace()
    coord._battery_schedules_payload = None  # noqa: SLF001
    coord._battery_cfg_schedule_id = None  # noqa: SLF001
    coord._battery_dtg_schedule_id = None  # noqa: SLF001
    coord._battery_rbd_schedule_id = None  # noqa: SLF001
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    monkeypatch.setattr(
        coord, "async_add_listener", lambda callback: (lambda: None), raising=False
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_battery_schedule_summary",
        config_entry=config_entry,
    )
    assert stale is not None

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert (
        ent_reg.async_get_entity_id(
            "sensor",
            DOMAIN,
            f"{DOMAIN}_site_{coord.site_id}_battery_schedule_summary",
        )
        is None
    )


@pytest.mark.asyncio
async def test_battery_schedule_sensor_setup_prunes_stale_summary_when_supported(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    monkeypatch.setattr(
        coord,
        "async_add_topology_listener",
        lambda callback: (lambda: None),
        raising=False,
    )
    monkeypatch.setattr(
        coord, "async_add_listener", lambda callback: (lambda: None), raising=False
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_battery_schedule_summary",
        config_entry=config_entry,
    )
    assert stale is not None

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert (
        ent_reg.async_get_entity_id(
            "sensor",
            DOMAIN,
            f"{DOMAIN}_site_{coord.site_id}_battery_schedule_summary",
        )
        is None
    )


@pytest.mark.asyncio
async def test_battery_schedule_services_support_crud_and_validation(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.enphase_ev.services import async_setup_services

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    coord._battery_charge_from_grid = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)

    force_refresh = registered[(DOMAIN, "force_refresh")]["handler"]
    add_schedule = registered[(DOMAIN, "add_schedule")]["handler"]
    update_schedule = registered[(DOMAIN, "update_schedule")]["handler"]
    delete_schedule = registered[(DOMAIN, "delete_schedule")]["handler"]
    validate_schedule = registered[(DOMAIN, "validate_schedule")]["handler"]

    await force_refresh(
        SimpleNamespace(data={"config_entry_id": config_entry.entry_id})
    )
    await add_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_type": "cfg",
                "start_time": dt_time(5, 0),
                "end_time": dt_time(7, 0),
                "limit": 88,
                "days": [1, 2, 3],
            }
        )
    )
    await update_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_id": "abc123",
                "schedule_type": "cfg",
                "start_time": dt_time(6, 0),
                "end_time": dt_time(8, 0),
                "limit": 91,
                "days": [1, 5],
                "confirm": True,
            }
        )
    )
    await delete_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_ids": ["abc123", "def456"],
                "confirm": True,
            }
        )
    )
    result = await validate_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_type": "cfg",
            }
        )
    )

    coord.async_request_refresh.assert_awaited()
    coord.client.create_battery_schedule.assert_awaited_once_with(
        schedule_type="CFG",
        start_time="05:00",
        end_time="07:00",
        limit=88,
        days=[1, 2, 3],
        timezone="US/Pacific",
        is_enabled=True,
    )
    coord.client.update_battery_schedule.assert_awaited_once_with(
        "abc123",
        schedule_type="CFG",
        start_time="06:00",
        end_time="08:00",
        limit=91,
        days=[1, 5],
        timezone="Australia/Melbourne",
        is_enabled=None,
        is_deleted=None,
    )
    assert coord.client.delete_battery_schedule.await_count == 2
    assert coord.client.delete_battery_schedule.await_args_list[0].kwargs == {
        "schedule_type": "cfg"
    }
    assert coord.client.delete_battery_schedule.await_args_list[1].kwargs == {
        "schedule_type": "dtg"
    }
    assert coord.client.set_battery_settings.await_count == 4
    create_payload = coord.client.set_battery_settings.await_args_list[0].args[0]
    assert create_payload["chargeFromGrid"] is True
    assert isinstance(create_payload["acceptedItcDisclaimer"], str)
    update_payload = coord.client.set_battery_settings.await_args_list[1].args[0]
    assert update_payload["chargeFromGrid"] is True
    assert isinstance(update_payload["acceptedItcDisclaimer"], str)
    delete_cfg_payload = coord.client.set_battery_settings.await_args_list[2].args[0]
    assert delete_cfg_payload["chargeFromGridScheduleEnabled"] is False
    assert "chargeBeginTime" not in delete_cfg_payload
    assert "chargeEndTime" not in delete_cfg_payload
    delete_dtg_payload = coord.client.set_battery_settings.await_args_list[3].args[0]
    assert delete_dtg_payload == {
        "dtgControl": {
            "enabled": False,
            "scheduleSupported": True,
            "startTime": 1080,
            "endTime": 1260,
        }
    }
    assert result == {"valid": True}

    with pytest.raises(ServiceValidationError, match="Select at least one day"):
        await add_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                    "start_time": dt_time(1, 0),
                    "end_time": dt_time(2, 0),
                    "limit": 50,
                    "days": [],
                }
            )
        )

    with pytest.raises(ServiceValidationError, match="Confirmation required"):
        await update_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "abc123",
                    "schedule_type": "cfg",
                    "start_time": dt_time(1, 0),
                    "end_time": dt_time(2, 0),
                    "limit": 50,
                    "days": [1],
                    "confirm": False,
                }
            )
        )

    with pytest.raises(ServiceValidationError, match="not found in current data"):
        await delete_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "fff999",
                    "confirm": True,
                }
            )
        )


@pytest.mark.asyncio
async def test_battery_schedule_services_update_uses_inventory_schedule_family(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.services import async_setup_services

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)

    update_schedule = registered[(DOMAIN, "update_schedule")]["handler"]

    await update_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_id": "def456",
                "schedule_type": "cfg",
                "start_time": dt_time(19, 0),
                "end_time": dt_time(22, 0),
                "limit": 41,
                "days": [2, 4],
                "confirm": True,
            }
        )
    )

    coord.client.update_battery_schedule.assert_awaited_once_with(
        "def456",
        schedule_type="DTG",
        start_time="19:00",
        end_time="22:00",
        limit=41,
        days=[2, 4],
        timezone="UTC",
        is_enabled=None,
        is_deleted=None,
    )
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": False,
                "scheduleSupported": True,
                "startTime": 1140,
                "endTime": 1320,
            }
        },
        schedule_type="dtg",
    )


@pytest.mark.asyncio
async def test_battery_schedule_services_delete_uses_enabled_remaining_schedule(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.services import async_setup_services

    coord = coordinator_factory()
    payload = _prepare_battery_schedule_coord(coord)
    payload["dtg"] = {
        "scheduleStatus": "active",
        "details": [
            {
                "scheduleId": "def456",
                "startTime": "18:00",
                "endTime": "21:00",
                "limit": 40,
                "days": [2, 4],
                "timezone": "UTC",
                "isEnabled": False,
            },
            {
                "scheduleId": "ghi789",
                "startTime": "21:30",
                "endTime": "23:00",
                "limit": 40,
                "days": [2, 4],
                "timezone": "UTC",
                "isEnabled": True,
            },
        ],
    }
    coord._battery_schedules_payload = payload  # noqa: SLF001
    coord.parse_battery_schedules_payload(payload)
    coord._battery_dtg_schedule_id = None  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {"handler": handler}

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)

    delete_schedule = registered[(DOMAIN, "delete_schedule")]["handler"]

    await delete_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_id": "def456",
                "confirm": True,
            }
        )
    )

    coord.client.delete_battery_schedule.assert_awaited_once_with(
        "def456",
        schedule_type="dtg",
    )
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": True,
                "scheduleSupported": True,
                "startTime": 1290,
                "endTime": 1380,
            }
        },
        schedule_type="dtg",
    )


@pytest.mark.asyncio
async def test_battery_schedule_services_delete_prefers_selected_remaining_schedule(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.services import async_setup_services

    coord = coordinator_factory()
    payload = _prepare_battery_schedule_coord(coord)
    coord._battery_dtg_control = (
        coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "enabled": False,
                "showDaySchedule": True,
                "scheduleSupported": True,
            }
        )
    )
    payload["dtg"] = {
        "scheduleStatus": "active",
        "details": [
            {
                "scheduleId": "def456",
                "startTime": "18:00",
                "endTime": "21:00",
                "limit": 40,
                "days": [2, 4],
                "timezone": "UTC",
                "isEnabled": False,
            },
            {
                "scheduleId": "ghi789",
                "startTime": "21:30",
                "endTime": "23:00",
                "limit": 40,
                "days": [2, 4],
                "timezone": "UTC",
                "isEnabled": True,
            },
            {
                "scheduleId": "jkl012",
                "startTime": "23:15",
                "endTime": "23:45",
                "limit": 40,
                "days": [2, 4],
                "timezone": "UTC",
                "isEnabled": False,
            },
        ],
    }
    coord._battery_schedules_payload = payload  # noqa: SLF001
    coord.parse_battery_schedules_payload(payload)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {"handler": handler}

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)

    delete_schedule = registered[(DOMAIN, "delete_schedule")]["handler"]

    await delete_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_id": "jkl012",
                "confirm": True,
            }
        )
    )

    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": False,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 1080,
                "endTime": 1260,
            }
        },
        schedule_type="dtg",
    )


@pytest.mark.asyncio
async def test_battery_schedule_services_delete_falls_back_to_first_remaining_schedule(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.services import async_setup_services

    coord = coordinator_factory()
    payload = _prepare_battery_schedule_coord(coord)
    payload["dtg"] = {
        "scheduleStatus": "active",
        "details": [
            {
                "scheduleId": "def456",
                "startTime": "18:00",
                "endTime": "21:00",
                "limit": 40,
                "days": [2, 4],
                "timezone": "UTC",
                "isEnabled": False,
            },
            {
                "scheduleId": "ghi789",
                "startTime": "21:30",
                "endTime": "23:00",
                "limit": 40,
                "days": [2, 4],
                "timezone": "UTC",
                "isEnabled": False,
            },
        ],
    }
    coord._battery_schedules_payload = payload  # noqa: SLF001
    coord.parse_battery_schedules_payload(payload)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {"handler": handler}

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)

    delete_schedule = registered[(DOMAIN, "delete_schedule")]["handler"]

    await delete_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_id": "def456",
                "confirm": True,
            }
        )
    )

    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": False,
                "scheduleSupported": True,
                "startTime": 1290,
                "endTime": 1380,
            }
        },
        schedule_type="dtg",
    )


@pytest.mark.asyncio
async def test_battery_schedule_services_update_preserves_selected_family_window(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.services import async_setup_services

    coord = coordinator_factory()
    payload = _prepare_battery_schedule_coord(coord)
    payload["dtg"] = {
        "scheduleStatus": "active",
        "details": [
            {
                "scheduleId": "def456",
                "startTime": "18:00",
                "endTime": "21:00",
                "limit": 40,
                "days": [2, 4],
                "timezone": "UTC",
                "isEnabled": False,
            },
            {
                "scheduleId": "ghi789",
                "startTime": "21:30",
                "endTime": "23:00",
                "limit": 40,
                "days": [2, 4],
                "timezone": "UTC",
                "isEnabled": True,
            },
        ],
    }
    coord._battery_schedules_payload = payload  # noqa: SLF001
    coord.parse_battery_schedules_payload(payload)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {"handler": handler}

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)

    update_schedule = registered[(DOMAIN, "update_schedule")]["handler"]

    await update_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_id": "def456",
                "schedule_type": "dtg",
                "start_time": dt_time(17, 0),
                "end_time": dt_time(17, 30),
                "limit": 41,
                "days": [2, 4],
                "confirm": True,
            }
        )
    )

    coord.client.update_battery_schedule.assert_awaited_once_with(
        "def456",
        schedule_type="DTG",
        start_time="17:00",
        end_time="17:30",
        limit=41,
        days=[2, 4],
        timezone="UTC",
        is_enabled=None,
        is_deleted=None,
    )
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": True,
                "scheduleSupported": True,
                "startTime": 1290,
                "endTime": 1380,
            }
        },
        schedule_type="dtg",
    )


@pytest.mark.asyncio
async def test_battery_schedule_services_update_falls_back_when_selected_schedule_missing(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.services import async_setup_services

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    coord._battery_dtg_schedule_id = "missing-id"  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {"handler": handler}

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)

    update_schedule = registered[(DOMAIN, "update_schedule")]["handler"]

    await update_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_id": "def456",
                "schedule_type": "dtg",
                "start_time": dt_time(19, 0),
                "end_time": dt_time(22, 0),
                "limit": 41,
                "days": [2, 4],
                "confirm": True,
            }
        )
    )

    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": False,
                "scheduleSupported": True,
                "startTime": 1140,
                "endTime": 1320,
            }
        },
        schedule_type="dtg",
    )


@pytest.mark.asyncio
async def test_battery_schedule_services_reject_local_overlaps_before_client_calls(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.enphase_ev.services import async_setup_services

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {"handler": handler}

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)

    add_schedule = registered[(DOMAIN, "add_schedule")]["handler"]
    update_schedule = registered[(DOMAIN, "update_schedule")]["handler"]

    with pytest.raises(
        ServiceValidationError, match="existing charge from grid schedule"
    ):
        await add_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "rbd",
                    "start_time": dt_time(2, 0),
                    "end_time": dt_time(4, 0),
                    "limit": 100,
                    "days": [1],
                }
            )
        )

    coord.client.create_battery_schedule.assert_not_awaited()

    with pytest.raises(
        ServiceValidationError, match="existing charge from grid schedule"
    ):
        await update_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "def456",
                    "schedule_type": "dtg",
                    "start_time": dt_time(2, 0),
                    "end_time": dt_time(4, 0),
                    "limit": 41,
                    "days": [1],
                    "confirm": True,
                }
            )
        )

    coord.client.update_battery_schedule.assert_not_awaited()

    await update_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_id": "abc123",
                "schedule_type": "cfg",
                "start_time": dt_time(1, 30),
                "end_time": dt_time(3, 0),
                "limit": 90,
                "days": [1, 3, 5],
                "confirm": True,
            }
        )
    )

    coord.client.update_battery_schedule.assert_awaited_once_with(
        "abc123",
        schedule_type="CFG",
        start_time="01:30",
        end_time="03:00",
        limit=90,
        days=[1, 3, 5],
        timezone="Australia/Melbourne",
        is_enabled=None,
        is_deleted=None,
    )


@pytest.mark.asyncio
async def test_battery_schedule_services_cover_failure_paths(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.enphase_ev.services import async_setup_services

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)

    start_stream = registered[(DOMAIN, "start_live_stream")]["handler"]
    add_schedule = registered[(DOMAIN, "add_schedule")]["handler"]
    update_schedule = registered[(DOMAIN, "update_schedule")]["handler"]
    delete_schedule = registered[(DOMAIN, "delete_schedule")]["handler"]
    validate_schedule = registered[(DOMAIN, "validate_schedule")]["handler"]

    coord.async_start_streaming = AsyncMock()
    await start_stream(SimpleNamespace(data={"config_entry_id": config_entry.entry_id}))
    coord.async_start_streaming.assert_awaited_once()
    with pytest.raises(ServiceValidationError):
        await start_stream(SimpleNamespace(data={"config_entry_id": "missing-entry"}))

    with pytest.raises(ServiceValidationError, match="must be different"):
        await add_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                    "start_time": dt_time(1, 0),
                    "end_time": dt_time(1, 0),
                    "limit": 50,
                    "days": [1],
                }
            )
        )

    with pytest.raises(ServiceValidationError, match="between 5 and 100"):
        await add_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                    "start_time": dt_time(1, 0),
                    "end_time": dt_time(2, 0),
                    "limit": 101,
                    "days": [1],
                }
            )
        )

    coord._battery_write_access_confirmed = False  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="editing is unavailable"):
        await add_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                    "start_time": dt_time(1, 0),
                    "end_time": dt_time(2, 0),
                    "limit": 50,
                    "days": [1],
                }
            )
        )
    coord._battery_write_access_confirmed = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001

    coord.client.create_battery_schedule = None
    with pytest.raises(ServiceValidationError, match="API is unavailable"):
        await add_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                    "start_time": dt_time(4, 0),
                    "end_time": dt_time(5, 0),
                    "limit": 50,
                    "days": [1],
                }
            )
        )
    coord.client.create_battery_schedule = AsyncMock(return_value={"status": "ok"})

    with pytest.raises(ServiceValidationError, match="Invalid schedule ID"):
        await update_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "bad id",
                    "schedule_type": "cfg",
                    "start_time": dt_time(1, 0),
                    "end_time": dt_time(2, 0),
                    "limit": 50,
                    "days": [1],
                    "confirm": True,
                }
            )
        )

    coord._battery_write_access_confirmed = False  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="editing is unavailable"):
        await update_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "abc123",
                    "schedule_type": "cfg",
                    "start_time": dt_time(1, 0),
                    "end_time": dt_time(2, 0),
                    "limit": 50,
                    "days": [1],
                    "confirm": True,
                }
            )
        )
    coord._battery_write_access_confirmed = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="Schedule ID not found"):
        await update_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "fff999",
                    "schedule_type": "cfg",
                    "start_time": dt_time(1, 0),
                    "end_time": dt_time(2, 0),
                    "limit": 50,
                    "days": [1],
                    "confirm": True,
                }
            )
        )

    coord.client.update_battery_schedule = None
    with pytest.raises(ServiceValidationError, match="API is unavailable"):
        await update_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "abc123",
                    "schedule_type": "cfg",
                    "start_time": dt_time(4, 0),
                    "end_time": dt_time(5, 0),
                    "limit": 50,
                    "days": [1],
                    "confirm": True,
                }
            )
        )
    coord.client.update_battery_schedule = AsyncMock(return_value={"status": "ok"})

    coord._battery_write_access_confirmed = False  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="editing is unavailable"):
        await delete_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "abc123",
                    "confirm": True,
                }
            )
        )
    coord._battery_write_access_confirmed = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="Confirmation required"):
        await delete_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "abc123",
                    "confirm": False,
                }
            )
        )

    with pytest.raises(ServiceValidationError, match="Provide at least one"):
        await delete_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": None,
                    "confirm": True,
                }
            )
        )

    with pytest.raises(ServiceValidationError, match="Invalid schedule ID"):
        await delete_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_ids": ["", "bad id"],
                    "confirm": True,
                }
            )
        )

    coord.client.delete_battery_schedule = None
    with pytest.raises(ServiceValidationError, match="API is unavailable"):
        await delete_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "abc123",
                    "confirm": True,
                }
            )
        )
    coord.client.delete_battery_schedule = AsyncMock(return_value={"status": "ok"})

    coord.client.validate_battery_schedule = None
    assert (
        await validate_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                }
            )
        )
        == {}
    )

    coord.client.validate_battery_schedule = AsyncMock(
        return_value={"valid": False, "message": "bad schedule"}
    )
    with pytest.raises(ServiceValidationError, match="bad schedule"):
        await validate_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                }
            )
        )

    coord.client.validate_battery_schedule = AsyncMock(
        return_value={"isValid": True, "message": "ok"}
    )
    assert await validate_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_type": "cfg",
            }
        )
    ) == {"isValid": True, "message": "ok", "valid": True}

    coord.client.validate_battery_schedule = AsyncMock(
        return_value={"isValid": False, "message": "raw invalid schedule"}
    )
    with pytest.raises(ServiceValidationError, match="raw invalid schedule"):
        await validate_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                }
            )
        )

    coord.client.validate_battery_schedule = AsyncMock(
        return_value={"isValid": "false", "message": "string invalid schedule"}
    )
    with pytest.raises(ServiceValidationError, match="string invalid schedule"):
        await validate_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                }
            )
        )

    coord.client.validate_battery_schedule = AsyncMock(return_value={"isValid": False})
    with pytest.raises(
        ServiceValidationError,
        match="Schedule rejected by the Enphase validation endpoint.",
    ):
        await validate_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                }
            )
        )

    coord.client.validate_battery_schedule = AsyncMock(
        return_value={"isValid": "true", "message": "string ok"}
    )
    assert await validate_schedule(
        SimpleNamespace(
            data={
                "config_entry_id": config_entry.entry_id,
                "schedule_type": "cfg",
            }
        )
    ) == {"isValid": "true", "message": "string ok", "valid": True}

    coord.client.validate_battery_schedule = AsyncMock(return_value=["unexpected"])
    assert (
        await validate_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                }
            )
        )
        == {}
    )

    coord.client.validate_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=Mock(real_url="https://example.invalid"),
            history=(),
            status=403,
            message="Forbidden",
        )
    )
    assert (
        await validate_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                }
            )
        )
        == {}
    )

    coord.client.validate_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=Mock(real_url="https://example.invalid"),
            history=(),
            status=500,
            message="Boom",
        )
    )
    with pytest.raises(aiohttp.ClientResponseError, match="Boom"):
        await validate_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                }
            )
        )

    coord._battery_write_access_confirmed = False  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="editing is unavailable"):
        await validate_schedule(
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                }
            )
        )


@pytest.mark.asyncio
async def test_battery_schedule_service_handlers_reraise_client_errors_when_helper_allows(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.services import async_setup_services

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    coord.battery_runtime.raise_schedule_update_validation_error = (
        MagicMock()
    )  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, *args, **kwargs):
        registered[(domain, service)] = {"handler": handler}

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    async_setup_services(hass)

    client_error = aiohttp.ClientResponseError(
        request_info=Mock(real_url="https://example.invalid"),
        history=(),
        status=403,
        message="Forbidden",
    )
    coord.client.create_battery_schedule = AsyncMock(side_effect=client_error)
    coord.client.update_battery_schedule = AsyncMock(side_effect=client_error)
    coord.client.delete_battery_schedule = AsyncMock(side_effect=client_error)

    with pytest.raises(aiohttp.ClientResponseError):
        await registered[(DOMAIN, "add_schedule")]["handler"](
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_type": "cfg",
                    "start_time": dt_time(5, 0),
                    "end_time": dt_time(7, 0),
                    "limit": 88,
                    "days": [1, 2, 3],
                }
            )
        )

    with pytest.raises(aiohttp.ClientResponseError):
        await registered[(DOMAIN, "update_schedule")]["handler"](
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_id": "abc123",
                    "schedule_type": "cfg",
                    "start_time": dt_time(6, 0),
                    "end_time": dt_time(8, 0),
                    "limit": 91,
                    "days": [1, 5],
                    "confirm": True,
                }
            )
        )

    with pytest.raises(aiohttp.ClientResponseError):
        await registered[(DOMAIN, "delete_schedule")]["handler"](
            SimpleNamespace(
                data={
                    "config_entry_id": config_entry.entry_id,
                    "schedule_ids": ["abc123"],
                    "confirm": True,
                }
            )
        )


def test_runtime_data_supports_battery_schedule_editor_field() -> None:
    runtime_data = EnphaseRuntimeData(coordinator=SimpleNamespace(site_id="site"))

    assert runtime_data.battery_schedule_editor is None
    assert runtime_data.coordinator.site_id == "site"
