from __future__ import annotations

import logging
from types import MappingProxyType
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from homeassistant.exceptions import ServiceValidationError
except ImportError:  # pragma: no cover - older HA cores
    from homeassistant.exceptions import HomeAssistantError

    class ServiceValidationError(HomeAssistantError):
        """Fallback for environments lacking ServiceValidationError."""

        def __init__(
            self,
            message: str | None = None,
            *,
            translation_domain: str | None = None,
            translation_key: str | None = None,
            translation_placeholders: dict[str, object] | None = None,
            **_: object,
        ) -> None:
            super().__init__(message)
            self.translation_domain = translation_domain
            self.translation_key = translation_key
            self.translation_placeholders = translation_placeholders


from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData


def test_button_type_available_uses_inventory_view() -> None:
    from custom_components.enphase_ev import button as button_mod

    coord = SimpleNamespace(
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "envoy"
        )
    )
    assert button_mod._type_available(coord, "envoy") is True
    assert button_mod._type_available(coord, "encharge") is False


def test_button_site_has_battery_branches() -> None:
    from custom_components.enphase_ev import button as button_mod

    coord_true = SimpleNamespace(battery_has_encharge=True, battery_has_enpower=False)
    assert button_mod._site_has_battery(coord_true) is True

    coord_false = SimpleNamespace(
        battery_has_encharge=False,
        battery_has_enpower=False,
        inventory_view=SimpleNamespace(has_type_for_entities=lambda _key: True),
    )
    assert button_mod._site_has_battery(coord_false) is False

    coord_unknown_gateway_only = SimpleNamespace(
        battery_has_encharge=None,
        battery_has_enpower=None,
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key == "envoy"
        ),
    )
    assert button_mod._site_has_battery(coord_unknown_gateway_only) is False


@pytest.mark.asyncio
async def test_start_stop_buttons_press(hass, monkeypatch):
    from custom_components.enphase_ev.button import StartChargeButton, StopChargeButton
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    coord.data = {
        sn: {
            "name": "Garage EV",
            "charging": False,
            "max_amp": 16,
            "min_amp": 6,
            "plugged": True,
        }
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.start_calls = []
            self.stop_calls = []
            self.stream_start_calls = 0

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "ok"}

        async def stop_charging(self, s):
            self.stop_calls.append(s)
            return {"status": "ok"}

        async def start_live_stream(self):
            self.stream_start_calls += 1
            return {"status": "accepted", "duration_s": 900}

    coord.client = StubClient()

    # Avoid debouncer refresh
    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore

    start_btn = StartChargeButton(coord, sn)
    stop_btn = StopChargeButton(coord, sn)

    # Start button clamps to device max when no prior setpoint exists
    await start_btn.async_press()
    assert coord.client.start_calls[-1] == (sn, 16, 1)
    assert coord.last_set_amps[sn] == 16
    # Stop button calls API
    await stop_btn.async_press()
    assert coord.client.stop_calls[-1] == sn


@pytest.mark.asyncio
async def test_start_button_requires_plugged(hass, monkeypatch):
    from custom_components.enphase_ev.button import StartChargeButton
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    coord.data = {
        sn: {
            "name": "Garage EV",
            "charging": False,
            "max_amp": 16,
            "min_amp": 6,
            "plugged": False,
        }
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.start_calls = []

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "ok"}

    coord.client = StubClient()

    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore

    start_btn = StartChargeButton(coord, sn)

    with pytest.raises(ServiceValidationError):
        await start_btn.async_press()
    assert coord.client.start_calls == []


@pytest.mark.asyncio
async def test_start_button_warns_when_auth_required(hass, monkeypatch, caplog):
    from custom_components.enphase_ev.button import StartChargeButton
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    coord.data = {
        sn: {
            "name": "Garage EV",
            "charging": False,
            "max_amp": 16,
            "min_amp": 6,
            "plugged": True,
            "auth_required": True,
        }
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.start_calls = []

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "ok"}

    coord.client = StubClient()

    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore

    start_btn = StartChargeButton(coord, sn)

    with caplog.at_level(logging.WARNING):
        await start_btn.async_press()
    assert coord.client.start_calls == [(sn, 16, 1)]
    assert any(
        "session authentication is required" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_start_button_skips_expectation_when_not_ready(hass, monkeypatch):
    from custom_components.enphase_ev.button import StartChargeButton
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        DEFAULT_SCAN_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    sn = RANDOM_SERIAL
    coord.data = {
        sn: {
            "name": "Garage EV",
            "charging": False,
            "max_amp": 16,
            "min_amp": 6,
            "plugged": True,
        }
    }
    coord.last_set_amps = {}

    class StubClient:
        def __init__(self):
            self.start_calls = []

        async def start_charging(
            self,
            s,
            amps,
            connector_id=1,
            *,
            include_level=None,
            strict_preference=False,
        ):
            self.start_calls.append((s, amps, connector_id))
            return {"status": "not_ready"}

    coord.client = StubClient()

    expectation_calls = []

    def _record_expectation(sn_arg, state, hold_for=0):
        expectation_calls.append((sn_arg, state, hold_for))

    coord.set_charging_expectation = _record_expectation  # type: ignore

    flags = {"kick": False, "refresh": False}

    def _kick_fast(duration):
        flags["kick"] = True

    async def _refresh():
        flags["refresh"] = True

    coord.kick_fast = _kick_fast  # type: ignore
    coord.async_request_refresh = _refresh  # type: ignore

    start_btn = StartChargeButton(coord, sn)
    await start_btn.async_press()

    assert coord.client.start_calls == [(sn, 16, 1)]
    assert expectation_calls == []
    assert flags["kick"] is False
    assert flags["refresh"] is False


@pytest.mark.asyncio
async def test_kick_fast_window(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
        MIN_FAST_POLL_INTERVAL,
        OPT_FAST_POLL_INTERVAL,
        OPT_SLOW_POLL_INTERVAL,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 15,
    }

    class DummyEntry:
        def __init__(self, options):
            self.options = options

        def async_on_unload(self, cb):
            return None

    options = {OPT_FAST_POLL_INTERVAL: 5, OPT_SLOW_POLL_INTERVAL: 20}
    entry = DummyEntry(options)
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg, config_entry=entry)

    class StubClient:
        def __init__(self, payload):
            self._payload = payload

        async def status(self):
            return self._payload

    # Idle payload (would normally be slow)
    payload_idle = {
        "evChargerData": [
            {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "charging": False,
                "pluggedIn": True,
            }
        ]
    }
    coord.client = StubClient(payload_idle)

    # Trigger fast window explicitly
    coord.kick_fast(60)
    await coord._async_update_data()
    assert int(coord.update_interval.total_seconds()) == MIN_FAST_POLL_INTERVAL


@pytest.mark.asyncio
async def test_button_platform_async_setup_entry_filters_known_serials(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.button import (
        CancelPendingProfileChangeButton,
        BatteryForceRefreshButton,
        BatteryScheduleDeleteButton,
        BatteryScheduleSaveButton,
        EvseScheduleDeleteButton,
        EvseScheduleRefreshButton,
        EvseScheduleSaveButton,
        StormAlertOptOutButton,
        StartChargeButton,
        StopChargeButton,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=["5555"])
    added: list[list[object]] = []
    listeners: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_listener(callback, *, context=None):
        listeners.append(callback)

        def _remove():
            listeners.remove(callback)

        return _remove

    coord.async_add_listener = capture_listener  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, capture_add)
    assert len(added) == 2
    assert isinstance(added[0][0], CancelPendingProfileChangeButton)
    assert isinstance(added[0][1], StormAlertOptOutButton)
    assert any(isinstance(entity, BatteryForceRefreshButton) for entity in added[0])
    assert any(isinstance(entity, BatteryScheduleSaveButton) for entity in added[0])
    assert any(isinstance(entity, BatteryScheduleDeleteButton) for entity in added[0])
    start_entity = next(
        entity for entity in added[1] if isinstance(entity, StartChargeButton)
    )
    stop_entity = next(
        entity for entity in added[1] if isinstance(entity, StopChargeButton)
    )
    assert isinstance(start_entity, StartChargeButton)
    assert isinstance(stop_entity, StopChargeButton)
    assert start_entity._sn == stop_entity._sn == "5555"
    assert any(isinstance(entity, EvseScheduleRefreshButton) for entity in added[1])
    assert any(isinstance(entity, EvseScheduleSaveButton) for entity in added[1])
    assert any(isinstance(entity, EvseScheduleDeleteButton) for entity in added[1])
    assert len(listeners) == 1

    added.clear()
    listeners[0]()

    coord._ensure_serial_tracked("6666")
    coord.data["6666"] = {"sn": "6666", "name": "Aux Charger"}
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_label": "EV Chargers",
                "count": 2,
                "devices": [
                    {"serial_number": "5555"},
                    {"serial_number": "6666"},
                ],
            }
        },
        ["iqevse"],
    )
    listeners[0]()

    assert len(added) == 1
    serials = {entity._sn for entity in added[0]}
    assert serials == {"6666"}


@pytest.mark.asyncio
async def test_button_platform_prunes_stale_buttons_when_inventory_ready(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.button import async_setup_entry

    coord = coordinator_factory(serials=["5555"])
    coord._devices_inventory_ready = True  # noqa: SLF001
    added: list[list[object]] = []
    listeners: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_listener(callback, *, context=None):
        listeners.append(callback)
        return lambda: None

    coord.async_add_listener = capture_listener  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "button",
        "enphase_ev",
        "enphase_ev_5555_start_charging",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, capture_add)

    coord.data.pop("5555", None)
    coord.iter_serials = lambda: []
    listeners[0]()

    remove_spy.assert_called_with(stale.entity_id)


@pytest.mark.asyncio
async def test_cancel_pending_profile_button(hass, monkeypatch) -> None:
    from custom_components.enphase_ev.button import CancelPendingProfileChangeButton
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            }
        },
        ["envoy"],
    )
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord.async_cancel_pending_profile_change = AsyncMock()

    button = CancelPendingProfileChangeButton(coord)
    assert button.available is True

    await button.async_press()

    coord.async_cancel_pending_profile_change.assert_awaited_once()


@pytest.mark.asyncio
async def test_storm_alert_opt_out_button(hass, monkeypatch) -> None:
    from custom_components.enphase_ev.button import StormAlertOptOutButton
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )

    cfg = {
        CONF_SITE_ID: RANDOM_SITE_ID,
        CONF_SERIALS: [RANDOM_SERIAL],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "Battery"}],
            },
        },
        ["envoy", "encharge"],
    )
    coord.async_opt_out_all_storm_alerts = AsyncMock()

    button = StormAlertOptOutButton(coord)
    assert button.available is True
    await button.async_press()
    coord.async_opt_out_all_storm_alerts.assert_awaited_once()

    coord._battery_show_storm_guard = False  # noqa: SLF001
    assert button.available is False


def test_storm_alert_opt_out_button_availability_guards() -> None:
    from custom_components.enphase_ev.button import StormAlertOptOutButton

    coord = SimpleNamespace(
        inventory_view=SimpleNamespace(
            has_type=lambda key: key == "envoy",
            type_bucket=lambda _key: None,
            type_device_info=lambda _key: None,
            has_type_for_entities=lambda key: key == "envoy",
        ),
        site_id="site",
        last_update_success=False,
        battery_has_encharge=True,
        battery_has_enpower=True,
        battery_show_storm_guard=True,
    )
    button = StormAlertOptOutButton(coord)
    assert button.available is False

    coord.last_update_success = True
    assert button.available is True

    coord.battery_show_storm_guard = False
    assert button.available is False

    coord.battery_show_storm_guard = True
    coord.inventory_view.has_type_for_entities = lambda _key: False
    assert button.available is False


def test_storm_alert_opt_out_button_device_info_fallback() -> None:
    from custom_components.enphase_ev.button import StormAlertOptOutButton

    coord = SimpleNamespace(
        site_id="site",
        last_update_success=True,
        battery_has_encharge=True,
        battery_has_enpower=True,
        battery_show_storm_guard=True,
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key == "envoy",
            type_device_info=lambda _key: None,
        ),
    )
    button = StormAlertOptOutButton(coord)
    assert button.device_info["identifiers"] == {("enphase_ev", "type:site:envoy")}
    assert button.device_info["manufacturer"] == "Enphase"


def test_cancel_pending_profile_button_device_info_fallback_and_override() -> None:
    from custom_components.enphase_ev.button import CancelPendingProfileChangeButton

    coord = SimpleNamespace(
        site_id="site",
        last_update_success=True,
        battery_profile_pending=True,
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key == "envoy",
            type_device_info=lambda _key: None,
        ),
    )
    button = CancelPendingProfileChangeButton(coord)

    assert button.device_info["identifiers"] == {("enphase_ev", "type:site:envoy")}

    expected = {"identifiers": {("enphase_ev", "provided")}}
    coord.inventory_view.type_device_info = MagicMock(return_value=expected)
    assert button.device_info is expected


@pytest.mark.asyncio
async def test_async_setup_entry_button_cleanup_waits_for_inventory_ready(
    hass, config_entry, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.button import async_setup_entry

    coord = SimpleNamespace(
        site_id="123456",
        _devices_inventory_ready=False,
        battery_has_encharge=True,
        battery_has_enpower=True,
        iter_serials=lambda: [],
        async_add_listener=MagicMock(return_value=lambda: None),
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key == "envoy",
            type_device_info=lambda _key: None,
        ),
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "button",
        "enphase_ev",
        "enphase_ev_site_123456_cancel_pending_profile_change",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    remove_spy.assert_not_called()
    assert ent_reg.async_get(stale.entity_id) is not None


def test_storm_alert_opt_out_button_device_info_prefers_type_info() -> None:
    from custom_components.enphase_ev.button import StormAlertOptOutButton

    expected = {
        "identifiers": {("enphase_ev", "type:site:envoy")},
        "manufacturer": "Enphase",
        "name": "IQ Gateway",
    }
    coord = SimpleNamespace(
        site_id="site",
        last_update_success=True,
        battery_has_encharge=True,
        battery_has_enpower=True,
        battery_show_storm_guard=True,
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key == "envoy",
            type_device_info=lambda key: expected if key == "envoy" else None,
        ),
    )
    button = StormAlertOptOutButton(coord)

    assert button.device_info is expected


@pytest.mark.asyncio
async def test_battery_schedule_save_button_shows_success_notification(
    hass, config_entry, monkeypatch
) -> None:
    from custom_components.enphase_ev.battery_schedule_editor import (
        BatteryScheduleEditorManager,
    )
    from custom_components.enphase_ev.button import BatteryScheduleSaveButton
    from custom_components.enphase_ev.const import OPT_BATTERY_SCHEDULES_ENABLED

    coord = SimpleNamespace(
        site_id="site",
        hass=hass,
        last_update_success=True,
        battery_has_encharge=True,
        battery_has_enpower=True,
        battery_write_access_confirmed=True,
        battery_timezone="Australia/Melbourne",
        _battery_schedules_payload={
            "dtg": {
                "scheduleStatus": "active",
                "details": [
                    {
                        "scheduleId": "sched-1",
                        "startTime": "18:00:00",
                        "endTime": "23:59:00",
                        "limit": 21,
                        "days": [1, 2, 3, 4, 5],
                        "timezone": "Australia/Melbourne",
                        "isEnabled": True,
                    }
                ],
            }
        },
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key == "encharge",
            type_device_info=lambda _key: None,
        ),
        client=SimpleNamespace(
            battery_schedules=object(),
            create_battery_schedule=object(),
            update_battery_schedule=object(),
            delete_battery_schedule=object(),
        ),
    )
    editor = BatteryScheduleEditorManager(coord)
    editor.sync_from_coordinator()
    config_entry.__dict__["options"] = MappingProxyType(
        {**config_entry.options, OPT_BATTERY_SCHEDULES_ENABLED: True}
    )
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        battery_schedule_editor=editor,
    )
    async_call = AsyncMock()
    monkeypatch.setattr(type(hass.services), "async_call", async_call)
    notification_spy = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.button.persistent_notification.async_create",
        notification_spy,
    )

    button = BatteryScheduleSaveButton(coord, config_entry)
    button.hass = hass

    await button.async_press()

    async_call.assert_awaited_once()
    notification_spy.assert_called_once()
    args = notification_spy.call_args.args
    kwargs = notification_spy.call_args.kwargs
    assert args[0] is hass
    assert args[1] == "Discharge To Grid Schedule 18:00-23:59"
    assert kwargs["title"] == button._button_label("save")  # noqa: SLF001
    assert kwargs["notification_id"] == "enphase_ev_battery_schedule_result_site"


@pytest.mark.asyncio
async def test_battery_schedule_delete_button_shows_success_notification(
    hass, config_entry, monkeypatch
) -> None:
    from custom_components.enphase_ev.battery_schedule_editor import (
        BatteryScheduleEditorManager,
    )
    from custom_components.enphase_ev.button import BatteryScheduleDeleteButton
    from custom_components.enphase_ev.const import OPT_BATTERY_SCHEDULES_ENABLED

    coord = SimpleNamespace(
        site_id="site",
        hass=hass,
        last_update_success=True,
        battery_has_encharge=True,
        battery_has_enpower=True,
        battery_write_access_confirmed=True,
        battery_timezone="Australia/Melbourne",
        _battery_schedules_payload={
            "rbd": {
                "scheduleStatus": "active",
                "details": [
                    {
                        "scheduleId": "sched-rbd",
                        "startTime": "06:30:00",
                        "endTime": "11:00:00",
                        "limit": 100,
                        "days": [1, 2, 3, 4, 5],
                        "timezone": "Australia/Melbourne",
                        "isEnabled": True,
                    }
                ],
            }
        },
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key == "encharge",
            type_device_info=lambda _key: None,
        ),
        client=SimpleNamespace(
            battery_schedules=object(),
            create_battery_schedule=object(),
            update_battery_schedule=object(),
            delete_battery_schedule=object(),
        ),
    )
    editor = BatteryScheduleEditorManager(coord)
    editor.sync_from_coordinator()
    config_entry.__dict__["options"] = MappingProxyType(
        {**config_entry.options, OPT_BATTERY_SCHEDULES_ENABLED: True}
    )
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        battery_schedule_editor=editor,
    )
    async_call = AsyncMock()
    monkeypatch.setattr(type(hass.services), "async_call", async_call)
    notification_spy = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.button.persistent_notification.async_create",
        notification_spy,
    )

    button = BatteryScheduleDeleteButton(coord, config_entry)
    button.hass = hass

    await button.async_press()

    async_call.assert_awaited_once()
    notification_spy.assert_called_once()
    args = notification_spy.call_args.args
    kwargs = notification_spy.call_args.kwargs
    assert args[1] == "Restrict Battery Discharge Schedule 06:30-11:00"
    assert kwargs["title"] == button._button_label("delete")  # noqa: SLF001
    assert kwargs["notification_id"] == "enphase_ev_battery_schedule_result_site"


def test_battery_schedule_button_skips_empty_notification_body(monkeypatch) -> None:
    from custom_components.enphase_ev.button import BatteryScheduleSaveButton

    coord = SimpleNamespace(
        site_id="site",
        hass=SimpleNamespace(),
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda _key: False,
            type_device_info=lambda _key: None,
        ),
        last_update_success=True,
        battery_has_encharge=True,
        battery_has_enpower=True,
    )
    config_entry = SimpleNamespace(
        entry_id="entry",
        runtime_data=EnphaseRuntimeData(
            coordinator=coord,
            battery_schedule_editor=None,
        ),
    )
    notification_spy = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.button.persistent_notification.async_create",
        notification_spy,
    )

    button = BatteryScheduleSaveButton(coord, config_entry)
    button.hass = coord.hass

    button._show_success_notification(action="save", body="  ")  # noqa: SLF001

    notification_spy.assert_not_called()


def test_battery_schedule_button_result_label_is_none_without_editor() -> None:
    from custom_components.enphase_ev.button import BatteryScheduleSaveButton

    coord = SimpleNamespace(
        site_id="site",
        hass=SimpleNamespace(),
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda _key: False,
            type_device_info=lambda _key: None,
        ),
        last_update_success=True,
        battery_has_encharge=True,
        battery_has_enpower=True,
    )
    config_entry = SimpleNamespace(
        entry_id="entry",
        runtime_data=EnphaseRuntimeData(
            coordinator=coord,
            battery_schedule_editor=None,
        ),
    )

    button = BatteryScheduleSaveButton(coord, config_entry)
    button.hass = coord.hass

    assert button._schedule_result_label() is None  # noqa: SLF001
