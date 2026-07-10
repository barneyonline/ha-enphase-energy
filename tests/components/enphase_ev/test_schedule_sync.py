from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.schedule.const import (
    CONF_FROM,
    CONF_THURSDAY,
    CONF_TO,
)
from homeassistant.components.schedule.const import DOMAIN as SCHEDULE_DOMAIN
from homeassistant.components import websocket_api
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.enphase_ev.api import (
    EnphaseLoginWallUnauthorized,
    SchedulerUnavailable,
)
from custom_components.enphase_ev.const import DOMAIN, OPT_SCHEDULE_SYNC_ENABLED
from custom_components.enphase_ev import schedule_sync as schedule_sync_mod
from custom_components.enphase_ev.schedule_sync import ScheduleSync
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


class DummyCoordinator(SimpleNamespace):
    def __init__(self, hass, client, entry, data):
        super().__init__()
        self.hass = hass
        self.client = client
        self.config_entry = entry
        self.data = data
        self.serials = {RANDOM_SERIAL}
        self._listener = None
        self.scheduler_last_error = None
        if not hasattr(self.client, "scheduler_bearer"):
            legacy_bearer = getattr(self.client, "_bearer", None)
            if callable(legacy_bearer):
                self.client.scheduler_bearer = legacy_bearer
            else:
                self.client.scheduler_bearer = lambda: None
        if not hasattr(self.client, "has_scheduler_bearer"):
            self.client.has_scheduler_bearer = lambda: bool(
                self.client.scheduler_bearer()
            )
        if not hasattr(self.client, "control_headers"):
            self.client.control_headers = lambda: (
                {"Authorization": f"Bearer {token}"}
                if (token := self.client.scheduler_bearer())
                else {}
            )

    def iter_serials(self):
        return [RANDOM_SERIAL]

    def async_add_listener(self, cb):
        self._listener = cb

        def _unsub():
            self._listener = None

        return _unsub

    def scheduler_backoff_active(self) -> bool:
        backoff_active = getattr(self, "_scheduler_backoff_active", None)
        if not callable(backoff_active):
            return False
        return bool(backoff_active())

    def mark_scheduler_available(self) -> None:
        mark_available = getattr(self, "_mark_scheduler_available", None)
        if callable(mark_available):
            mark_available()

    def note_scheduler_unavailable(self, err: Exception) -> None:
        note_unavailable = getattr(self, "_note_scheduler_unavailable", None)
        if callable(note_unavailable):
            note_unavailable(err)


def _slot(slot_id: str, **overrides):
    base = {
        "id": slot_id,
        "startTime": "08:00",
        "endTime": "09:00",
        "days": [1],
        "scheduleType": "CUSTOM",
        "enabled": True,
        "remindFlag": False,
        "remindTime": None,
        "chargingLevel": 32,
        "chargingLevelAmp": 32,
        "recurringKind": "Recurring",
        "chargeLevelType": "Weekly",
        "sourceType": "SYSTEM",
    }
    base.update(overrides)
    return base


def _reset_schedule_storage(path: Path) -> None:
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


async def _setup_sync(hass, entry, payload):
    entry.add_to_hass(hass)
    options = dict(entry.options or {})
    if OPT_SCHEDULE_SYNC_ENABLED not in options:
        options[OPT_SCHEDULE_SYNC_ENABLED] = True
        hass.config_entries.async_update_entry(entry, options=options)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedule = AsyncMock()
    client.patch_schedule_states = AsyncMock()
    coord = DummyCoordinator(
        hass,
        client,
        entry,
        data={RANDOM_SERIAL: {"display_name": "Garage Charger"}},
    )
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)
    return sync, client


@pytest.mark.asyncio
async def test_schedule_sync_skips_when_scheduler_unavailable(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(return_value=payload)
    coord = DummyCoordinator(
        hass,
        client,
        entry,
        data={RANDOM_SERIAL: {"display_name": "Garage Charger"}},
    )
    coord._scheduler_backoff_active = lambda: True  # noqa: SLF001
    coord.scheduler_last_error = "scheduler down"

    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    assert sync._last_status == "scheduler_unavailable"
    assert sync._last_error == "scheduler down"
    client.get_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_set_slot_enabled_skips_when_scheduler_backoff(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._scheduler_backoff_active = lambda: True  # noqa: SLF001
    coord.scheduler_last_error = "scheduler down"
    sync._slot_cache = {
        RANDOM_SERIAL: {
            "slot-1": {
                "id": "slot-1",
                "scheduleType": "CUSTOM",
                "enabled": True,
                "startTime": "08:00",
                "endTime": "09:00",
            }
        }
    }

    await sync.async_set_slot_enabled(RANDOM_SERIAL, "slot-1", False)

    assert sync._last_status == "scheduler_unavailable"
    assert sync._last_error == "scheduler down"
    client.patch_schedule_states.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_set_slot_enabled_marks_scheduler_available(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._mark_scheduler_available = MagicMock()  # noqa: SLF001
    sync._slot_cache = {
        RANDOM_SERIAL: {
            "slot-1": {
                "id": "slot-1",
                "scheduleType": "CUSTOM",
                "enabled": True,
                "startTime": "08:00",
                "endTime": "09:00",
            }
        }
    }
    client.patch_schedule_states = AsyncMock(return_value={"meta": {}})

    await sync.async_set_slot_enabled(RANDOM_SERIAL, "slot-1", False)

    coord._mark_scheduler_available.assert_called_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_set_slot_enabled_handles_scheduler_unavailable(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._note_scheduler_unavailable = MagicMock()  # noqa: SLF001
    sync._slot_cache = {
        RANDOM_SERIAL: {
            "slot-1": {
                "id": "slot-1",
                "scheduleType": "CUSTOM",
                "enabled": True,
                "startTime": "08:00",
                "endTime": "09:00",
            }
        }
    }
    client.patch_schedule_states = AsyncMock(side_effect=SchedulerUnavailable("down"))

    await sync.async_set_slot_enabled(RANDOM_SERIAL, "slot-1", False)

    assert sync._last_status == "scheduler_unavailable"
    assert sync._last_error == "down"
    coord._note_scheduler_unavailable.assert_called_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_set_slot_enabled_activates_auth_block_on_login_wall(
    hass,
) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    err = EnphaseLoginWallUnauthorized(
        endpoint="/service/evse_scheduler/test/states",
        request_label="PATCH scheduler states",
        status=200,
        content_type="application/json; charset=utf-8",
    )
    coord._activate_auth_block_from_login_wall = MagicMock(
        return_value=True
    )  # noqa: SLF001
    sync._slot_cache = {
        RANDOM_SERIAL: {
            "slot-1": {
                "id": "slot-1",
                "scheduleType": "CUSTOM",
                "enabled": True,
                "startTime": "08:00",
                "endTime": "09:00",
            }
        }
    }
    client.patch_schedule_states = AsyncMock(side_effect=err)

    await sync.async_set_slot_enabled(RANDOM_SERIAL, "slot-1", False)

    assert sync._last_status == "auth_blocked"
    assert "Enphase login wall returned HTML" in sync._last_error
    coord._activate_auth_block_from_login_wall.assert_called_once_with(
        err
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_patch_slot_respects_scheduler_backoff(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, _client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._scheduler_backoff_active = lambda: True  # noqa: SLF001
    coord.scheduler_last_error = "down"
    sync._slot_cache = {
        RANDOM_SERIAL: {
            "slot-1": {
                "id": "slot-1",
                "scheduleType": "CUSTOM",
                "startTime": "08:00",
                "endTime": "09:00",
            }
        }
    }

    await sync._patch_slot(RANDOM_SERIAL, "slot-1", {"startTime": "09:00"})

    assert sync._last_status == "scheduler_unavailable"
    assert sync._last_error == "down"


@pytest.mark.asyncio
async def test_patch_slot_handles_scheduler_unavailable(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._note_scheduler_unavailable = MagicMock()  # noqa: SLF001
    client.patch_schedule = AsyncMock(side_effect=SchedulerUnavailable("down"))
    sync._slot_cache = {
        RANDOM_SERIAL: {
            "slot-1": {
                "id": "slot-1",
                "scheduleType": "CUSTOM",
                "startTime": "08:00",
                "endTime": "09:00",
            }
        }
    }

    await sync._patch_slot(RANDOM_SERIAL, "slot-1", {"startTime": "09:00"})

    assert sync._last_status == "scheduler_unavailable"
    assert sync._last_error == "down"
    coord._note_scheduler_unavailable.assert_called_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_patch_slot_activates_auth_block_on_login_wall(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    err = EnphaseLoginWallUnauthorized(
        endpoint="/service/evse_scheduler/test/slot",
        request_label="PATCH scheduler slot",
        status=200,
        content_type="application/json; charset=utf-8",
    )
    coord._activate_auth_block_from_login_wall = MagicMock(
        return_value=True
    )  # noqa: SLF001
    client.patch_schedule = AsyncMock(side_effect=err)
    sync._slot_cache = {
        RANDOM_SERIAL: {
            "slot-1": {
                "id": "slot-1",
                "scheduleType": "CUSTOM",
                "startTime": "08:00",
                "endTime": "09:00",
            }
        }
    }

    await sync._patch_slot(RANDOM_SERIAL, "slot-1", {"startTime": "09:00"})

    assert sync._last_status == "auth_blocked"
    coord._activate_auth_block_from_login_wall.assert_called_once_with(
        err
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_patch_slot_marks_scheduler_available(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._mark_scheduler_available = MagicMock()  # noqa: SLF001
    client.patch_schedule = AsyncMock(return_value={"meta": {}})
    sync._slot_cache = {
        RANDOM_SERIAL: {
            "slot-1": {
                "id": "slot-1",
                "scheduleType": "CUSTOM",
                "startTime": "08:00",
                "endTime": "09:00",
            }
        }
    }

    await sync._patch_slot(RANDOM_SERIAL, "slot-1", {"startTime": "09:00"})

    coord._mark_scheduler_available.assert_called_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_sync_serial_handles_scheduler_unavailable(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._note_scheduler_unavailable = MagicMock()  # noqa: SLF001
    client.get_schedules = AsyncMock(side_effect=SchedulerUnavailable("down"))

    await sync._sync_serial(RANDOM_SERIAL)

    assert sync._last_status == "scheduler_unavailable"
    assert sync._last_error == "down"
    coord._note_scheduler_unavailable.assert_called_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_sync_serial_activates_auth_block_on_login_wall(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    err = EnphaseLoginWallUnauthorized(
        endpoint="/service/evse_scheduler/test/schedules",
        request_label="GET scheduler schedules",
        status=200,
        content_type="application/json; charset=utf-8",
    )
    coord._activate_auth_block_from_login_wall = MagicMock(
        return_value=True
    )  # noqa: SLF001
    client.get_schedules = AsyncMock(side_effect=err)

    await sync._sync_serial(RANDOM_SERIAL)

    assert sync._last_status == "auth_blocked"
    assert "Enphase login wall returned HTML" in sync._last_error
    coord._activate_auth_block_from_login_wall.assert_called_once_with(
        err
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_refresh_preserves_auth_block_status_on_login_wall(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    err = EnphaseLoginWallUnauthorized(
        endpoint="/service/evse_scheduler/test/schedules",
        request_label="GET scheduler schedules",
        status=200,
        content_type="application/json; charset=utf-8",
    )
    coord._activate_auth_block_from_login_wall = MagicMock(
        return_value=True
    )  # noqa: SLF001
    client.get_schedules = AsyncMock(side_effect=err)

    await sync.async_refresh(reason="startup", serials=[RANDOM_SERIAL])

    assert sync._last_status == "auth_blocked"
    assert sync._last_sync is not None
    coord._activate_auth_block_from_login_wall.assert_called_once_with(
        err
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_sync_serial_marks_scheduler_available(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._mark_scheduler_available = MagicMock()  # noqa: SLF001
    client.get_schedules = AsyncMock(return_value=payload)

    await sync._sync_serial(RANDOM_SERIAL)

    coord._mark_scheduler_available.assert_called_once()  # noqa: SLF001


@pytest.fixture(autouse=True)
async def _cleanup_schedule_sync(hass):
    yield
    for sync in hass.data.pop("enphase_ev_schedule_syncs", []):
        await sync.async_stop()


@pytest.mark.asyncio
async def test_schedule_sync_async_start_listener_error_when_enabled(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedule = AsyncMock()

    class BrokenCoordinator(DummyCoordinator):
        def async_add_listener(self, _cb):
            raise RuntimeError("boom")

    coord = BrokenCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    assert sync._unsub_coordinator is None


def test_schedule_sync_notify_listeners_handles_exception(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    calls: list[str] = []

    def _bad() -> None:
        raise RuntimeError("boom")

    def _good() -> None:
        calls.append("ok")

    sync.async_add_listener(_bad)
    sync.async_add_listener(_good)

    sync._notify_listeners()

    assert calls == ["ok"]


@pytest.mark.asyncio
async def test_schedule_sync_post_patch_refresh_dedupes(hass, monkeypatch) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    callbacks: list[callable] = []
    created: list[bool] = []
    names: list[str | None] = []

    def _fake_call_later(_hass, _delay, action):
        callbacks.append(action)
        return lambda: None

    def _create_task(coro, *, name=None):
        created.append(True)
        names.append(name)
        coro.close()

    monkeypatch.setattr(schedule_sync_mod, "async_call_later", _fake_call_later)
    monkeypatch.setattr(hass, "async_create_task", _create_task)
    sync.async_refresh = AsyncMock()

    sync._schedule_post_patch_refresh(RANDOM_SERIAL)
    sync._schedule_post_patch_refresh(RANDOM_SERIAL)

    assert len(callbacks) == 1
    callbacks[0](None)

    assert created == [True]
    assert len(names) == 1
    assert RANDOM_SERIAL not in str(names[0])
    assert "..." in str(names[0])
    assert RANDOM_SERIAL not in sync._pending_patch_refresh
    assert sync._pending_patch_refresh_tasks == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_schedule_sync_disable_support_noop_when_already_done(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    sync._disabled_cleanup_done = True
    sync._remove_all_helpers = AsyncMock()

    await sync._disable_support()

    sync._remove_all_helpers.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_refresh_caches_slots_without_helpers(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-1"
    off_peak_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-2"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [
            _slot(slot_id),
            _slot(
                off_peak_id,
                scheduleType="OFF_PEAK",
                startTime=None,
                endTime=None,
                enabled=False,
            ),
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)
    assert sync._slot_cache[RANDOM_SERIAL][slot_id]["startTime"] == "08:00"
    assert sync._slot_cache[RANDOM_SERIAL][off_peak_id]["scheduleType"] == "OFF_PEAK"
    diag = sync.diagnostics()
    assert diag["enabled"] is True


@pytest.mark.asyncio
async def test_schedule_sync_listener_notified_on_refresh(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-listener"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    calls: list[str] = []

    def _listener() -> None:
        calls.append("ping")

    unsub = sync.async_add_listener(_listener)
    await sync.async_refresh(reason="manual")
    assert calls
    unsub()
    count = len(calls)
    await sync.async_refresh(reason="manual")
    assert len(calls) == count


@pytest.mark.asyncio
async def test_schedule_sync_refresh_fetches_serials_concurrently(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    release = asyncio.Event()
    both_started = asyncio.Event()
    started: list[str] = []

    async def _get_schedules(sn: str):
        started.append(sn)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        return {
            "meta": {"serverTimeStamp": f"{sn}-ts"},
            "config": {},
            "slots": [_slot(f"{sn}:slot-1")],
        }

    client.get_schedules = AsyncMock(side_effect=_get_schedules)
    coord = DummyCoordinator(
        hass,
        client,
        entry,
        data={
            RANDOM_SERIAL: {"display_name": "Garage Charger"},
            "SECONDARY": {"display_name": "Driveway Charger"},
        },
    )
    coord.iter_serials = lambda: [RANDOM_SERIAL, "SECONDARY"]  # type: ignore[assignment]
    sync = ScheduleSync(hass, coord, entry)

    refresh_task = asyncio.create_task(sync.async_refresh(reason="manual"))
    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    await refresh_task

    assert set(started) == {RANDOM_SERIAL, "SECONDARY"}
    assert set(sync._slot_cache) == {RANDOM_SERIAL, "SECONDARY"}
    assert sync._last_status == "ok:manual"


@pytest.mark.asyncio
async def test_schedule_sync_removes_helper_when_slot_removed(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-3"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    client.get_schedules = AsyncMock(
        return_value={
            "meta": {"serverTimeStamp": "2025-01-02T00:00:00.000+00:00"},
            "config": {},
            "slots": [],
        }
    )
    await sync.async_refresh(reason="test")
    assert sync._slot_cache[RANDOM_SERIAL] == {}


@pytest.mark.asyncio
async def test_schedule_sync_disabled_skips_refresh(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-7"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: False},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedule = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    client.get_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_disable_support_removes_entities(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-disable"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    schedule_unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    ent_reg = er.async_get(hass)
    schedule_entity = ent_reg.async_get_or_create(
        SCHEDULE_DOMAIN,
        SCHEDULE_DOMAIN,
        schedule_unique_id,
        config_entry=entry,
        suggested_object_id="enphase_schedule_disable",
    )
    assert schedule_entity is not None
    assert sync._storage_collection is not None
    sync._storage_collection.data[schedule_unique_id] = {"name": "Legacy schedule"}

    switch_unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}:enabled"
    ent_reg.async_get_or_create(
        "switch",
        DOMAIN,
        switch_unique_id,
        suggested_object_id="enphase_schedule_enabled",
    )
    assert ent_reg.async_get_entity_id("switch", DOMAIN, switch_unique_id) is not None

    initial_calls = client.get_schedules.await_count
    hass.config_entries.async_update_entry(
        entry, options={OPT_SCHEDULE_SYNC_ENABLED: False}
    )
    await sync.async_refresh(reason="manual")

    assert (
        ent_reg.async_get_entity_id(
            SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, schedule_unique_id
        )
        is None
    )
    assert ent_reg.async_get_entity_id("switch", DOMAIN, switch_unique_id) is None
    assert sync._storage_collection is not None
    assert not any(
        item_id.startswith(f"{DOMAIN}:") for item_id in sync._storage_collection.data
    )
    assert client.get_schedules.await_count == initial_calls


@pytest.mark.asyncio
async def test_schedule_sync_disable_support_removes_orphaned_switches(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: False},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})

    ent_reg = er.async_get(hass)
    off_peak_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-off-peak-orphan"
    switch_unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{off_peak_id}:enabled"
    ent_reg.async_get_or_create(
        "switch",
        DOMAIN,
        switch_unique_id,
        suggested_object_id="enphase_schedule_off_peak",
    )
    assert ent_reg.async_get_entity_id("switch", DOMAIN, switch_unique_id) is not None

    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    assert ent_reg.async_get_entity_id("switch", DOMAIN, switch_unique_id) is None


@pytest.mark.asyncio
async def test_schedule_sync_remove_all_helpers_filters_registry_entries(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    entry.add_to_hass(hass)

    class ExplodingCoordinator(SimpleNamespace):
        def __init__(self):
            super().__init__()
            self.serials = {"KNOWN"}

        def iter_serials(self):
            raise RuntimeError("boom")

    coord = ExplodingCoordinator()
    sync = ScheduleSync(hass, coord, entry)
    sync._ensure_storage_collection = AsyncMock(return_value=None)

    ent_reg = er.async_get(hass)
    ent_reg.entities["light.fake"] = SimpleNamespace(
        id="light.fake",
        entity_id="light.fake",
        unique_id="",
        platform=None,
        config_entry_id=entry.entry_id,
        domain=None,
        device_id=None,
        area_id=None,
        labels=set(),
        disabled_by=None,
    )
    ent_reg.entities["switch.other_platform"] = SimpleNamespace(
        id="switch.other_platform",
        entity_id="switch.other_platform",
        unique_id="ignored",
        platform="other",
        config_entry_id=entry.entry_id,
        domain="switch",
        device_id=None,
        area_id=None,
        labels=set(),
        disabled_by=None,
    )
    ent_reg.entities["switch.bad_unique"] = SimpleNamespace(
        id="switch.bad_unique",
        entity_id="switch.bad_unique",
        unique_id="bad",
        platform=DOMAIN,
        config_entry_id=entry.entry_id,
        domain="switch",
        device_id=None,
        area_id=None,
        labels=set(),
        disabled_by=None,
    )
    ent_reg.entities["switch.mismatch_entry"] = SimpleNamespace(
        id="switch.mismatch_entry",
        entity_id="switch.mismatch_entry",
        unique_id=f"{DOMAIN}:KNOWN:schedule:slot-1:enabled",
        platform=DOMAIN,
        config_entry_id="other",
        domain="switch",
        device_id=None,
        area_id=None,
        labels=set(),
        disabled_by=None,
    )
    ent_reg.entities["switch.parse_fail"] = SimpleNamespace(
        id="switch.parse_fail",
        entity_id="switch.parse_fail",
        unique_id=f"{DOMAIN}::schedule::enabled",
        platform=DOMAIN,
        config_entry_id=entry.entry_id,
        domain="switch",
        device_id=None,
        area_id=None,
        labels=set(),
        disabled_by=None,
    )
    ent_reg.entities["switch.unknown_serial"] = SimpleNamespace(
        id="switch.unknown_serial",
        entity_id="switch.unknown_serial",
        unique_id=f"{DOMAIN}:UNKNOWN:schedule:slot-1:enabled",
        platform=DOMAIN,
        config_entry_id=entry.entry_id,
        domain="switch",
        device_id=None,
        area_id=None,
        labels=set(),
        disabled_by=None,
    )
    ent_reg.entities["schedule.bad_unique"] = SimpleNamespace(
        id="schedule.bad_unique",
        entity_id="schedule.bad_unique",
        unique_id="legacy_schedule_helper",
        platform=SCHEDULE_DOMAIN,
        config_entry_id=entry.entry_id,
        domain=SCHEDULE_DOMAIN,
        device_id=None,
        area_id=None,
        labels=set(),
        disabled_by=None,
    )
    ent_reg.entities["schedule.mismatch_entry"] = SimpleNamespace(
        id="schedule.mismatch_entry",
        entity_id="schedule.mismatch_entry",
        unique_id=f"{DOMAIN}:KNOWN:schedule:slot-legacy",
        platform=SCHEDULE_DOMAIN,
        config_entry_id="other",
        domain=SCHEDULE_DOMAIN,
        device_id=None,
        area_id=None,
        labels=set(),
        disabled_by=None,
    )

    await sync._remove_all_helpers()


@pytest.mark.asyncio
async def test_schedule_sync_remove_helpers_uses_registry_fallback(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-fallback"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}:{RANDOM_SERIAL}:schedule:{slot_id}"
    ent_reg.async_get_or_create(
        SCHEDULE_DOMAIN,
        SCHEDULE_DOMAIN,
        unique_id,
        config_entry=entry,
        suggested_object_id="enphase_schedule_cleanup",
    )

    collection = sync._storage_collection
    collection.data[123] = {"name": "bad"}
    collection.data["other:slot"] = {"name": "bad"}

    await sync._remove_all_helpers()

    assert (
        ent_reg.async_get_entity_id(SCHEDULE_DOMAIN, SCHEDULE_DOMAIN, unique_id) is None
    )
    assert 123 in collection.data
    assert "other:slot" in collection.data


@pytest.mark.asyncio
async def test_schedule_sync_missing_bearer_skips_refresh(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-8"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: None
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedule = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    client.get_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_missing_bearer_sets_status(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client.get_schedules = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)

    await sync.async_refresh(reason="manual")

    assert sync._last_status == "missing_bearer"
    client.get_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_bearer_exception_skips_refresh(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()

    def _raise():
        raise RuntimeError("boom")

    client._bearer = _raise
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedule = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    client.get_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_callbacks_trigger_refresh(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-9"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)
    sync.async_refresh = AsyncMock()

    sync._handle_interval()
    await hass.async_block_till_done()
    assert sync.async_refresh.await_count >= 1

    sync._last_sync = None
    await sync._refresh_if_stale()
    assert sync.async_refresh.await_count >= 2

    sync._last_sync = dt_util.utcnow() - timedelta(minutes=10)
    sync._handle_coordinator_update()
    await hass.async_block_till_done()
    assert sync.async_refresh.await_count >= 3


@pytest.mark.asyncio
async def test_schedule_sync_fresh_coordinator_update_does_not_create_task(
    hass, monkeypatch
) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)
    create_task = MagicMock()
    monkeypatch.setattr(hass, "async_create_task", create_task)
    sync._last_sync = dt_util.utcnow()

    sync._handle_coordinator_update()

    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_sync_callbacks_fall_back_for_legacy_create_task(
    hass, monkeypatch
) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-9"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)
    sync.async_refresh = AsyncMock()

    tasks: list[asyncio.Task] = []
    callbacks: list[callable] = []

    def _legacy_create_task(coro, *, name=None):
        if name is not None:
            raise TypeError("legacy")
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    def _fake_call_later(_hass, _delay, action):
        callbacks.append(action)
        return lambda: None

    monkeypatch.setattr(hass, "async_create_task", _legacy_create_task)
    monkeypatch.setattr(schedule_sync_mod, "async_call_later", _fake_call_later)
    sync._handle_interval()
    sync._last_sync = dt_util.utcnow() - timedelta(minutes=10)
    sync._handle_coordinator_update()
    sync._schedule_post_patch_refresh(RANDOM_SERIAL)
    callbacks[0](None)
    await asyncio.gather(*tasks)

    assert sync.async_refresh.await_count == 3


@pytest.mark.asyncio
async def test_schedule_sync_replace_slots_updates_cache_and_meta(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-11c"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, startTime="08:00", endTime="09:00")],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    sync._schedule_post_patch_refresh = MagicMock()

    client.patch_schedules = AsyncMock(
        return_value={
            "meta": {"serverTimeStamp": "2025-01-02T00:00:00.000+00:00"},
            "data": {
                "config": {"isOffPeakEligible": False},
                "slots": [_slot(slot_id, startTime="10:00", endTime="11:30")],
            },
        }
    )

    await sync.async_replace_slots(
        RANDOM_SERIAL, [_slot(slot_id, startTime="10:00", endTime="11:30")]
    )

    assert sync._meta_cache[RANDOM_SERIAL] == "2025-01-02T00:00:00.000+00:00"
    assert sync._config_cache[RANDOM_SERIAL] == {"isOffPeakEligible": False}
    assert sync._slot_cache[RANDOM_SERIAL][slot_id]["startTime"] == "10:00"
    sync._schedule_post_patch_refresh.assert_called_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_schedule_sync_upsert_delete_and_default_timestamp(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-11d"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, startTime="08:00", endTime="09:00")],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    timestamp = sync._default_server_timestamp()
    assert "." in timestamp

    sync._patch_slot = AsyncMock()
    client.create_schedule = AsyncMock(
        return_value={
            "meta": {"serverTimeStamp": "2025-01-03T00:00:00.000+00:00"},
            "data": "new-slot",
        }
    )
    client.delete_schedule = AsyncMock(
        return_value={"meta": {"serverTimeStamp": "2025-01-04T00:00:00.000+00:00"}}
    )

    existing_slot = _slot(slot_id, startTime="09:00", endTime="10:00")
    assert await sync.async_upsert_slot(RANDOM_SERIAL, existing_slot) is True
    sync._patch_slot.assert_awaited_once_with(RANDOM_SERIAL, slot_id, existing_slot)

    new_slot = _slot("new-slot", startTime="11:00", endTime="12:00")
    assert await sync.async_upsert_slot(RANDOM_SERIAL, new_slot) is True
    client.create_schedule.assert_awaited_once()
    create_args = client.create_schedule.await_args
    assert create_args.args[0] == RANDOM_SERIAL
    assert create_args.args[1]["id"] == "new-slot"
    assert sync._slot_cache[RANDOM_SERIAL]["new-slot"]["startTime"] == "11:00"

    client.delete_schedule.reset_mock()
    await sync.async_delete_slot(RANDOM_SERIAL, "missing")
    client.delete_schedule.assert_not_awaited()

    await sync.async_delete_slot(RANDOM_SERIAL, slot_id)
    client.delete_schedule.assert_awaited_once_with(RANDOM_SERIAL, slot_id)
    assert slot_id not in sync._slot_cache[RANDOM_SERIAL]


@pytest.mark.asyncio
async def test_schedule_sync_delete_slot_skips_when_scheduler_backoff(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-delete-backoff"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._scheduler_backoff_active = lambda: True  # noqa: SLF001
    coord.scheduler_last_error = "scheduler down"
    client.delete_schedule = AsyncMock()

    await sync.async_delete_slot(RANDOM_SERIAL, slot_id)

    assert sync._last_status == "scheduler_unavailable"
    assert sync._last_error == "scheduler down"
    client.delete_schedule.assert_not_awaited()
    assert slot_id in sync._slot_cache[RANDOM_SERIAL]


@pytest.mark.asyncio
async def test_schedule_sync_delete_slot_handles_scheduler_unavailable(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-delete-unavailable"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._note_scheduler_unavailable = MagicMock()  # noqa: SLF001
    client.delete_schedule = AsyncMock(side_effect=SchedulerUnavailable("down"))

    await sync.async_delete_slot(RANDOM_SERIAL, slot_id)

    assert sync._last_status == "scheduler_unavailable"
    assert sync._last_error == "down"
    assert slot_id in sync._slot_cache[RANDOM_SERIAL]
    coord._note_scheduler_unavailable.assert_called_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_schedule_sync_delete_slot_ignores_generic_delete_error(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-delete-error"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.delete_schedule = AsyncMock(side_effect=RuntimeError("boom"))

    await sync.async_delete_slot(RANDOM_SERIAL, slot_id)

    assert slot_id in sync._slot_cache[RANDOM_SERIAL]


@pytest.mark.asyncio
async def test_schedule_sync_delete_slot_uses_nested_response_metadata(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-delete-meta"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._mark_scheduler_available = MagicMock()  # noqa: SLF001
    client.delete_schedule = AsyncMock(
        return_value={
            "data": {"meta": {"serverTimeStamp": "2025-01-06T00:00:00.000+00:00"}}
        }
    )

    await sync.async_delete_slot(RANDOM_SERIAL, slot_id)

    assert sync._meta_cache[RANDOM_SERIAL] == "2025-01-06T00:00:00.000+00:00"
    assert slot_id not in sync._slot_cache[RANDOM_SERIAL]
    coord._mark_scheduler_available.assert_called_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_schedule_sync_upsert_returns_false_when_create_rejected(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    client.create_schedule = AsyncMock(side_effect=RuntimeError("boom"))

    assert await sync.async_upsert_slot(RANDOM_SERIAL, _slot("new-slot")) is False
    assert sync._last_status == "create_failed"
    assert sync._last_error == "boom"


@pytest.mark.asyncio
async def test_schedule_sync_create_slot_skips_when_scheduler_backoff(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._scheduler_backoff_active = lambda: True  # noqa: SLF001
    coord.scheduler_last_error = "scheduler down"
    client.create_schedule = AsyncMock()

    assert await sync._create_slot(RANDOM_SERIAL, _slot("new-slot")) is False
    assert sync._last_status == "scheduler_unavailable"
    assert sync._last_error == "scheduler down"
    client.create_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_create_slot_handles_scheduler_unavailable(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._note_scheduler_unavailable = MagicMock()  # noqa: SLF001
    client.create_schedule = AsyncMock(side_effect=SchedulerUnavailable("down"))

    assert await sync._create_slot(RANDOM_SERIAL, _slot("new-slot")) is False
    assert sync._last_status == "scheduler_unavailable"
    assert sync._last_error == "down"
    coord._note_scheduler_unavailable.assert_called_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_schedule_sync_create_slot_uses_nested_response_metadata(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    coord = sync._coordinator
    coord._mark_scheduler_available = MagicMock()  # noqa: SLF001
    client.create_schedule = AsyncMock(
        return_value={
            "data": {
                "id": "nested-slot",
                "meta": {"serverTimeStamp": "2025-01-05T00:00:00.000+00:00"},
            }
        }
    )

    assert await sync._create_slot(RANDOM_SERIAL, _slot("ignored-slot")) is True
    assert sync._meta_cache[RANDOM_SERIAL] == "2025-01-05T00:00:00.000+00:00"
    assert "nested-slot" in sync._slot_cache[RANDOM_SERIAL]
    coord._mark_scheduler_available.assert_called_once()  # noqa: SLF001


def test_schedule_sync_parse_slot_id_invalid() -> None:
    assert ScheduleSync._parse_slot_id("bad") == (None, None)
    assert ScheduleSync._parse_slot_id(f"{DOMAIN}:serial:slot") == (None, None)


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_updates_cache(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-enable"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, enabled=True)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedule_states = AsyncMock(
        return_value={"meta": {"serverTimeStamp": "2025-02-02T00:00:00.000+00:00"}}
    )

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, False)

    call = client.patch_schedule_states.await_args
    assert call.args[0] == RANDOM_SERIAL
    assert call.kwargs["slot_states"] == {slot_id: False}
    assert sync.get_slot(RANDOM_SERIAL, slot_id)["enabled"] is False


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_refreshes_without_slots(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-refresh"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, enabled=True)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedule_states = AsyncMock(
        return_value={"meta": {"serverTimeStamp": "2025-02-02T00:00:00.000+00:00"}}
    )
    sync._schedule_post_patch_refresh = MagicMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, False)

    sync._schedule_post_patch_refresh.assert_called_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_uses_response_slots(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-response-1"
    slot_id_two = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-response-2"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, enabled=True), _slot(slot_id_two, enabled=False)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    response_slots = [
        "bad",
        {"id": ""},
        _slot(slot_id, enabled=False),
        _slot(slot_id_two, enabled=True),
    ]
    client.patch_schedule_states = AsyncMock(
        return_value={
            "meta": {"serverTimeStamp": "2025-02-02T00:00:00.000+00:00"},
            "data": response_slots,
        }
    )
    sync._schedule_post_patch_refresh = MagicMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, False)

    assert sync.get_slot(RANDOM_SERIAL, slot_id)["enabled"] is False
    assert sync.get_slot(RANDOM_SERIAL, slot_id_two)["enabled"] is True
    sync._schedule_post_patch_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_empty_state_map(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-empty-map"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, enabled=True)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    class EmptyItems(dict):
        def __init__(self, slot):
            super().__init__()
            self._slot = slot

        def get(self, key, default=None):
            if key == slot_id:
                return self._slot
            return super().get(key, default)

        def items(self):
            return []

    sync._slot_cache = {RANDOM_SERIAL: EmptyItems(_slot(slot_id, enabled=True))}
    client.patch_schedule_states = AsyncMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, False)

    client.patch_schedule_states.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_patch_raises(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-raise"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, enabled=True)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedule_states = AsyncMock(side_effect=RuntimeError("boom"))

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, False)

    assert sync.get_slot(RANDOM_SERIAL, slot_id)["enabled"] is True


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_updates_from_dict_response(
    hass,
) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-dict-1"
    slot_id_two = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-dict-2"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, enabled=True), _slot(slot_id_two, enabled=False)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    response_slots = [
        "bad",
        {"id": ""},
        _slot(slot_id, enabled=False),
        _slot(slot_id_two, enabled=True),
    ]
    client.patch_schedule_states = AsyncMock(
        return_value={
            "meta": {"serverTimeStamp": "2025-02-02T00:00:00.000+00:00"},
            "data": {"config": {"isOffPeakEligible": False}, "slots": response_slots},
        }
    )
    sync._schedule_post_patch_refresh = MagicMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, False)

    assert sync.get_slot(RANDOM_SERIAL, slot_id)["enabled"] is False
    assert sync.get_slot(RANDOM_SERIAL, slot_id_two)["enabled"] is True
    assert sync._config_cache[RANDOM_SERIAL]["isOffPeakEligible"] is False
    sync._schedule_post_patch_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_updates_from_list_response(
    hass,
) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-list-1"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id, enabled=True)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedule_states = AsyncMock(
        return_value={
            "data": [
                "bad",
                {"id": ""},
                _slot(slot_id, enabled=False),
            ]
        }
    )
    sync._schedule_post_patch_refresh = MagicMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, False)

    assert sync.get_slot(RANDOM_SERIAL, slot_id)["enabled"] is False
    sync._schedule_post_patch_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_disabled_noop(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-disabled"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: False},
    )
    client = SimpleNamespace()
    client.patch_schedule_states = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    sync._slot_cache = {RANDOM_SERIAL: {slot_id: _slot(slot_id)}}

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, False)

    client.patch_schedule_states.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_allows_off_peak(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-off-peak"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [
            _slot(
                slot_id,
                scheduleType="OFF_PEAK",
                startTime=None,
                endTime=None,
                enabled=False,
            )
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedule_states = AsyncMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, True)
    await sync.async_set_slot_enabled(RANDOM_SERIAL, "missing", True)

    assert client.patch_schedule_states.await_count == 1
    assert client.patch_schedule_states.await_args.kwargs["slot_states"] == {
        slot_id: True
    }


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_off_peak_ineligible_skips_patch(
    hass,
) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-off-peak"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {"isOffPeakEligible": False},
        "slots": [
            _slot(
                slot_id,
                scheduleType="OFF_PEAK",
                startTime=None,
                endTime=None,
                enabled=False,
            )
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedule_states = AsyncMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id, True)

    client.patch_schedule_states.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_set_slot_enabled_sends_state_map(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-state-1"
    slot_id_two = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-state-2"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {"isOffPeakEligible": True},
        "slots": [
            _slot(slot_id, enabled=True),
            _slot(slot_id_two, enabled=False),
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedule_states = AsyncMock()

    await sync.async_set_slot_enabled(RANDOM_SERIAL, slot_id_two, True)

    assert client.patch_schedule_states.await_args.kwargs["slot_states"] == {
        slot_id: True,
        slot_id_two: True,
    }
    assert sync.get_slot(RANDOM_SERIAL, slot_id)["enabled"] is True
    assert sync.get_slot(RANDOM_SERIAL, slot_id_two)["enabled"] is True


@pytest.mark.asyncio
async def test_schedule_sync_missing_storage_handler_returns_none(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    handlers = hass.data.get(websocket_api.DOMAIN, {})
    handlers.pop(f"{SCHEDULE_DOMAIN}/create", None)
    sync._storage_collection = None
    assert await sync._ensure_storage_collection() is None


def test_schedule_sync_iter_slots(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    sync._slot_cache = {"SN1": {"slot-1": {"id": "slot-1"}}}

    assert list(sync.iter_slots()) == [("SN1", "slot-1", {"id": "slot-1"})]


def test_schedule_sync_off_peak_eligible_defaults_true(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    sync._config_cache["SN1"] = "bad"

    assert sync.is_off_peak_eligible("SN1") is True


@pytest.mark.asyncio
async def test_schedule_sync_sanitizes_schedule_storage_times(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_schedule_storage(storage_path)
    raw = {
        "version": 1,
        "key": "schedule",
        "data": {
            "items": [
                {
                    "id": "schedule-test",
                    CONF_THURSDAY: [
                        {
                            CONF_FROM: "18:00:00.123456",
                            CONF_TO: "23:59:59.999999",
                        }
                    ],
                }
            ]
        },
    }
    storage_path.write_text(json.dumps(raw), encoding="utf-8")

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is True

    updated = json.loads(storage_path.read_text(encoding="utf-8"))
    entry = updated["data"]["items"][0][CONF_THURSDAY][0]
    assert entry[CONF_FROM] == "18:00:00"
    assert entry[CONF_TO] == "23:59:59"


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_missing_file(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    _reset_schedule_storage(storage_path)

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_load_error(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    _reset_schedule_storage(storage_path)
    storage_path.mkdir(parents=True, exist_ok=True)

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_data_not_dict(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_schedule_storage(storage_path)
    storage_path.write_text(json.dumps({"data": "bad"}), encoding="utf-8")

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_items_not_list(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_schedule_storage(storage_path)
    storage_path.write_text(json.dumps({"data": {"items": "bad"}}), encoding="utf-8")

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_skips_invalid_items(hass) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_schedule_storage(storage_path)
    raw = {
        "data": {
            "items": [
                "bad-item",
                {
                    CONF_THURSDAY: [
                        "bad-entry",
                        {CONF_FROM: "bad.123456", CONF_TO: "bad.123456"},
                    ]
                },
            ]
        }
    }
    storage_path.write_text(json.dumps(raw), encoding="utf-8")

    sync = ScheduleSync(hass, SimpleNamespace(), None)
    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_sanitize_storage_save_error(hass, monkeypatch) -> None:
    storage_path = Path(hass.config.path(".storage", SCHEDULE_DOMAIN))
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    _reset_schedule_storage(storage_path)
    raw = {
        "data": {
            "items": [
                {CONF_THURSDAY: [{CONF_FROM: "18:00:00.123456", CONF_TO: "19:00:00"}]}
            ]
        }
    }
    storage_path.write_text(json.dumps(raw), encoding="utf-8")

    def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(schedule_sync_mod.json, "dump", _raise)
    sync = ScheduleSync(hass, SimpleNamespace(), None)

    assert await sync._sanitize_schedule_storage() is False


@pytest.mark.asyncio
async def test_schedule_sync_async_stop_clears_handlers(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-14"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, _client = await _setup_sync(hass, entry, payload)

    await sync.async_stop()
    assert sync._unsub_interval is None
    assert sync._unsub_coordinator is None


@pytest.mark.asyncio
async def test_schedule_sync_async_stop_clears_interval_and_coordinator_handlers(
    hass,
) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    called: list[str] = []
    sync._unsub_interval = lambda *_args: called.append("interval")
    sync._unsub_coordinator = lambda: called.append("coord")
    sync._pending_patch_refresh.add(RANDOM_SERIAL)
    sync._pending_patch_refresh_cancels[RANDOM_SERIAL] = lambda: called.append(
        "patch"
    )  # noqa: SLF001

    await sync.async_stop()

    assert called == ["interval", "coord", "patch"]
    assert sync._pending_patch_refresh == set()
    assert sync._pending_patch_refresh_cancels == {}  # noqa: SLF001
    assert sync._pending_patch_refresh_tasks == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_schedule_sync_stop_cancels_fired_post_patch_refresh(
    hass, monkeypatch
) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)
    callbacks: list[callable] = []
    started = asyncio.Event()
    cancelled = asyncio.Event()

    def _fake_call_later(_hass, _delay, action):
        callbacks.append(action)
        return lambda: None

    async def _refresh(*, reason, serials) -> None:
        assert reason == "patch"
        assert serials == [RANDOM_SERIAL]
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(schedule_sync_mod, "async_call_later", _fake_call_later)
    sync.async_refresh = _refresh

    sync._schedule_post_patch_refresh(RANDOM_SERIAL)
    callbacks[0](None)
    task = sync._pending_patch_refresh_tasks[RANDOM_SERIAL]  # noqa: SLF001
    await started.wait()

    await sync.async_stop()

    assert cancelled.is_set()
    assert task.cancelled()
    assert sync._pending_patch_refresh == set()
    assert sync._pending_patch_refresh_cancels == {}  # noqa: SLF001
    assert sync._pending_patch_refresh_tasks == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_schedule_sync_refresh_skips_when_locked(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-15"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    async with sync._lock:
        await sync.async_refresh(reason="locked")
    assert client.get_schedules.await_count >= 1


@pytest.mark.asyncio
async def test_schedule_sync_patch_slot_missing_timestamp_still_patches(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-17"
    payload = {
        "meta": None,
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    slot_patch = _slot(slot_id, startTime="07:00", endTime="08:00")
    await sync._patch_slot(RANDOM_SERIAL, slot_id, slot_patch)

    call = client.patch_schedule.await_args
    assert call.args[0] == RANDOM_SERIAL
    assert call.args[1] == slot_id
    assert call.args[2]["id"] == slot_id


@pytest.mark.asyncio
async def test_schedule_sync_patch_slot_schedules_refresh(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-17b"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedule = AsyncMock(return_value={"meta": {"serverTimeStamp": "ts"}})
    sync._schedule_post_patch_refresh = MagicMock()

    slot_patch = _slot(slot_id, startTime="07:00", endTime="08:00")
    await sync._patch_slot(RANDOM_SERIAL, slot_id, slot_patch)

    sync._schedule_post_patch_refresh.assert_called_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_schedule_sync_patch_slot_uses_inner_meta(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-17c"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    client.patch_schedule = AsyncMock(
        return_value={"data": {"meta": {"serverTimeStamp": "inner-ts"}}}
    )
    sync._schedule_post_patch_refresh = MagicMock()

    slot_patch = _slot(slot_id, startTime="07:00", endTime="08:00")
    await sync._patch_slot(RANDOM_SERIAL, slot_id, slot_patch)

    assert sync._meta_cache[RANDOM_SERIAL] == "inner-ts"


@pytest.mark.asyncio
async def test_schedule_sync_patch_slot_missing_id_skips(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-18"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    await sync._patch_slot(RANDOM_SERIAL, "missing", _slot("missing"))
    client.patch_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_patch_slot_runtime_error_returns(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-runtime-error"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)
    original = dict(sync._slot_cache[RANDOM_SERIAL][slot_id])
    client.patch_schedule = AsyncMock(side_effect=RuntimeError("boom"))

    await sync._patch_slot(
        RANDOM_SERIAL, slot_id, _slot(slot_id, startTime="10:00", endTime="11:00")
    )

    assert sync._slot_cache[RANDOM_SERIAL][slot_id] == original


@pytest.mark.asyncio
async def test_schedule_sync_handles_bad_response_and_slots(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(
        return_value={
            "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
            "config": {},
            "slots": ["bad", {"id": ""}],
        }
    )
    client.patch_schedule = AsyncMock()
    coord = DummyCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    assert sync._slot_cache.get(RANDOM_SERIAL) == {}
    client.get_schedules = AsyncMock(return_value="bad")
    await sync.async_refresh(reason="bad")
    assert sync._slot_cache.get(RANDOM_SERIAL) == {}


@pytest.mark.asyncio
async def test_schedule_sync_async_start_listener_error_sets_none(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: False},
    )
    entry.add_to_hass(hass)
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(return_value=payload)
    client.patch_schedule = AsyncMock()

    class BrokenCoordinator(DummyCoordinator):
        def async_add_listener(self, _cb):
            raise RuntimeError("boom")

    coord = BrokenCoordinator(hass, client, entry, data={})
    sync = ScheduleSync(hass, coord, entry)
    await sync.async_start()
    hass.data.setdefault("enphase_ev_schedule_syncs", []).append(sync)

    assert sync._unsub_coordinator is None


@pytest.mark.asyncio
async def test_schedule_sync_sync_serial_and_replace_error_paths(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    entry.add_to_hass(hass)
    await async_setup_component(hass, SCHEDULE_DOMAIN, {})
    client = SimpleNamespace()
    client._bearer = lambda: "token"
    client.get_schedules = AsyncMock(side_effect=RuntimeError("boom"))
    client.patch_schedule = AsyncMock()
    client.patch_schedules = AsyncMock(side_effect=SchedulerUnavailable("down"))
    coord = DummyCoordinator(hass, client, entry, data={RANDOM_SERIAL: {"name": "EV"}})
    sync = ScheduleSync(hass, coord, entry)

    await sync._sync_serial(RANDOM_SERIAL)
    assert sync._last_error is not None

    client.get_schedules = AsyncMock(
        return_value={"meta": None, "config": "bad", "slots": "bad"}
    )
    await sync._sync_serial(RANDOM_SERIAL)
    assert sync._config_cache.get(RANDOM_SERIAL) is None
    assert sync._slot_cache[RANDOM_SERIAL] == {}

    await sync.async_replace_slots(RANDOM_SERIAL, [_slot("slot"), "bad"])
    assert sync._last_status == "scheduler_unavailable"

    client.patch_schedules = AsyncMock(side_effect=RuntimeError("boom"))
    await sync.async_replace_slots(RANDOM_SERIAL, [_slot("slot")])


@pytest.mark.asyncio
async def test_schedule_sync_replace_slots_refreshes_timestamp_and_uses_list_data(
    hass,
) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)

    sync._meta_cache.pop(RANDOM_SERIAL, None)

    async def _refresh(*, reason="manual", serials=None):
        assert reason == "replace_prepare"
        sync._meta_cache[RANDOM_SERIAL] = "prep-ts"

    sync.async_refresh = AsyncMock(side_effect=_refresh)
    client.patch_schedules = AsyncMock(
        return_value={
            "data": {
                "meta": {"serverTimeStamp": "inner-ts"},
                "config": {"isOffPeakEligible": True},
            }
        }
    )
    sync._schedule_post_patch_refresh = MagicMock()

    await sync.async_replace_slots(RANDOM_SERIAL, [_slot("slot-list")])
    assert sync._meta_cache[RANDOM_SERIAL] == "inner-ts"
    assert sync._slot_cache[RANDOM_SERIAL]["slot-list"]["id"] == "slot-list"

    client.patch_schedules = AsyncMock(return_value={"data": [_slot("slot-list-two")]})
    await sync.async_replace_slots(RANDOM_SERIAL, [_slot("slot-list-two")])
    assert sync._slot_cache[RANDOM_SERIAL]["slot-list-two"]["id"] == "slot-list-two"


@pytest.mark.asyncio
async def test_schedule_sync_replace_slots_disabled_and_backoff(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [],
    }
    disabled_entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: False},
    )
    sync, client = await _setup_sync(hass, disabled_entry, payload)
    client.patch_schedules = AsyncMock()

    await sync.async_replace_slots(RANDOM_SERIAL, [_slot("disabled")])
    client.patch_schedules.assert_not_awaited()

    enabled_entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, enabled_entry, payload)
    client.patch_schedules = AsyncMock()
    sync._coordinator._scheduler_backoff_active = lambda: True  # noqa: SLF001

    await sync.async_replace_slots(RANDOM_SERIAL, [_slot("backoff")])
    client.patch_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_sync_replace_slots_runtime_error_returns(hass) -> None:
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot("existing-slot")],
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"site_id": RANDOM_SITE_ID},
        options={OPT_SCHEDULE_SYNC_ENABLED: True},
    )
    sync, client = await _setup_sync(hass, entry, payload)
    original = dict(sync._slot_cache[RANDOM_SERIAL])
    client.patch_schedules = AsyncMock(side_effect=RuntimeError("boom"))

    await sync.async_replace_slots(RANDOM_SERIAL, [_slot("replacement-slot")])

    assert sync._slot_cache[RANDOM_SERIAL] == original


@pytest.mark.asyncio
async def test_schedule_sync_sync_serial_exception_sets_error(hass) -> None:
    slot_id = f"{RANDOM_SITE_ID}:{RANDOM_SERIAL}:slot-error"
    payload = {
        "meta": {"serverTimeStamp": "2025-01-01T00:00:00.000+00:00"},
        "config": {},
        "slots": [_slot(slot_id)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"site_id": RANDOM_SITE_ID}, options={})
    sync, client = await _setup_sync(hass, entry, payload)

    client.get_schedules = AsyncMock(side_effect=RuntimeError("boom"))
    await sync._sync_serial(RANDOM_SERIAL)

    assert sync._last_error == "boom"


def test_schedule_sync_defaults_without_config_entry(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), None)

    assert sync._sync_enabled() is True


def test_schedule_sync_defaults_to_enabled_when_option_missing(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(), SimpleNamespace(options={}))

    assert sync._sync_enabled() is True


def test_schedule_sync_scheduler_backoff_active_requires_callable(hass) -> None:
    sync = ScheduleSync(hass, SimpleNamespace(scheduler_backoff_active=None), None)
    assert sync._scheduler_backoff_active() is False


def test_schedule_sync_scheduler_backoff_active_handles_error(hass) -> None:
    sync = ScheduleSync(
        hass,
        SimpleNamespace(
            scheduler_backoff_active=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        ),
        None,
    )
    assert sync._scheduler_backoff_active() is False


def test_schedule_sync_has_scheduler_bearer_handles_has_bearer_error(hass) -> None:
    client = SimpleNamespace(
        has_scheduler_bearer=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    sync = ScheduleSync(hass, SimpleNamespace(client=client), None)
    assert sync._has_scheduler_bearer() is False


def test_schedule_sync_has_scheduler_bearer_edge_cases(hass) -> None:
    sync_no_client = ScheduleSync(hass, SimpleNamespace(), None)
    assert sync_no_client._has_scheduler_bearer() is False

    control_headers = SimpleNamespace(control_headers=lambda: {"Authorization": "ok"})
    sync_control_headers = ScheduleSync(
        hass, SimpleNamespace(client=control_headers), None
    )
    assert sync_control_headers._has_scheduler_bearer() is True

    async def _control_headers_async():
        return {"Authorization": "ok"}

    control_headers_async = SimpleNamespace(control_headers=_control_headers_async)
    sync_control_headers_async = ScheduleSync(
        hass, SimpleNamespace(client=control_headers_async), None
    )
    assert sync_control_headers_async._has_scheduler_bearer() is False

    def _control_headers_raise():
        raise RuntimeError("boom")

    control_headers_raise = SimpleNamespace(control_headers=_control_headers_raise)
    sync_control_headers_raise = ScheduleSync(
        hass, SimpleNamespace(client=control_headers_raise), None
    )
    assert sync_control_headers_raise._has_scheduler_bearer() is False

    bearer_attr_none = SimpleNamespace(scheduler_bearer=None)
    sync_bearer_attr_none = ScheduleSync(
        hass, SimpleNamespace(client=bearer_attr_none), None
    )
    assert sync_bearer_attr_none._has_scheduler_bearer() is False

    async def _bearer_async():
        return "token"

    bearer_async = SimpleNamespace(scheduler_bearer=_bearer_async)
    sync_bearer_async = ScheduleSync(hass, SimpleNamespace(client=bearer_async), None)
    assert sync_bearer_async._has_scheduler_bearer() is False

    bearer_none = SimpleNamespace(scheduler_bearer=lambda: None)
    sync_bearer_none = ScheduleSync(hass, SimpleNamespace(client=bearer_none), None)
    assert sync_bearer_none._has_scheduler_bearer() is False

    def _bearer_raise():
        raise RuntimeError("boom")

    bearer_raise = SimpleNamespace(scheduler_bearer=_bearer_raise)
    sync_bearer_raise = ScheduleSync(hass, SimpleNamespace(client=bearer_raise), None)
    assert sync_bearer_raise._has_scheduler_bearer() is False

    bearer_awaitable = SimpleNamespace(scheduler_bearer=lambda: asyncio.sleep(0))
    sync_bearer_awaitable = ScheduleSync(
        hass, SimpleNamespace(client=bearer_awaitable), None
    )
    assert sync_bearer_awaitable._has_scheduler_bearer() is False
