import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


@pytest.mark.asyncio
async def test_charge_mode_select(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.select import ChargeModeSelect

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

    # preload coordinator state
    coord.data = {RANDOM_SERIAL: {"charge_mode": "SCHEDULED_CHARGING"}}

    class StubClient:
        async def set_charge_mode(
            self, sn: str, mode: str, *, previous_mode: str | None = None
        ):
            return {"status": "accepted", "mode": mode}

    coord.client = StubClient()
    coord.evse_runtime.async_set_charge_mode = AsyncMock(
        wraps=coord.evse_runtime.async_set_charge_mode
    )

    # Avoid exercising Debouncer / hass loop; stub refresh
    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore[attr-defined]

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert "Green" in sel.options
    assert sel.current_option == "Scheduled"

    await sel.async_select_option("Manual")
    coord.evse_runtime.async_set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL,
        "MANUAL_CHARGING",
        previous_mode="SCHEDULED_CHARGING",
    )
    # cache should update immediately
    assert coord._charge_mode_cache[RANDOM_SERIAL][0] == "MANUAL_CHARGING"


@pytest.mark.asyncio
async def test_charge_mode_select_ignores_current_option(hass, monkeypatch) -> None:
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.select import ChargeModeSelect

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
    coord.data = {RANDOM_SERIAL: {"charge_mode": "MANUAL_CHARGING"}}
    coord.async_request_refresh = AsyncMock()

    class StubClient:
        set_charge_mode = AsyncMock()

    coord.client = StubClient()

    await ChargeModeSelect(coord, RANDOM_SERIAL).async_select_option("Manual")

    coord.client.set_charge_mode.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_setup_entry_falls_back_to_generic_listener_for_selects(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.select import async_setup_entry

    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
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
async def test_charge_mode_select_scheduled_requires_enabled_schedule(
    hass, monkeypatch
) -> None:
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.select import ChargeModeSelect

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
    coord.data = {RANDOM_SERIAL: {"charge_mode": "MANUAL_CHARGING"}}

    class StubClient:
        async def set_charge_mode(
            self, sn: str, mode: str, *, previous_mode: str | None = None
        ):
            err = aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url="https://example.test"),
                history=(),
                status=400,
                message="HTTP error from Enphase endpoint (status=400)",
            )
            err.enphase_scheduler_error = {
                "code": "iqevc_sch_10031",
                "display": "No Schedules enabled for Scheduled Charging",
            }
            raise err

    coord.client = StubClient()

    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore[attr-defined]

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    with pytest.raises(
        HomeAssistantError, match="Enable at least one schedule before selecting"
    ):
        await sel.async_select_option("Scheduled")


@pytest.mark.asyncio
async def test_charge_mode_select_reraises_unknown_scheduler_error(
    hass, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator
    from custom_components.enphase_ev.select import ChargeModeSelect

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
    coord.data = {RANDOM_SERIAL: {"charge_mode": "MANUAL_CHARGING"}}

    error_message = json.dumps(
        {
            "error": {
                "displayMessage": "Invalid Input",
                "errorMessageCode": "some_other_code",
            }
        }
    )

    class StubClient:
        async def set_charge_mode(
            self, sn: str, mode: str, *, previous_mode: str | None = None
        ):
            raise aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url="https://example.test"),
                history=(),
                status=400,
                message=error_message,
            )

    coord.client = StubClient()

    async def _noop():
        return None

    coord.async_request_refresh = _noop  # type: ignore[attr-defined]

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    with pytest.raises(aiohttp.ClientResponseError):
        await sel.async_select_option("Scheduled")


def test_parse_scheduler_error_handles_invalid_payloads() -> None:
    from custom_components.enphase_ev.select import _parse_scheduler_error

    assert _parse_scheduler_error("") == (None, None)
    assert _parse_scheduler_error("not-json") == (None, None)
    assert _parse_scheduler_error(json.dumps(["bad"])) == (None, None)
    assert _parse_scheduler_error(json.dumps({"error": "bad"})) == (None, None)


def test_select_helper_fallbacks() -> None:
    from custom_components.enphase_ev import select as select_mod

    coord = SimpleNamespace(
        site_id="site",
        battery_has_encharge=True,
        has_type=lambda type_key: type_key == "envoy",
        battery_write_access_confirmed=None,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_profile_selection_available=None,
        battery_controls_available=True,
    )

    assert select_mod._type_available(coord, "envoy") is True
    assert select_mod._retain_system_profile(coord) is True

    coord.battery_write_access_confirmed = False
    coord.battery_write_access_confirmed = None

    coord.battery_profile_selection_available = False
    assert select_mod._retain_system_profile(coord) is False

    coord.battery_profile_selection_available = None
    coord.battery_controls_available = False
    assert select_mod._retain_system_profile(coord) is False

    coord.battery_controls_available = True
    coord.battery_user_is_owner = False
    coord.battery_user_is_installer = False
    assert select_mod._retain_system_profile(coord) is False

    coord.inventory_view.has_type_for_entities = lambda _type_key: False
    coord.battery_user_is_owner = True
    assert select_mod._retain_system_profile(coord) is False


def test_explicit_charge_mode_handles_bad_data() -> None:
    from custom_components.enphase_ev.select import _explicit_charge_mode

    class BadDataCoordinator:
        @property
        def data(self):
            raise RuntimeError

        def _cached_charge_mode_preference(self, _sn):
            return None

    assert _explicit_charge_mode(BadDataCoordinator(), RANDOM_SERIAL) is None


def test_ac_battery_target_state_of_charge_select_state_and_options(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.select import AcBatteryTargetStateOfChargeSelect

    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._ac_battery_selected_sleep_min_soc = 25  # noqa: SLF001
    coord._ac_battery_sleep_state = "pending"  # noqa: SLF001
    coord._ac_battery_control_pending = True  # noqa: SLF001

    sel = AcBatteryTargetStateOfChargeSelect(coord)

    assert sel.available is True
    assert "25-30%" in sel.options
    assert sel.current_option == "25-30%"
    assert sel.extra_state_attributes["selected_sleep_min_soc"] == 25


@pytest.mark.asyncio
async def test_ac_battery_target_state_of_charge_select_updates_runtime(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.select import AcBatteryTargetStateOfChargeSelect

    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.async_set_ac_battery_target_soc = AsyncMock()

    sel = AcBatteryTargetStateOfChargeSelect(coord)

    await sel.async_select_option("30-35%")
    coord.async_set_ac_battery_target_soc.assert_awaited_once_with(30)


def test_ac_battery_target_state_of_charge_select_edge_branches(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.select import AcBatteryTargetStateOfChargeSelect

    coord = coordinator_factory()
    coord._battery_has_acb = False  # noqa: SLF001
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._ac_battery_selected_sleep_min_soc = 101  # noqa: SLF001

    sel = AcBatteryTargetStateOfChargeSelect(coord)

    assert sel.suggested_object_id == "ac_battery_target_state_of_charge"
    assert sel.available is False
    assert sel.current_option is None
    assert sel.device_info["name"] == "AC Battery"

    coord._battery_has_acb = True  # noqa: SLF001
    coord.last_update_success = False
    assert sel.available is False


@pytest.mark.asyncio
async def test_ac_battery_target_state_of_charge_select_rejects_invalid_option(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.select import AcBatteryTargetStateOfChargeSelect

    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001

    sel = AcBatteryTargetStateOfChargeSelect(coord)

    with pytest.raises(ServiceValidationError, match="not available"):
        await sel.async_select_option("invalid")


def test_charge_mode_select_current_option_paths(coordinator_factory):
    from custom_components.enphase_ev.const import STORM_GUARD_CACHE_TTL
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "GREEN_CHARGING"
    coord.data[RANDOM_SERIAL]["charge_mode"] = "MANUAL_CHARGING"

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert sel.current_option == "Green"

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = ""
    coord.data[RANDOM_SERIAL]["charge_mode"] = "experimental_mode"
    assert sel.current_option is None

    coord.set_charge_mode_cache(RANDOM_SERIAL, "SCHEDULED_CHARGING")
    coord.data[RANDOM_SERIAL]["charge_mode"] = "CUSTOM"
    assert sel.current_option == "Scheduled"

    coord.data[RANDOM_SERIAL]["charge_mode"] = ""
    coord._battery_profile_devices_last_success_mono = time.monotonic()  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "chargeMode": "GREEN", "enable": True}
    ]
    coord._charge_mode_cache.clear()  # noqa: SLF001
    assert sel.current_option == "Green"

    coord._battery_profile_devices_last_success_mono = (  # noqa: SLF001
        time.monotonic() - STORM_GUARD_CACHE_TTL
    )
    coord._charge_mode_cache.clear()  # noqa: SLF001
    assert sel.current_option is None

    coord.data[RANDOM_SERIAL]["schedule_type"] = "greencharging"
    assert sel.current_option == "Green"


def test_charge_mode_select_uses_smart_label_for_smart_mode(coordinator_factory):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "SMART_CHARGING"
    coord.data[RANDOM_SERIAL]["charge_mode"] = "SMART_CHARGING"

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    assert "Smart" in sel.options
    assert "Green" not in sel.options
    assert sel.current_option == "Smart"


@pytest.mark.asyncio
async def test_charge_mode_select_localizes_labels(coordinator_factory):
    from custom_components.enphase_ev.select import ChargeModeSelect
    from custom_components.enphase_ev.labels import async_prime_label_translations

    coord = coordinator_factory()
    coord.hass.config.language = "fr"
    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "SCHEDULED_CHARGING"
    await async_prime_label_translations(coord.hass)

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    assert sel.options == ["Manuel", "Planifié", "Vert"]
    assert sel.current_option == "Planifié"


@pytest.mark.asyncio
async def test_charge_mode_select_sets_smart_mode_for_single_evse_profile_context(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord._battery_profile_devices_last_success_mono = time.monotonic()  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": RANDOM_SERIAL, "chargeMode": "SMART", "enable": False}
    ]
    coord.client.set_charge_mode = AsyncMock(return_value={"status": "accepted"})
    coord.async_request_refresh = AsyncMock()

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert "Smart" in sel.options
    await sel.async_select_option("Smart")

    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SMART_CHARGING", previous_mode=None
    )


@pytest.mark.asyncio
async def test_charge_mode_select_maps_legacy_green_alias_to_smart_mode(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord._battery_profile_devices_last_success_mono = time.monotonic()  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": RANDOM_SERIAL, "chargeMode": "SMART", "enable": False}
    ]
    coord.client.set_charge_mode = AsyncMock(return_value={"status": "accepted"})
    coord.async_request_refresh = AsyncMock()

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    await sel.async_select_option("Green")

    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SMART_CHARGING", previous_mode=None
    )


@pytest.mark.asyncio
async def test_charge_mode_select_maps_localized_green_alias_to_smart_mode(
    coordinator_factory,
):
    from custom_components.enphase_ev.labels import async_prime_label_translations
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.hass.config.language = "de"
    await async_prime_label_translations(coord.hass)
    coord._battery_profile_devices_last_success_mono = time.monotonic()  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": RANDOM_SERIAL, "chargeMode": "SMART", "enable": False}
    ]
    coord.client.set_charge_mode = AsyncMock(return_value={"status": "accepted"})
    coord.async_request_refresh = AsyncMock()

    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    await sel.async_select_option("Grün")

    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "SMART_CHARGING", previous_mode=None
    )


@pytest.mark.asyncio
async def test_charge_mode_select_keeps_green_for_other_evse_on_ai_site(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import ChargeModeSelect

    other_serial = "SERIAL-2"
    coord = coordinator_factory(serials=[RANDOM_SERIAL, other_serial])
    coord._battery_profile = "ai_optimisation"  # noqa: SLF001
    coord.client.set_charge_mode = AsyncMock(return_value={"status": "accepted"})
    coord.async_request_refresh = AsyncMock()

    sel = ChargeModeSelect(coord, other_serial)

    assert "Green" in sel.options
    assert "Smart" not in sel.options

    await sel.async_select_option("Green")

    coord.client.set_charge_mode.assert_awaited_once_with(
        other_serial, "GREEN_CHARGING", previous_mode=None
    )


def test_charge_mode_select_helper_branches(coordinator_factory):
    from custom_components.enphase_ev.select import (
        ChargeModeSelect,
        _english_charge_mode_label,
        _smart_charging_context,
    )

    coord = coordinator_factory()
    assert _smart_charging_context(coord) is False
    assert _english_charge_mode_label("unexpected") is None
    coord._battery_profile_charge_mode_preference = None  # noqa: SLF001
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False

    class BrokenData:
        def get(self, _sn, default=None):
            raise RuntimeError("boom")

    coord.data = BrokenData()
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False

    coord = coordinator_factory()

    class BadString:
        def __str__(self):
            raise ValueError("boom")

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = BadString()
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "SMART_CHARGING"
    assert _smart_charging_context(coord, RANDOM_SERIAL) is True

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = None
    coord._charge_mode_cache[RANDOM_SERIAL] = (BadString(), 1.0)  # noqa: SLF001
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False

    coord = coordinator_factory()
    coord._battery_profile = "ai_optimisation"  # noqa: SLF001
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False
    coord._battery_profile_charge_mode_preference = lambda _sn: (  # noqa: SLF001
        _ for _ in ()
    ).throw(RuntimeError("boom"))
    assert _smart_charging_context(coord, RANDOM_SERIAL) is False

    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "MANUAL_CHARGING"
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert sel.current_option == "Manual"

    coord._resolve_charge_mode_pref = lambda _sn: "EXPERIMENTAL"  # noqa: SLF001
    assert sel.current_option is None


@pytest.mark.asyncio
async def test_charge_mode_select_passes_previous_mode_to_client(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.data[RANDOM_SERIAL]["charge_mode_pref"] = "GREEN_CHARGING"
    coord.client.set_charge_mode = AsyncMock(return_value={"status": "accepted"})
    coord.async_request_refresh = AsyncMock()

    await ChargeModeSelect(coord, RANDOM_SERIAL).async_select_option("Manual")

    coord.client.set_charge_mode.assert_awaited_once_with(
        RANDOM_SERIAL, "MANUAL_CHARGING", previous_mode="GREEN_CHARGING"
    )


@pytest.mark.asyncio
async def test_charge_mode_select_rejects_unknown_option(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.client.set_charge_mode = AsyncMock(return_value={"status": "accepted"})
    coord.async_request_refresh = AsyncMock()
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    with pytest.raises(
        ServiceValidationError,
        match="Selected charging mode is not available",
    ):
        await sel.async_select_option("experimental")

    coord.client.set_charge_mode.assert_not_awaited()


def test_charge_mode_select_unavailable_when_scheduler_down(coordinator_factory):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord._scheduler_available = False  # noqa: SLF001
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert sel.available is False


def test_charge_mode_select_ignores_feature_flag_for_availability(coordinator_factory):
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.data[RANDOM_SERIAL]["charge_mode_supported"] = False
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)
    assert sel.available is True


@pytest.mark.asyncio
async def test_charge_mode_select_blocks_when_scheduler_down(coordinator_factory):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord._scheduler_available = False  # noqa: SLF001
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    with pytest.raises(HomeAssistantError, match="scheduler service is down"):
        await sel.async_select_option("Manual")


@pytest.mark.asyncio
async def test_charge_mode_select_handles_scheduler_unavailable(
    coordinator_factory,
):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.enphase_ev.api import SchedulerUnavailable
    from custom_components.enphase_ev.select import ChargeModeSelect

    coord = coordinator_factory()
    coord.client.set_charge_mode = AsyncMock(side_effect=SchedulerUnavailable("down"))
    sel = ChargeModeSelect(coord, RANDOM_SERIAL)

    with pytest.raises(HomeAssistantError, match="scheduler service is down"):
        await sel.async_select_option("Manual")


@pytest.mark.asyncio
async def test_select_platform_async_setup_entry_filters_known_serials(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.select import (
        ChargeModeSelect,
        SystemProfileSelect,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=["1111"])
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    added: list[list[ChargeModeSelect]] = []
    listeners: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_listener(callback, *, context=None):
        listeners.append(callback)

        def _remove():
            listeners.remove(callback)

        return _remove

    coord.async_add_topology_listener = capture_listener  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, capture_add)
    flat_added = [entity for group in added for entity in group]
    assert any(isinstance(entity, SystemProfileSelect) for entity in flat_added)
    assert any(
        isinstance(entity, ChargeModeSelect) and entity._sn == "1111"
        for entity in flat_added
    )
    assert len(listeners) == 1

    added.clear()
    listeners[0]()
    assert added == []

    coord._ensure_serial_tracked("2222")
    coord.data["2222"] = {"sn": "2222", "name": "Driveway"}
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_label": "EV Chargers",
                "count": 2,
                "devices": [
                    {"serial_number": "1111"},
                    {"serial_number": "2222"},
                ],
            }
        },
        ["iqevse"],
    )
    listeners[0]()

    assert len(added) == 1
    assert {entity._sn for entity in added[0]} == {"2222"}


@pytest.mark.asyncio
async def test_select_platform_skips_system_profile_without_battery(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.const import (
        OPT_BATTERY_SCHEDULES_ENABLED,
        OPT_SCHEDULE_SYNC_ENABLED,
    )
    from custom_components.enphase_ev.select import ChargeModeSelect, async_setup_entry

    coord = coordinator_factory(serials=["1111"])
    coord._battery_has_encharge = False  # noqa: SLF001
    object.__setattr__(
        config_entry,
        "options",
        {
            OPT_SCHEDULE_SYNC_ENABLED: False,
            OPT_BATTERY_SCHEDULES_ENABLED: False,
        },
    )
    added: list[list[ChargeModeSelect]] = []
    listeners: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_listener(callback, *, context=None):
        listeners.append(callback)
        return lambda: None

    coord.async_add_topology_listener = capture_listener  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, capture_add)

    assert len(added) == 1
    assert all(isinstance(entity, ChargeModeSelect) for entity in added[0])
    assert len(listeners) == 1


@pytest.mark.asyncio
async def test_select_platform_retains_system_profile_while_permission_unknown(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import (
        OPT_BATTERY_SCHEDULES_ENABLED,
        OPT_SCHEDULE_SYNC_ENABLED,
    )
    from custom_components.enphase_ev.select import (
        ChargeModeSelect,
        SystemProfileSelect,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=["1111"])
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    object.__setattr__(
        config_entry,
        "options",
        {
            OPT_SCHEDULE_SYNC_ENABLED: False,
            OPT_BATTERY_SCHEDULES_ENABLED: False,
        },
    )
    added: list[list[object]] = []
    topology_callbacks: list[object] = []
    update_callbacks: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_topology(callback, *, context=None):
        topology_callbacks.append(callback)
        return lambda: None

    def capture_update(callback, *, context=None):
        update_callbacks.append(callback)
        return lambda: None

    coord.async_add_topology_listener = capture_topology  # type: ignore[attr-defined]
    coord.async_add_listener = capture_update  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    prune_spy = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.select.prune_managed_entities", prune_spy
    )

    await async_setup_entry(hass, config_entry, capture_add)

    assert len(topology_callbacks) == 1
    assert len(update_callbacks) == 1
    flat_added = [entity for group in added for entity in group]
    system_profile = next(
        entity for entity in flat_added if isinstance(entity, SystemProfileSelect)
    )
    assert system_profile.available is True
    assert any(
        isinstance(entity, ChargeModeSelect) and entity._sn == "1111"
        for entity in flat_added
    )

    system_profile_unique_id = f"enphase_ev_site_{coord.site_id}_system_profile"
    prune_spy.reset_mock()
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    update_callbacks[0]()

    active_unique_ids = prune_spy.call_args.kwargs["active_unique_ids"]
    assert system_profile_unique_id in active_unique_ids

    prune_spy.reset_mock()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    update_callbacks[0]()

    flat_added = [entity for group in added for entity in group]
    assert sum(isinstance(entity, SystemProfileSelect) for entity in flat_added) == 1
    active_unique_ids = prune_spy.call_args.kwargs["active_unique_ids"]
    assert system_profile_unique_id in active_unique_ids


@pytest.mark.asyncio
async def test_select_platform_adds_ac_battery_target_soc_select(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.select import (
        AcBatteryTargetStateOfChargeSelect,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=["1111"])
    coord._battery_has_acb = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    added: list[list[object]] = []
    topology_callbacks: list[object] = []
    update_callbacks: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_topology(callback, *, context=None):
        topology_callbacks.append(callback)
        return lambda: None

    def capture_update(callback, *, context=None):
        update_callbacks.append(callback)
        return lambda: None

    coord.async_add_topology_listener = capture_topology  # type: ignore[attr-defined]
    coord.async_add_listener = capture_update  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, capture_add)

    assert any(
        isinstance(entity, AcBatteryTargetStateOfChargeSelect)
        for group in added
        for entity in group
    )
    assert len(topology_callbacks) == 1
    assert len(update_callbacks) == 1


@pytest.mark.asyncio
async def test_select_platform_prunes_stale_entities_when_inventory_ready(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.select import async_setup_entry

    coord = coordinator_factory(serials=["1111"])
    coord._devices_inventory_ready = True  # noqa: SLF001
    added: list[list[object]] = []
    topology_callbacks: list[object] = []
    update_callbacks: list[object] = []

    def capture_add(entities, update_before_add=False):
        added.append(list(entities))

    def capture_topology(callback, *, context=None):
        topology_callbacks.append(callback)
        return lambda: None

    def capture_update(callback, *, context=None):
        update_callbacks.append(callback)
        return lambda: None

    coord.async_add_topology_listener = capture_topology  # type: ignore[attr-defined]
    coord.async_add_listener = capture_update  # type: ignore[attr-defined]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    stale_charge_mode = ent_reg.async_get_or_create(
        "select",
        "enphase_ev",
        "enphase_ev_1111_charge_mode_select",
        config_entry=config_entry,
    )
    stale_system_profile = ent_reg.async_get_or_create(
        "select",
        "enphase_ev",
        f"enphase_ev_site_{coord.site_id}_system_profile",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, capture_add)

    coord.data.pop("1111", None)
    coord.iter_serials = lambda: []
    coord._battery_has_encharge = False  # noqa: SLF001
    topology_callbacks[0]()
    update_callbacks[0]()

    remove_spy.assert_any_call(stale_charge_mode.entity_id)
    remove_spy.assert_any_call(stale_system_profile.entity_id)


def test_system_profile_select_options_and_current(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_ai_opti_savings_mode = True  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001

    sel = SystemProfileSelect(coord)

    assert sel.options == [
        "Self-Consumption",
        "Savings",
        "AI Optimisation",
        "Full Backup",
    ]
    assert sel.current_option == "Self-Consumption"


def test_system_profile_select_current_option_labels_ai_optimization(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_ai_optimisation_mode = True  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord._battery_profile = "ai_optimisation"  # noqa: SLF001

    sel = SystemProfileSelect(coord)

    assert sel.current_option == "AI Optimisation"


def test_system_profile_select_options_include_ai_optimization(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_ai_optimisation_mode = True  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord._battery_profile = "ai_optimisation"  # noqa: SLF001

    sel = SystemProfileSelect(coord)

    assert sel.options == ["Self-Consumption", "AI Optimisation", "Full Backup"]


def test_system_profile_select_includes_self_consumption_when_grid_charge_hidden(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = False  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_ai_optimisation_mode = True  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord._battery_profile = "backup_only"  # noqa: SLF001

    sel = SystemProfileSelect(coord)

    assert sel.options == [
        "Self-Consumption",
        "Savings",
        "AI Optimisation",
        "Full Backup",
    ]
    assert sel.current_option == "Full Backup"


@pytest.mark.asyncio
async def test_system_profile_select_sets_ai_optimization_profile(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_ai_optimisation_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.battery_runtime.async_set_system_profile = AsyncMock()

    sel = SystemProfileSelect(coord)
    await sel.async_select_option("AI Optimisation")

    coord.battery_runtime.async_set_system_profile.assert_awaited_once_with(
        "ai_optimisation"
    )


def test_system_profile_select_unavailable_and_none_current(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord.last_update_success = False
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = None  # noqa: SLF001
    sel = SystemProfileSelect(coord)

    assert sel.available is False
    assert sel.current_option is None


def test_system_profile_select_unavailable_for_read_only_user(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    sel = SystemProfileSelect(coord)

    assert sel.available is False


def test_system_profile_select_unavailable_when_fallback_permission_denied() -> None:
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = SimpleNamespace(
        site_id="site",
        hass=None,
        last_update_success=True,
        battery_profile_selection_available=True,
        battery_write_access_confirmed=False,
        battery_user_is_owner=False,
        battery_user_is_installer=False,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "envoy"
        ),
    )

    assert SystemProfileSelect(coord).available is False


def test_system_profile_select_available_while_write_access_unknown(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    sel = SystemProfileSelect(coord)

    assert sel.available is True


@pytest.mark.asyncio
async def test_system_profile_select_sets_profile(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_ai_opti_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.battery_runtime.async_set_system_profile = AsyncMock()

    sel = SystemProfileSelect(coord)
    await sel.async_select_option("Savings")

    coord.battery_runtime.async_set_system_profile.assert_awaited_once_with(
        "cost_savings"
    )

    await sel.async_select_option("AI Optimisation")
    coord.battery_runtime.async_set_system_profile.assert_awaited_with(
        "ai_optimisation"
    )


@pytest.mark.asyncio
async def test_system_profile_select_rejects_unknown_option(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="not available"):
        await sel.async_select_option("Not A Mode")


@pytest.mark.asyncio
async def test_system_profile_select_surfaces_validation_error(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.battery_runtime.async_set_system_profile = AsyncMock(
        side_effect=ServiceValidationError("Battery profile update was rejected.")
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="rejected"):
        await sel.async_select_option("Savings")


@pytest.mark.asyncio
async def test_system_profile_select_translates_raw_http_forbidden(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.battery_runtime.async_set_system_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await sel.async_select_option("Savings")


@pytest.mark.asyncio
async def test_system_profile_select_translates_raw_http_unauthorized(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.battery_runtime.async_set_system_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await sel.async_select_option("Savings")


@pytest.mark.asyncio
async def test_system_profile_select_translates_raw_http_other_error(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.battery_runtime.async_set_system_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="Boom",
        )
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="update failed"):
        await sel.async_select_option("Savings")


@pytest.mark.asyncio
async def test_system_profile_select_translates_raw_network_error(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.battery_runtime.async_set_system_profile = AsyncMock(
        side_effect=aiohttp.ClientConnectionError("boom")
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="network error"):
        await sel.async_select_option("Savings")


@pytest.mark.asyncio
async def test_system_profile_select_translates_timeout_error(
    coordinator_factory,
):
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.battery_runtime.async_set_system_profile = AsyncMock(
        side_effect=asyncio.TimeoutError()
    )
    sel = SystemProfileSelect(coord)

    with pytest.raises(ServiceValidationError, match="timed out"):
        await sel.async_select_option("Savings")


def test_system_profile_select_availability_fallback_and_device_info(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = SimpleNamespace(
        site_id="site",
        hass=None,
        last_update_success=True,
        battery_has_encharge=True,
        battery_profile_selection_available=None,
        battery_controls_available=False,
        battery_write_access_confirmed=None,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "envoy",
            type_device_info=lambda _type_key: None,
        ),
    )

    sel = SystemProfileSelect(coord)
    assert sel.available is False
    assert sel.device_info["identifiers"] == {("enphase_ev", "type:site:envoy")}

    coord.battery_controls_available = True
    coord.battery_user_is_owner = False
    coord.battery_user_is_installer = False
    assert sel.available is False

    coord.inventory_view.has_type_for_entities = lambda _type_key: False
    assert sel.available is False

    expected = {"identifiers": {("enphase_ev", "provided")}}
    coord.inventory_view.has_type_for_entities = lambda type_key: type_key == "envoy"
    coord.inventory_view.type_device_info = MagicMock(return_value=expected)
    assert sel.device_info is expected


def test_system_profile_select_available_with_legacy_fallback_controls() -> None:
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = SimpleNamespace(
        site_id="site",
        hass=None,
        last_update_success=True,
        battery_has_encharge=True,
        has_type=lambda type_key: type_key == "envoy",
        battery_profile_selection_available=None,
        battery_controls_available=True,
        battery_write_access_confirmed=None,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
    )

    sel = SystemProfileSelect(coord)
    assert sel.available is True

    coord.battery_user_is_owner = False
    coord.battery_user_is_installer = False
    assert sel.available is False


def test_system_profile_select_unavailable_when_legacy_controls_absent() -> None:
    from custom_components.enphase_ev.select import SystemProfileSelect

    coord = SimpleNamespace(
        site_id="site",
        hass=None,
        last_update_success=True,
        battery_has_encharge=True,
        has_type=lambda type_key: type_key == "envoy",
        battery_profile_selection_available=None,
        battery_controls_available=False,
        battery_write_access_confirmed=None,
        battery_user_is_owner=True,
        battery_user_is_installer=False,
        battery_profile_option_labels={"self-consumption": "Self-Consumption"},
        battery_profile_option_keys=["self-consumption"],
        battery_selected_profile="self-consumption",
    )

    assert SystemProfileSelect(coord).available is False


@pytest.mark.asyncio
async def test_async_setup_entry_select_cleanup_waits_for_inventory_ready(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.select import async_setup_entry

    coord = coordinator_factory()
    coord._devices_inventory_ready = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "select",
        "enphase_ev",
        f"enphase_ev_{RANDOM_SERIAL}_charge_mode_select",
        config_entry=config_entry,
    )
    remove_spy = MagicMock(wraps=ent_reg.async_remove)
    monkeypatch.setattr(ent_reg, "async_remove", remove_spy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    remove_spy.assert_not_called()
    assert ent_reg.async_get(stale.entity_id) is not None
