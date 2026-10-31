"""Scheduler acknowledgements may omit readback and concurrency metadata."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.enphase_ev.api import EnphaseLoginWallUnauthorized
from custom_components.enphase_ev.battery_schedule_editor import (
    BatteryScheduleEditorManager,
    editor_days_from_list as battery_days,
)
from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.evse_schedule_editor import (
    EvseScheduleEditorManager,
    editor_days_from_list as evse_days,
)
from custom_components.enphase_ev.schedule_sync import ScheduleSync
from tests.components.enphase_ev.test_schedule_sync import _slot


@pytest.mark.parametrize(
    "operation", ["create", "replace", "delete", "patch", "toggle"]
)
@pytest.mark.parametrize(
    "response",
    [
        None,
        {"data": {}},
        {"data": {"slots": []}},
        {"data": []},
        {
            "meta": {"serverTimeStamp": "outer"},
            "data": {"meta": {"serverTimeStamp": "inner"}},
        },
    ],
)
async def test_schedule_mutations_accept_sparse_acknowledgements(
    hass, coordinator_factory, operation, response
):
    coord = coordinator_factory()
    sync = ScheduleSync(hass, coord)
    sync._slot_cache = {"EV": {"slot": _slot("slot")}}
    sync._meta_cache["EV"] = "previous"
    endpoint = {
        "create": "create_schedule",
        "replace": "patch_schedules",
        "delete": "delete_schedule",
        "patch": "patch_schedule",
        "toggle": "patch_schedule_states",
    }[operation]
    request = AsyncMock(return_value=response)
    setattr(coord.client, endpoint, request)
    try:
        if operation == "create":
            result = await sync._create_slot("EV", _slot(""))
        elif operation == "replace":
            result = await sync._async_replace_slots("EV", [_slot("slot")])
        elif operation == "delete":
            result = await sync._async_delete_slot("EV", "slot")
        elif operation == "patch":
            result = await sync._patch_slot("EV", "slot", _slot("slot"))
        else:
            result = await sync._async_set_slot_enabled("EV", "slot", False)
        assert result is True
        request.assert_awaited_once()
        if isinstance(response, dict) and "meta" in response:
            assert sync._meta_cache["EV"] == "outer"
    finally:
        await sync.async_stop()


async def test_schedule_toggle_tolerates_cache_replaced_while_request_pending(
    hass, coordinator_factory
):
    coord = coordinator_factory()
    sync = ScheduleSync(hass, coord)
    sync._slot_cache = {"EV": {"slot": _slot("slot")}}

    async def replace_cache(serial, *, slot_states):
        assert slot_states == {"slot": False}
        sync._slot_cache = {}
        return {"data": {"slots": []}}

    coord.client.patch_schedule_states = replace_cache
    try:
        assert await sync._async_set_slot_enabled("EV", "slot", False)
        assert sync._slot_cache == {}
    finally:
        await sync.async_stop()


async def test_recent_schedule_sync_does_not_refresh(hass):
    sync = ScheduleSync(hass, SimpleNamespace())
    sync._last_sync = datetime.now(timezone.utc)
    sync.async_refresh = AsyncMock()
    await sync._refresh_if_stale()
    sync.async_refresh.assert_not_awaited()


async def test_schedule_cleanup_removes_malformed_owned_helper_ids(hass, config_entry):
    sync = ScheduleSync(hass, SimpleNamespace())
    record = er.async_get(hass).async_get_or_create(
        "schedule", "schedule", f"{DOMAIN}:broken", config_entry=config_entry
    )
    collection = SimpleNamespace(
        data={f"{DOMAIN}:broken": {}}, async_delete_item=AsyncMock()
    )
    sync._storage_collection = collection
    await sync._remove_all_helpers()
    assert er.async_get(hass).async_get(record.entity_id) is None
    collection.async_delete_item.assert_awaited_once_with(f"{DOMAIN}:broken")


def test_scheduler_optional_host_callbacks_are_not_required(hass):
    sync = ScheduleSync(hass, SimpleNamespace())
    sync._mark_scheduler_available()
    sync._note_scheduler_unavailable(RuntimeError("temporary"))
    sync._note_login_wall_unauthorized(
        EnphaseLoginWallUnauthorized(
            endpoint="/schedule", request_label="GET /schedule"
        )
    )
    assert sync._last_status == "auth_failed"


async def test_async_scheduler_bearer_future_is_not_used_as_token(hass):
    future = asyncio.get_running_loop().create_future()
    future.set_result("token")
    sync = ScheduleSync(
        hass, SimpleNamespace(client=SimpleNamespace(scheduler_bearer=lambda: future))
    )
    assert not sync._has_scheduler_bearer()


@pytest.mark.parametrize(
    "manager_class", [BatteryScheduleEditorManager, EvseScheduleEditorManager]
)
def test_editor_listener_unsubscription_is_idempotent(manager_class):
    manager = manager_class(SimpleNamespace())
    listener = Mock()
    unsubscribe = manager.async_add_listener(listener)
    unsubscribe()
    unsubscribe()
    manager._notify_listeners()
    listener.assert_not_called()


def test_schedule_listener_unsubscription_is_idempotent(hass):
    sync = ScheduleSync(hass, SimpleNamespace())
    listener = Mock()
    unsubscribe = sync.async_add_listener(listener)
    unsubscribe()
    unsubscribe()
    sync._notify_listeners()
    listener.assert_not_called()


@pytest.mark.parametrize("normalize", [battery_days, evse_days])
def test_editor_day_flags_ignore_unknown_indices(normalize):
    assert not any(normalize([-1, 8, 99]).values())


def test_evse_editor_without_serial_and_cache_extensions():
    manager = EvseScheduleEditorManager(
        SimpleNamespace(schedule_sync=SimpleNamespace(_slot_cache=None))
    )
    manager.sync_from_coordinator()
    assert manager.schedules_by_serial == {}


@pytest.mark.parametrize("kind", ["battery", "evse"])
async def test_editor_entity_lifecycle_without_editor(
    hass, config_entry, coordinator_factory, monkeypatch, kind
):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity
    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
    from custom_components.enphase_ev.battery_schedule_editor import (
        BatteryScheduleEditorEntity,
    )
    from custom_components.enphase_ev.evse_schedule_editor import (
        EvseScheduleEditorEntity,
    )

    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())
    monkeypatch.setattr(CoordinatorEntity, "async_will_remove_from_hass", AsyncMock())
    if kind == "battery":
        entity = BatteryScheduleEditorEntity(coord, config_entry)
    else:
        entity = EvseScheduleEditorEntity(coord, config_entry, "EV")
    entity.hass = hass
    await entity.async_added_to_hass()
    await entity.async_will_remove_from_hass()
    assert entity._editor_unsub is None
