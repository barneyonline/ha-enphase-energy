from __future__ import annotations

from datetime import time as dt_time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.enphase_ev.battery_schedule_editor import (
    BatteryScheduleEditorManager,
)
from custom_components.enphase_ev.const import (
    OPT_BATTERY_SCHEDULES_ENABLED,
    OPT_SCHEDULE_SYNC_ENABLED,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.time import (
    BatteryScheduleEditEndTimeEntity,
    BatteryScheduleEditStartTimeEntity,
    async_setup_entry,
)

from .device_info_helpers import device_info_fields


def _schedule_payload() -> dict[str, object]:
    return {
        "cfg": {
            "scheduleStatus": "active",
            "details": [
                {
                    "scheduleId": "cfg-1",
                    "startTime": "02:00:00",
                    "endTime": "05:00:00",
                    "limit": 80,
                    "days": [1, 2, 3],
                    "timezone": "Australia/Melbourne",
                    "isEnabled": True,
                }
            ],
        }
    }


def _prepare_editor_coord(coord) -> BatteryScheduleEditorManager:
    payload = _schedule_payload()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_write_access_confirmed = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.battery_schedules = AsyncMock(return_value=payload)
    coord.client.create_battery_schedule = AsyncMock(return_value={"status": "ok"})
    coord.client.update_battery_schedule = AsyncMock(return_value={"status": "ok"})
    coord.client.delete_battery_schedule = AsyncMock(return_value={"status": "ok"})
    coord._battery_schedules_payload = payload  # noqa: SLF001
    coord.parse_battery_schedules_payload(payload)
    editor = BatteryScheduleEditorManager(coord)
    editor.sync_from_coordinator()
    return editor


def _attach_runtime(
    config_entry, coord, editor: BatteryScheduleEditorManager | None
) -> None:
    object.__setattr__(
        config_entry,
        "options",
        {
            OPT_BATTERY_SCHEDULES_ENABLED: editor is not None,
            OPT_SCHEDULE_SYNC_ENABLED: False,
        },
    )
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        battery_schedule_editor=editor,
    )


def test_time_type_available_uses_inventory_view() -> None:
    from custom_components.enphase_ev import time as time_mod

    coord = SimpleNamespace(
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "encharge"
        )
    )

    assert time_mod._type_available(coord, "encharge") is True
    assert time_mod._type_available(coord, "envoy") is False


def test_retained_site_time_unique_ids_follow_scheduler_and_client_support() -> None:
    from custom_components.enphase_ev import time as time_mod

    coord = SimpleNamespace(
        site_id="site",
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "encharge"
        ),
        client=SimpleNamespace(),
    )

    assert (
        time_mod._retained_site_time_unique_ids(
            coord, SimpleNamespace(options={OPT_BATTERY_SCHEDULES_ENABLED: False})
        )
        == set()
    )

    coord.inventory_view.has_type_for_entities = lambda _type_key: False
    assert (
        time_mod._retained_site_time_unique_ids(
            coord, SimpleNamespace(options={OPT_BATTERY_SCHEDULES_ENABLED: True})
        )
        == set()
    )

    coord.inventory_view.has_type_for_entities = lambda type_key: type_key == "encharge"
    coord.client.battery_schedules = lambda: None
    coord.client.create_battery_schedule = lambda: None
    coord.client.update_battery_schedule = lambda: None
    coord.client.delete_battery_schedule = lambda: None
    assert time_mod._retained_site_time_unique_ids(
        coord, SimpleNamespace(options={OPT_BATTERY_SCHEDULES_ENABLED: True})
    ) == {
        "enphase_ev_site_site_battery_schedule_edit_start_time",
        "enphase_ev_site_site_battery_schedule_edit_end_time",
    }


@pytest.mark.asyncio
async def test_async_setup_entry_adds_generic_time_entities_when_scheduler_enabled(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    editor = _prepare_editor_coord(coord)
    _attach_runtime(config_entry, coord, editor)

    added: list = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert any(isinstance(ent, BatteryScheduleEditStartTimeEntity) for ent in added)
    assert any(isinstance(ent, BatteryScheduleEditEndTimeEntity) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_hides_time_entities_when_scheduler_disabled(
    hass, config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    _attach_runtime(config_entry, coord, None)

    added: list = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert added == []


@pytest.mark.asyncio
async def test_async_setup_entry_uses_listener_fallback_when_topology_missing(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    editor = _prepare_editor_coord(coord)
    _attach_runtime(config_entry, coord, editor)
    callbacks: list = []

    monkeypatch.setattr(coord, "async_add_topology_listener", None, raising=False)
    monkeypatch.setattr(
        coord,
        "async_add_listener",
        lambda callback: callbacks.append(callback) or (lambda: None),
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert callbacks


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_legacy_time_entities_when_inventory_ready(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory()
    editor = _prepare_editor_coord(coord)
    _attach_runtime(config_entry, coord, editor)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "time",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_charge_from_grid_start_time",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    remove_spy.assert_called_with(stale.entity_id)


def test_time_entities_expose_editor_values_and_device_info_fallback(
    config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    editor = _prepare_editor_coord(coord)
    coord.inventory_view.type_device_info = lambda _type_key: None
    _attach_runtime(config_entry, coord, editor)

    start = BatteryScheduleEditStartTimeEntity(coord, config_entry)
    end = BatteryScheduleEditEndTimeEntity(coord, config_entry)

    assert start.available is True
    assert start.native_value == dt_time(2, 0)
    assert end.native_value == dt_time(5, 0)
    assert device_info_fields(start.device_info)["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:encharge")
    }


@pytest.mark.asyncio
async def test_time_entities_use_type_device_info_and_update_editor(
    config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    editor = _prepare_editor_coord(coord)
    expected = {"identifiers": {("enphase_ev", "provided")}}
    coord.inventory_view.type_device_info = MagicMock(return_value=expected)
    _attach_runtime(config_entry, coord, editor)

    start = BatteryScheduleEditStartTimeEntity(coord, config_entry)
    end = BatteryScheduleEditEndTimeEntity(coord, config_entry)

    assert start.device_info is expected
    await start.async_set_value(dt_time(6, 45))
    await end.async_set_value(dt_time(7, 15))

    assert editor.edit.start_time == "06:45"
    assert editor.edit.end_time == "07:15"


def test_time_entities_unavailable_without_editor_or_write_access(
    config_entry, coordinator_factory
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_write_access_confirmed = False  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.client.battery_schedules = AsyncMock()
    coord.client.create_battery_schedule = AsyncMock()
    coord.client.update_battery_schedule = AsyncMock()
    coord.client.delete_battery_schedule = AsyncMock()
    _attach_runtime(config_entry, coord, None)

    start = BatteryScheduleEditStartTimeEntity(coord, config_entry)

    assert start.available is False
