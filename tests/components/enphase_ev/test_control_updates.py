"""Control progress must never replace confirmed device values or connectivity."""

from datetime import time as dt_time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.enphase_ev import control_updates as updates_mod
from custom_components.enphase_ev.control_updates import (
    ControlUpdates,
    ControlUpdate,
    control_readback,
    current_control_value,
    tracked_control,
)
from custom_components.enphase_ev.control_values import (
    BATTERY_PROFILE_CONTROLS,
    EVSE_CACHES,
    SCALAR_CONTROLS,
    confirmed_control_values,
    matches_requested,
    requested_control_values,
)
from custom_components.enphase_ev.const import (
    DEFAULT_CHARGE_LEVEL_SETTING,
    GREEN_BATTERY_SETTING,
    SAVINGS_OPERATION_MODE_SUBTYPE,
)
from custom_components.enphase_ev.sensor import EnphaseControlUpdateStatusSensor
from custom_components.enphase_ev.switch import GreenBatterySwitch, ChargingSwitch


@pytest.fixture
def tracker(hass, monkeypatch):
    timers = []

    def schedule(_hass, _delay, action):
        cancel = Mock()
        timers.append((action, cancel))
        return cancel

    monkeypatch.setattr(updates_mod, "async_call_later", schedule)
    coord = SimpleNamespace(hass=hass, publish_runtime_state_update=Mock())
    coord.control_updates = ControlUpdates(coord)
    yield coord, coord.control_updates, timers
    coord.control_updates.cleanup()


def test_progress_confirmed_values_and_stale_reads(tracker):
    coord, runtime, timers = tracker
    old_tokens = runtime.read_tokens()
    update = runtime.begin("example", None, {"enabled": True}, {"enabled": False})
    assert runtime.status() == "pending"
    assert runtime.pending("example")
    assert current_control_value(coord, "example", "enabled", True) is False
    runtime.observe("example", None, {"enabled": True}, old_tokens)
    assert update.observed is None
    tokens = runtime.read_tokens()
    runtime.observe("example", None, {"enabled": False}, tokens)
    runtime.finish(("example", None), update)
    assert runtime.status() == "pending"
    with pytest.raises(ServiceValidationError) as caught:
        runtime.begin("example", None, {"enabled": False}, {})
    assert caught.value.translation_key == "control_change_pending"
    runtime.observe("example", None, {"enabled": True}, tokens)
    assert update.status == "confirmed"

    assert runtime.status() == "idle"
    assert not runtime.pending("example")
    timers[0][1].assert_called_once()
    assert current_control_value(coord, "example", "enabled", False) is False
    # Successful external changes continue updating the confirmed attributes.
    runtime.observe("example", None, {"enabled": False}, tokens)
    assert runtime.attributes()["example"]["confirmed"] == {"enabled": False}
    runtime.observe("other", "EV1", {"enabled": True}, tokens)
    assert runtime.values[("other", "EV1")] == {"enabled": True}
    assert current_control_value(SimpleNamespace(), "other", "enabled", 1) == 1


def test_confirmation_timeout_retry_failure_and_supersession(tracker):
    _coord, runtime, timers = tracker
    update = runtime.begin(
        "profile", None, {"mode": "new"}, {"mode": "old"}, group="battery"
    )
    with pytest.raises(ServiceValidationError):
        runtime.begin("reserve", None, {"reserve": 20}, {}, group="battery")
    runtime.finish(("profile", None), update)
    timers[0][0](None)
    assert runtime.status() == "unconfirmed"
    assert runtime.pending("profile")
    retry = runtime.begin(
        "profile", None, {"mode": "next"}, {"mode": "echo"}, group="battery"
    )
    assert runtime.value("profile", "mode", "echo") == "old"
    runtime.finish(("profile", None), update)  # Superseded completion is harmless.
    timers[0][0](None)
    assert retry.status == "pending"
    replacement = runtime.begin(
        "profile", None, {"mode": "final"}, {}, group="battery", supersede=True
    )
    assert timers[1][1].called
    runtime.finish(("profile", None), replacement, failed=True)
    assert runtime.status() == "failed"
    runtime.observe("profile", None, {"mode": "actual"}, runtime.read_tokens())
    assert runtime.value("profile", "mode", None) == "actual"
    update = runtime.begin("other", "EV1", {"enabled": True}, {"enabled": False})
    runtime.observe("other", "EV1", {"enabled": True}, runtime.read_tokens())
    assert update.status == "pending"  # Completion waits for accepted submission.
    runtime.finish(("other", "EV1"), update)
    assert update.status == "confirmed"
    assert runtime.status("EV1") == "idle"
    assert "other" not in runtime.attributes()
    runtime.cleanup()
    assert not runtime.updates and not runtime.values


@pytest.mark.asyncio
async def test_command_wrapper_nested_calls_errors_and_rejected_actions(tracker):
    coord, runtime, _timers = tracker

    class Commands:
        def __init__(self):
            self._coordinator = coord

        @tracked_control("outer", arguments=("enabled",), group="shared")
        async def outer(self, enabled=True):
            return await self.inner(enabled)

        @tracked_control("inner", arguments=("enabled",), group="shared")
        async def inner(self, enabled):
            return enabled

        @tracked_control("rejected", arguments=())
        async def rejected(self):
            return {"status": "not_ready"}

        @tracked_control("error", arguments=())
        async def error(self):
            raise asyncio.CancelledError

    commands = Commands()
    assert await commands.outer(False) is False
    assert runtime.updates[("outer", None)].status == "failed"
    assert ("inner", None) not in runtime.updates
    assert await commands.outer() is True
    assert runtime.updates[("outer", None)].requested == {"enabled": True}
    with pytest.raises(ServiceValidationError):
        await commands.inner(False)
    await commands.rejected()
    assert runtime.updates[("rejected", None)].status == "failed"
    with pytest.raises(asyncio.CancelledError):
        await commands.error()
    assert runtime.updates[("error", None)].status == "failed"
    coord.control_updates = None
    assert await commands.inner(True) is True
    coord.control_updates = runtime
    coord.runtime_active = False
    previous_updates = dict(runtime.updates)
    assert await commands.inner(True) is True
    assert runtime.updates == previous_updates


@pytest.mark.asyncio
async def test_endpoint_success_cache_and_failure_are_distinguished(tracker):
    coord, runtime, _timers = tracker
    coord._battery_profile = "self-consumption"
    coord._battery_settings_payload = {"profile": "self-consumption"}
    coord.battery_runtime = SimpleNamespace(
        normalize_battery_profile_key=lambda value: value
    )
    coord.battery_profile_pending = False
    health = SimpleNamespace(request_count=0, consecutive_failures=0)
    coord._endpoint_family_health = {"battery_settings": health}

    class Reader:
        coordinator = coord

        @control_readback("battery_settings")
        async def read(self, success=None):
            if success is not None:
                health.request_count += 1
                health.consecutive_failures = 0 if success else 1
            return True

    update = runtime.begin(
        "system_profile",
        None,
        {"profile_key": "self-consumption", "profile_ready": True},
        {},
    )
    reader = Reader()
    await reader.read()
    await reader.read(False)
    assert update.observed is None
    await reader.read(True)
    runtime.finish(("system_profile", None), update)
    assert update.status == "confirmed"


@pytest.mark.parametrize("control", SCALAR_CONTROLS)
def test_scalar_confirmed_contracts(tracker, control):
    coord, _runtime, _timers = tracker
    field, attr = SCALAR_CONTROLS[control]
    setattr(coord, attr, "enabled" if control == "storm_guard" else 20)
    coord._battery_operation_mode_sub_type = SAVINGS_OPERATION_MODE_SUBTYPE
    coord.battery_profile_pending = False
    values = confirmed_control_values(coord, control)
    assert field in values
    if control in BATTERY_PROFILE_CONTROLS:
        assert values["profile_ready"] is True
    if control == "storm_guard":
        setattr(coord, attr, None)
        assert confirmed_control_values(coord, control)[field] is None


@pytest.mark.parametrize("control", EVSE_CACHES)
def test_evse_confirmed_contracts(tracker, control):
    coord, _runtime, _timers = tracker
    field = {
        "charge_mode": "mode",
        "green_battery": "enabled",
        "app_authentication": "enabled",
        "default_charge_level": "amps",
    }[control]
    data_field = {
        "charge_mode": "charge_mode",
        "green_battery": "green_battery_enabled",
        "app_authentication": "app_auth_enabled",
        "default_charge_level": "default_charge_level",
    }[control]
    value = (
        "MANUAL_CHARGING"
        if control == "charge_mode"
        else 32 if control == "default_charge_level" else True
    )
    coord.data = {"EV1": {data_field: value}}
    assert confirmed_control_values(coord, control, "EV1") == {field: value}
    cache_value = (
        {DEFAULT_CHARGE_LEVEL_SETTING: value}
        if control == "default_charge_level"
        else value
    )
    coord.evse_state = SimpleNamespace(
        **{EVSE_CACHES[control]: {"EV1": (cache_value, 0, 0, 0, 0)}}
    )
    assert confirmed_control_values(coord, control, "EV1") == {field: value}
    if control == "default_charge_level":
        cache_value[DEFAULT_CHARGE_LEVEL_SETTING] = "invalid"
        assert confirmed_control_values(coord, control, "EV1") == {field: None}


def test_other_confirmed_contracts(tracker):
    coord, runtime, _timers = tracker
    coord.data = {"EV1": {"charging": True}}
    assert confirmed_control_values(coord, "charging", "EV1") == {"enabled": True}
    coord.data = object()
    assert confirmed_control_values(coord, "charging", "EV1") == {"enabled": None}
    coord._last_actual_charging = {"EV1": False}
    assert confirmed_control_values(coord, "charging", "EV1") == {"enabled": False}
    for family, name in (
        ("cfg", "charge_from_grid"),
        ("dtg", "discharge_to_grid"),
        ("rbd", "restrict_battery_discharge"),
    ):
        setattr(coord, f"battery_{name}_start_time", dt_time(8))
        setattr(coord, f"battery_{name}_end_time", dt_time(9))
        assert (
            confirmed_control_values(coord, f"{family}_schedule")["start_time"]
            == "08:00"
        )
    assert confirmed_control_values(coord, "battery_schedule_delete") == {
        "schedules": []
    }
    coord.schedule_sync = SimpleNamespace(_slot_cache={"EV1": {"id": {"id": "id"}}})
    assert confirmed_control_values(coord, "evse_schedule_save", "EV1")["slots"] == {
        "id": {"id": "id"}
    }
    coord.tariff_runtime = SimpleNamespace(_rate_signature=lambda: ())
    assert confirmed_control_values(coord, "tariff")["billing"] is None
    coord.tariff_billing = SimpleNamespace(attributes={"billing_cycle": "Monthly"})
    assert confirmed_control_values(coord, "tariff")["billing"] == {
        "billing_cycle": "Monthly"
    }
    coord.storm_alert_active = False
    assert confirmed_control_values(coord, "storm_alert_opt_out") == {
        "active_alerts": False
    }
    coord.grid_profile_runtime = SimpleNamespace(
        gateway_targets={}, current_profile_id="old"
    )
    assert confirmed_control_values(coord, "grid_profile")["profile_id"] == "old"
    runtime.updates[("grid_profile", None)] = ControlUpdate(
        "grid_profile", {"gateway_serial": "gateway"}, 0
    )
    coord.grid_profile_runtime.gateway_targets["gateway"] = SimpleNamespace(
        current_profile_id="new"
    )
    assert confirmed_control_values(coord, "grid_profile") == {
        "profile_id": "new",
        "gateway_serial": "gateway",
    }
    assert confirmed_control_values(coord, "unknown") == {}


@pytest.mark.parametrize(
    ("control", "arguments", "expected"),
    [
        (
            "system_profile",
            {"profile_key": "backup_only"},
            {"profile_key": "backup_only", "profile_ready": True},
        ),
        ("charge_mode", {"mode": "MANUAL"}, {"mode": "MANUAL_CHARGING"}),
        ("grid_mode", {"mode": "off_grid"}, {"mode": "off_grid"}),
        (
            "cfg_schedule",
            {"start": dt_time(8), "end": dt_time(9), "limit": None},
            {"start_time": "08:00", "end_time": "09:00", "schedule_ready": True},
        ),
        (
            "battery_schedule_delete",
            {"schedule_id": "1"},
            {"deleted_schedule": "1", "deleted_schedule_type": "cfg"},
        ),
        (
            "battery_schedule_update",
            {"schedule_id": "1", "is_deleted": True},
            {"deleted_schedule": "1", "deleted_schedule_type": "cfg"},
        ),
        (
            "battery_schedule_create",
            {"schedule_type": "CFG", "is_enabled": True},
            {
                "schedule": {"schedule_type": "cfg", "schedule_ready": True},
                "family_enabled": {"cfg": True},
            },
        ),
        ("evse_schedule_delete", {"slot_id": "1"}, {"deleted_slot": "1"}),
        (
            "evse_schedule_enabled",
            {"slot_id": "1", "enabled": True},
            {"slot": {"id": "1", "enabled": True}},
        ),
        (
            "evse_schedule_save",
            {"slot": {"enabled": True}},
            {"slot": {"enabled": True, "scheduleType": "CUSTOM"}},
        ),
        ("evse_schedule_replace", {"slots": []}, {"slots": []}),
        ("storm_alert_opt_out", {}, {"active_alerts": False}),
        ("tariff", {}, {"write_requested": True}),
        ("unknown", {"enabled": True}, {"enabled": True}),
    ],
)
def test_normalized_requested_contracts(
    coordinator_factory, control, arguments, expected
):
    coord = coordinator_factory()
    assert requested_control_values(control, arguments, coord) == expected


@pytest.mark.parametrize(
    ("requested", "observed", "matches"),
    [
        ({"deleted_schedule": "1"}, {"schedules": []}, True),
        ({"deleted_schedule": "1"}, {"schedules": [{"schedule_id": "1"}]}, False),
        ({"deleted_schedule": "1"}, {}, False),
        (
            {"schedule": {"enabled": True}},
            {"schedules": [{"enabled": True, "schedule_id": "1"}]},
            True,
        ),
        ({"schedule": {"enabled": True}}, {"schedules": [{"enabled": False}]}, False),
        ({"deleted_slot": "1"}, {"slots": {}}, True),
        ({"deleted_slot": "1"}, {}, False),
        (
            {"slot": {"id": "1", "enabled": True}},
            {"slots": {"1": {"id": "1", "enabled": True}}},
            True,
        ),
        ({"slots": [{"enabled": True}]}, {"slots": {"1": {"enabled": True}}}, True),
        ({"slots": []}, {"slots": {"1": {"enabled": True}}}, False),
        ({"enabled": True}, {"enabled": False}, False),
        ({"deleted_schedules": None}, {"schedules": []}, False),
        ({"deleted_schedules": []}, {"schedules": []}, False),
        ({"deleted_schedules": [None]}, {"schedules": []}, False),
        (
            {"deleted_schedules": [{"deleted_schedule": "1"}]},
            {"schedules": [{"schedule_id": "1"}]},
            False,
        ),
        ({}, {}, False),
    ],
)
def test_fresh_feedback_must_match_the_requested_fields(requested, observed, matches):
    assert matches_requested(requested, observed) is matches


@pytest.mark.asyncio
async def test_green_battery_pending_keeps_confirmed_switch_and_forces_fresh_get(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1"])
    coord.last_update_success = True
    coord.data["EV1"].update(green_battery_enabled=False, green_battery_supported=True)
    coord.evse_runtime.set_green_battery_cache("EV1", False)
    coord.client.set_green_battery_setting = AsyncMock()
    coord.client.green_charging_settings = AsyncMock(
        return_value=[{"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": False}]
    )
    coord.async_request_refresh = AsyncMock()
    await coord.evse_runtime.async_set_green_battery_setting("EV1", enabled=True)
    switch = GreenBatterySwitch(coord, "EV1")
    assert switch.available and not switch.is_on
    assert coord.control_updates.status("EV1") == "pending"
    assert coord.evse_runtime.green_battery_lookup_candidates(["EV1"]) == ["EV1"]
    with pytest.raises(ServiceValidationError):
        await coord.evse_runtime.async_set_green_battery_setting("EV1", enabled=False)
    coord.client.green_charging_settings.return_value = [
        {"chargerSettingName": GREEN_BATTERY_SETTING, "enabled": True}
    ]
    await coord.evse_runtime.async_get_green_battery_setting("EV1")
    assert coord.control_updates.status("EV1") == "idle"


@pytest.mark.asyncio
async def test_stop_remains_available_when_start_is_pending(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    coord.last_update_success = True
    coord.data["EV1"].update(plugged=True, charging=False)
    coord._last_actual_charging["EV1"] = False
    coord.client.start_charging = AsyncMock(return_value={"status": "ok"})
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    coord.async_start_streaming = AsyncMock()
    coord.async_stop_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    await coord.async_start_charging("EV1", allow_unplugged=True)
    assert coord.control_updates.status("EV1") == "pending"
    assert not ChargingSwitch(coord, "EV1").is_on
    await coord.async_stop_charging("EV1", allow_unplugged=True)
    coord.client.stop_charging.assert_awaited_once()
    assert coord.control_updates.updates[("charging", "EV1")].requested == {
        "enabled": False
    }


def test_progress_sensor_includes_site_chargers_export_and_grid(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    sensor = EnphaseControlUpdateStatusSensor(coord)
    assert sensor.native_value == "idle"
    update = coord.control_updates.begin(
        "charge_mode", "EV1", {"mode": "new"}, {"mode": "old"}
    )
    assert sensor.native_value == "pending"
    assert sensor.extra_state_attributes["updates"]["EV1"]["charge_mode"][
        "confirmed"
    ] == {"mode": "old"}
    coord.control_updates.finish(("charge_mode", "EV1"), update, failed=True)
    assert sensor.native_value == "failed"
    update.status = "unconfirmed"
    assert sensor.native_value == "unconfirmed"
    coord.export_limit_runtime.pending = {"watts": 0, "started": 0}
    coord.export_limit_runtime.request_status = "pending"
    assert sensor.native_value == "pending"
    coord.export_limit_runtime.request_status = "unconfirmed"
    assert sensor.native_value == "unconfirmed"
    coord.export_limit_runtime = SimpleNamespace(
        enabled=True,
        pending=coord.export_limit_runtime.pending,
        request_status="unconfirmed",
        attributes=lambda: {},
    )
    assert "export_limit" in sensor.extra_state_attributes["updates"]["site"]
    coord.grid_profile_runtime.pending_profile_id = "profile"
    assert sensor.native_value == "pending"
    assert (
        sensor.extra_state_attributes["updates"]["site"]["grid_profile"][
            "requested_profile_id"
        ]
        == "profile"
    )


def test_cancel_prune_and_related_values(tracker):
    coord, runtime, _timers = tracker
    update = runtime.begin(
        "profile", None, {"mode": "new"}, {"mode": "old"}, group="battery"
    )
    runtime.hold_related("reserve", None, update, {"reserve": 20})
    assert runtime.value("reserve", "reserve", 50) == 20
    runtime.observe("reserve", None, {"reserve": 25}, {})
    assert runtime.value("reserve", "reserve", 50) == 20
    runtime.observe("reserve", None, {"reserve": 25}, runtime.read_tokens())
    assert runtime.value("reserve", "reserve", 50) == 25
    assert update.status == "pending"  # Companion feedback cannot confirm the profile.
    runtime.cancel_group("battery")
    assert update.status == "cancelled" and runtime.status() == "idle"
    assert current_control_value(coord, "reserve", "reserve", 50) == 25
    runtime.begin("example", "removed", {"enabled": True}, {})
    runtime.hold_related(
        "companion", "removed", runtime.updates[("example", "removed")], {}
    )
    runtime.values[("only_cached", "removed")] = {}
    runtime.prune({"retained"})
    assert all(key[1] is None for key in runtime.updates)
    assert all(key[1] is None for key in runtime.values)
    assert all(key[1] is None for key in runtime.held_by)


@pytest.mark.asyncio
async def test_saved_schedule_uses_get_feedback_and_ignores_malformed_reads(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1"])
    sync = coord.schedule_sync
    slot = {"id": "1", "enabled": False, "startTime": "08:00", "endTime": "09:00"}
    sync._slot_cache["EV1"] = {"1": slot}
    update = coord.control_updates.begin(
        "evse_schedule_enabled",
        "EV1",
        {"slot": {"id": "1", "enabled": True}},
        {"slots": {"1": slot}},
        group="evse_schedule",
    )
    coord.client.get_schedules = AsyncMock(
        return_value={"slots": [dict(slot, enabled=True)]}
    )
    response, err = await sync._async_fetch_serial_sync("EV1")
    assert err is None
    assert update.status == "pending"
    coord.control_updates.finish(("evse_schedule_enabled", "EV1"), update)
    assert update.status == "confirmed"
    sync._apply_sync_serial_result("EV1", response, None)
    assert sync.get_slot("EV1", "1")["enabled"] is True
    coord.client.get_schedules.return_value = {"slots": [None]}
    await sync._async_fetch_serial_sync("EV1")
    assert (
        coord.control_updates.values[("evse_schedule_enabled", "EV1")]["slots"]["1"][
            "enabled"
        ]
        is True
    )


@pytest.mark.asyncio
async def test_grid_profile_request_target_and_fresh_confirmation(coordinator_factory):
    from tests.components.enphase_ev.test_grid_profile_runtime import (
        _FakeGridProfileClient,
    )
    from custom_components.enphase_ev.grid_profile_runtime import GridProfileRuntime

    client = _FakeGridProfileClient()
    coord = coordinator_factory(serials=[], client=client)
    runtime = GridProfileRuntime(coord)
    coord.grid_profile_runtime = runtime
    runtime._start_pending_refresh = Mock()
    await runtime.async_refresh(force=True)
    await runtime.async_apply_grid_profile("agf:export", region_code="VIC")
    update = coord.control_updates.updates[("grid_profile", None)]
    assert update.status == "pending"
    assert update.requested == {
        "profile_id": "agf:export",
        "gateway_serial": "122532006376",
    }
    with pytest.raises(ServiceValidationError):
        await runtime.async_apply_grid_profile("agf:common", region_code="VIC")
    client.activation_record["envoys"][0]["grid_profile_id"] = "agf:export"
    await runtime.async_refresh_device_status(force=True)
    assert update.status == "confirmed"


def test_export_rejection_is_reported_separately(coordinator_factory):
    coord = coordinator_factory()
    coord.export_limit_runtime.request_status = "rejected"
    assert EnphaseControlUpdateStatusSensor(coord).native_value == "failed"


@pytest.mark.asyncio
async def test_tariff_progress_confirms_only_matching_fresh_values(coordinator_factory):
    from tests.components.enphase_ev.test_tariff import _write_test_branch

    coord = coordinator_factory()
    payload = {
        "purchase": _write_test_branch("0.1"),
        "buyback": _write_test_branch("0.2"),
    }
    coord.client.site_tariff = AsyncMock(return_value=payload)
    coord.client.site_tariff_bundle = AsyncMock(return_value=({}, payload))
    coord.client.site_tariff_update = AsyncMock(return_value={"message": "success"})
    coord.client.notify_tariff_change = AsyncMock()
    coord.tariff_runtime._schedule_post_write_reconciliation = Mock()
    await coord.tariff_runtime.async_refresh(force=True)
    await coord.tariff_runtime.async_update_tariff(
        purchase_tariff=_write_test_branch("0.3")
    )
    update = coord.control_updates.updates[("tariff", None)]
    assert update.status == "pending"
    assert coord.tariff_import_rate is not None
    with pytest.raises(ServiceValidationError):
        await coord.tariff_runtime.async_update_tariff(
            purchase_tariff=_write_test_branch("0.4")
        )
    coord.client.site_tariff_update.assert_awaited_once()
    coord.client.site_tariff_bundle.return_value = (
        {},
        coord.client.site_tariff_update.await_args.args[0],
    )
    await coord.tariff_runtime.async_refresh(force=True)
    assert update.status == "confirmed"


@pytest.mark.asyncio
async def test_companion_battery_setting_remains_confirmed_during_schedule_write(
    coordinator_factory,
):
    from custom_components.enphase_ev.switch import ChargeFromGridSwitch

    coord = coordinator_factory()
    coord._battery_charge_from_grid = False
    runtime = coord.battery_runtime
    # The underlying write path echoes both settings optimistically; neither is
    # device feedback. Exercise the real public wrapper with that local behavior.
    runtime._async_set_schedule_family_enabled = AsyncMock()

    async def write(_family, _enabled):
        coord._battery_charge_from_grid = True
        coord._battery_dtg_schedule_enabled = True

    runtime._async_set_schedule_family_enabled.side_effect = write
    await runtime.async_set_discharge_to_grid_schedule_enabled(True)
    assert not ChargeFromGridSwitch(coord).is_on
    assert coord.control_updates.status() == "pending"
    with pytest.raises(ServiceValidationError):
        await runtime.async_set_charge_from_grid(False)
    coord.control_updates.observe(
        "charge_from_grid", None, {"enabled": True}, coord.control_updates.read_tokens()
    )
    assert ChargeFromGridSwitch(coord).is_on
    assert coord.control_updates.status() == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["app_authentication", "default_charge_level"])
async def test_pending_settings_bypass_fresh_caches_and_respect_backoff(
    coordinator_factory, control
):
    from custom_components.enphase_ev.const import AUTH_APP_SETTING

    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    coord.async_request_refresh = AsyncMock()
    if control == "app_authentication":
        runtime.set_app_auth_cache("EV1", False)
        coord.client.set_app_authentication = AsyncMock()
        coord.client.charger_auth_settings = AsyncMock(
            return_value=[{"key": AUTH_APP_SETTING, "value": True}]
        )
        await runtime.async_set_app_authentication("EV1", enabled=True)
        assert runtime.auth_settings_lookup_candidates(["EV1"]) == ["EV1"]
        await runtime.async_get_auth_settings("EV1")
        coord.client.charger_auth_settings.assert_awaited_once()
    else:
        coord._charger_config_cache["EV1"] = (
            {DEFAULT_CHARGE_LEVEL_SETTING: 16},
            updates_mod.time.monotonic(),
        )
        coord.client.set_default_charge_level = AsyncMock(return_value={})
        coord.client.charger_config = AsyncMock(
            return_value=[{"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": 24}]
        )
        await runtime.async_set_default_charge_level("EV1", 24)
        assert runtime.charger_config_lookup_candidates(
            ["EV1"], keys=[DEFAULT_CHARGE_LEVEL_SETTING]
        ) == ["EV1"]
        coord._charger_config_backoff_until["EV1"] = updates_mod.time.monotonic() + 60
        assert (
            runtime.charger_config_lookup_candidates(
                ["EV1"], keys=[DEFAULT_CHARGE_LEVEL_SETTING]
            )
            == []
        )
        assert (
            await runtime.async_get_charger_config(
                "EV1", keys=[DEFAULT_CHARGE_LEVEL_SETTING]
            )
            is None
        )
        assert coord.control_updates.status("EV1") == "pending"
        coord._charger_config_backoff_until.clear()
        await runtime.async_get_charger_config(
            "EV1", keys=[DEFAULT_CHARGE_LEVEL_SETTING]
        )
        coord.client.charger_config.assert_awaited_once()
    assert coord.control_updates.updates[(control, "EV1")].status == "confirmed"


@pytest.mark.asyncio
async def test_parallel_task_cannot_inherit_a_guard_bypass(tracker):
    coord, runtime, _timers = tracker
    entered = asyncio.Event()

    class Commands:
        coordinator = coord

        @tracked_control("mode", arguments=("enabled",), group="shared")
        async def outer(self, enabled):
            entered.set()
            task = asyncio.create_task(self.inner(False))
            with pytest.raises(ServiceValidationError):
                await task
            # Same task, different charger: this is a distinct progress record.
            assert await self.per_charger("EV2", True) is True
            return True

        @tracked_control("reserve", arguments=("enabled",), group="shared")
        async def inner(self, enabled):
            return enabled

        @tracked_control(
            "toggle", arguments=("enabled",), serial_argument="sn", group="shared"
        )
        async def per_charger(self, sn, enabled):
            return enabled

    assert await Commands().outer(True) is True
    assert entered.is_set()
    assert ("toggle", "EV2") in runtime.updates
    assert ("reserve", None) not in runtime.updates


def test_profile_pending_does_not_change_companion_availability_or_bounds(
    coordinator_factory,
):
    from custom_components.enphase_ev.number import BatteryReserveNumber
    from custom_components.enphase_ev.select import SystemProfileSelect
    from custom_components.enphase_ev.switch import SavingsUseBatteryAfterPeakSwitch

    coord = coordinator_factory()
    coord.last_update_success = True
    coord._battery_show_battery_backup_percentage = True
    coord._battery_show_savings_mode = True
    coord._battery_show_full_backup = True
    coord._battery_profile = "cost_savings"
    coord._battery_backup_percentage = 20
    coord._battery_backup_percentage_min = 10
    coord._battery_user_is_owner = True
    coord.battery_runtime.set_battery_pending(
        profile="backup_only", reserve=100, sub_type=None, require_exact_settings=False
    )
    # Even a restored/backend pending profile must not change confirmed controls.
    assert SystemProfileSelect(coord).current_option == "Savings"
    assert BatteryReserveNumber(coord).available
    assert BatteryReserveNumber(coord).native_value == 20
    assert BatteryReserveNumber(coord).native_min_value == 10
    assert SavingsUseBatteryAfterPeakSwitch(coord).available


@pytest.mark.asyncio
async def test_cancelled_profile_releases_the_confirmation_guard(coordinator_factory):
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"
    coord._battery_user_is_owner = True
    coord.client.cancel_battery_profile_update = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    update = coord.control_updates.begin(
        "system_profile",
        None,
        {"profile_key": "backup_only"},
        {"profile_key": "self-consumption"},
        group="battery_profile",
    )
    coord.battery_runtime.set_battery_pending(
        profile="backup_only", reserve=100, sub_type=None, require_exact_settings=False
    )
    await coord.async_cancel_pending_profile_change()
    assert update.status == "cancelled"
    assert coord.control_updates.status() == "idle"
    coord.control_updates.begin(
        "system_profile",
        None,
        {"profile_key": "self-consumption"},
        {},
        group="battery_profile",
    )
    await coord.async_cancel_pending_profile_change()  # Already processed backend state.
    assert coord.control_updates.status() == "idle"


def test_pending_controls_bypass_success_cache_without_bypassing_cooldown(
    coordinator_factory,
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord.client.storm_guard_profile = AsyncMock()
    coord.client.battery_settings_details = AsyncMock()
    for group, family, due in (
        ("storm_guard", "storm_guard", runtime.storm_guard_refresh_due),
        ("battery_settings", "battery_settings", runtime.battery_settings_refresh_due),
    ):
        coord._note_endpoint_family_success(family, success_ttl_s=300)
        coord.control_updates.begin(
            "example_" + group, None, {"enabled": True}, {}, group=group
        )
        assert due()
        coord._note_endpoint_family_failure(family, TimeoutError())
        assert not due()


@pytest.mark.asyncio
async def test_profile_echo_never_becomes_confirmed_entity_state(coordinator_factory):
    from custom_components.enphase_ev.select import SystemProfileSelect
    from custom_components.enphase_ev.sensor import EnphaseSystemProfileStatusSensor
    from custom_components.enphase_ev.number import BatteryReserveNumber

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"
    coord._battery_backup_percentage = 20
    coord._battery_show_full_backup = True
    coord._battery_show_battery_backup_percentage = True
    coord._battery_user_is_owner = True
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    await coord.battery_runtime.async_set_system_profile("backup_only")
    assert coord.control_updates.status() == "pending"
    assert SystemProfileSelect(coord).current_option == "Self-Consumption"
    sensor = EnphaseSystemProfileStatusSensor(coord)
    assert sensor.native_value == "Self-Consumption"
    assert sensor.extra_state_attributes["configured_profile"] == "self-consumption"
    assert sensor.extra_state_attributes["effective_reserve_percentage"] == 20
    assert BatteryReserveNumber(coord).native_value == 20
    assert (
        coord.control_updates.attributes()["system_profile"]["requested"]["profile_key"]
        == "backup_only"
    )


def test_schedule_intent_matches_normalized_wire_values(coordinator_factory):
    coord = coordinator_factory()
    requested = requested_control_values(
        "evse_schedule_save",
        {
            "slot": {
                "id": None,
                "enabled": "true",
                "startTime": dt_time(8),
                "chargingLevelAmp": "24",
                "days": ["1", 3],
                "serverOnly": "ignore",
            }
        },
        coord,
    )
    assert requested == {
        "slot": {
            "enabled": True,
            "startTime": "08:00",
            "chargingLevelAmp": 24,
            "days": [1, 3],
            "scheduleType": "CUSTOM",
        }
    }
    requested = requested_control_values(
        "evse_schedule_replace", {"slots": [{"id": "one", "enabled": 1}, None]}, coord
    )
    assert requested == {
        "slots": [{"id": "one", "enabled": True, "scheduleType": "CUSTOM"}]
    }
    requested = requested_control_values(
        "battery_schedule_update",
        {
            "schedule_id": 1,
            "schedule_type": "CFG",
            "start_time": "08:05:00",
            "end_time": "09:10:00",
            "days": [3, 1],
        },
        coord,
    )
    assert requested["schedule"]["schedule_id"] == "1"
    assert requested["schedule"]["start_time"] == "08:05"
    assert requested["schedule"]["days"] == [1, 3]
    assert not matches_requested(
        {"slots": [{"enabled": True}, {"enabled": True}]},
        {"slots": {"a": {"enabled": True}, "b": {"enabled": False}}},
    )
    assert matches_requested(
        {"slots": [{"enabled": True}, {"enabled": True}]},
        {"slots": {"a": {"enabled": True}, "b": {"enabled": True}}},
    )


def test_schedule_readback_uses_individual_enable_flags(coordinator_factory):
    coord = coordinator_factory()
    coord._battery_charge_from_grid_schedule_enabled = True
    coord._battery_schedules_payload = {
        "cfg": {
            "details": [
                {
                    "scheduleId": 1,
                    "isEnabled": False,
                    "startTime": "08:00",
                    "endTime": "09:00",
                    "days": [1],
                },
                {
                    "scheduleId": 2,
                    "startTime": "10:00",
                    "endTime": "11:00",
                    "days": [2],
                },
            ]
        },
        "dtg": {"details": None},
        "invalid": None,
    }
    records = confirmed_control_values(coord, "battery_schedule_update")["schedules"]
    assert records[0]["enabled"] is False
    assert records[1]["enabled"] is True


@pytest.mark.asyncio
async def test_new_schedule_confirmation_requires_its_created_id(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    sync = coord.schedule_sync
    sync._sync_enabled = Mock(return_value=True)
    sync._schedule_post_patch_refresh = Mock()
    slot = {
        "enabled": True,
        "startTime": "08:00",
        "endTime": "09:00",
        "scheduleType": "CUSTOM",
    }
    sync._slot_cache["EV1"] = {"old": dict(slot, id="old")}
    coord.client.create_schedule = AsyncMock(return_value={"data": {"id": "new"}})
    assert await sync.async_upsert_slot("EV1", slot)
    update = coord.control_updates.updates[("evse_schedule_save", "EV1")]
    assert update.requested["slot"]["id"] == "new"
    coord.client.get_schedules = AsyncMock(
        return_value={"slots": [dict(slot, id="old")]}
    )
    await sync._async_fetch_serial_sync("EV1")
    assert update.status == "pending"
    coord.client.get_schedules.return_value = {
        "slots": [dict(slot, id="old"), dict(slot, id="new")]
    }
    await sync._async_fetch_serial_sync("EV1")
    assert update.status == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("control", list(EVSE_CACHES))
async def test_unconfirmed_charger_settings_resume_normal_cache_policy(
    coordinator_factory, control
):
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    now = updates_mod.time.monotonic()
    caches = {
        "charge_mode": ("MANUAL_CHARGING", now),
        "green_battery": (False, True, now),
        "app_authentication": (False, False, True, True, now),
        "default_charge_level": ({DEFAULT_CHARGE_LEVEL_SETTING: 16}, now),
    }
    getattr(coord.evse_state, EVSE_CACHES[control])["EV1"] = caches[control]
    update = coord.control_updates.begin(control, "EV1", {"enabled": True}, {})
    coord.control_updates.finish((control, "EV1"), update)
    update.status = "unconfirmed"
    runtime.state._charge_mode_pending["EV1"] = ("SCHEDULED_CHARGING", now - 601)
    coord.client.charge_mode = AsyncMock(return_value="MANUAL_CHARGING")
    coord.client.green_charging_settings = AsyncMock(return_value=[])
    coord.client.charger_auth_settings = AsyncMock(return_value=[])
    coord.client.charger_config = AsyncMock(return_value=[])
    readers = {
        "charge_mode": runtime.async_get_charge_mode,
        "green_battery": runtime.async_get_green_battery_setting,
        "app_authentication": runtime.async_get_auth_settings,
        "default_charge_level": runtime.async_get_charger_config,
    }
    if control == "default_charge_level":
        await readers[control]("EV1", keys=[DEFAULT_CHARGE_LEVEL_SETTING])
    else:
        await readers[control]("EV1")
    for reader in (
        coord.client.charge_mode,
        coord.client.green_charging_settings,
        coord.client.charger_auth_settings,
        coord.client.charger_config,
    ):
        reader.assert_not_awaited()
    assert update.status == "unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [TimeoutError(), {"evChargerData": []}])
async def test_reused_charging_status_cannot_confirm_a_command(
    coordinator_factory, response
):
    coord = coordinator_factory(serials=["EV1"])
    payload = {
        "evChargerData": [
            {"sn": "EV1", "charging": False, "connectors": [{}], "session_d": {}}
        ]
    }
    coord.client.status = AsyncMock(return_value=payload)
    await coord._async_update_data()
    update = coord.control_updates.begin(
        "charging", "EV1", {"enabled": False}, {"enabled": True}
    )
    coord.control_updates.finish(("charging", "EV1"), update)
    if isinstance(response, Exception):
        coord.client.status.side_effect = response
    else:
        coord.client.status.return_value = response
    await coord._async_update_data()
    assert coord.payload_using_stale
    assert update.status == "pending"
    assert update.observed is None


def test_new_group_request_retires_old_progress_and_preserves_confirmed_values(tracker):
    _coord, runtime, timers = tracker
    old = runtime.begin(
        "system_profile",
        None,
        {"profile_key": "backup_only"},
        {},
        group="battery_profile",
    )
    runtime.hold_related("battery_reserve", None, old, {"reserve": 20})
    runtime.finish(("system_profile", None), old)
    timers[0][0](None)
    new = runtime.begin(
        "battery_reserve",
        None,
        {"reserve": 25},
        {"reserve": 100},
        group="battery_profile",
    )
    runtime.hold_related("system_profile", None, new, {})
    assert runtime.value("battery_reserve", "reserve", 100) == 20
    runtime.finish(("battery_reserve", None), new)
    runtime.observe("battery_reserve", None, {"reserve": 25}, runtime.read_tokens())
    assert runtime.status() == "idle"
    assert old.status == "cancelled"


@pytest.mark.asyncio
async def test_battery_settings_read_cannot_confirm_an_echoed_schedule_limit(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord._battery_cfg_schedule_limit = 60
    update = coord.control_updates.begin(
        "cfg_schedule", None, {"limit": 80, "schedule_ready": True}, {"limit": 60}
    )
    coord._battery_cfg_schedule_limit = 80  # Existing write path's local echo.
    coord.client.battery_settings_details = AsyncMock(return_value={"data": {}})
    coord.control_updates.finish(("cfg_schedule", None), update)
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert update.status == "pending"
    assert coord.control_updates.value("cfg_schedule", "limit", 80) == 60


@pytest.mark.asyncio
async def test_battery_status_read_cannot_confirm_reserve_without_reserve_feedback(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"
    coord._battery_backup_percentage = 20
    update = coord.control_updates.begin(
        "battery_reserve", None, {"reserve": 25, "profile_ready": True}, {"reserve": 20}
    )
    coord._battery_backup_percentage = 25  # Legacy pending-profile promotion.
    coord.client.battery_status = AsyncMock(
        return_value={"storages": [{"id": "one", "battery_mode": "Self-Consumption"}]}
    )
    coord.control_updates.finish(("battery_reserve", None), update)
    await coord.battery_runtime.async_refresh_battery_status(force=True)
    assert update.status == "pending"
    assert coord.control_updates.value("battery_reserve", "reserve", 25) == 20


@pytest.mark.asyncio
async def test_battery_reserve_confirmation_uses_the_normalized_request(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"
    coord._battery_backup_percentage = 20
    coord._battery_backup_percentage_min = 10
    coord._battery_show_battery_backup_percentage = True
    runtime = coord.battery_runtime
    runtime.async_apply_battery_reserve_only = AsyncMock()
    await runtime.async_set_battery_reserve(0)
    runtime.async_apply_battery_reserve_only.assert_awaited_once_with(
        profile="self-consumption", reserve=10
    )
    assert (
        coord.control_updates.updates[("battery_reserve", None)].requested["reserve"]
        == 10
    )


@pytest.mark.asyncio
async def test_queued_stop_requires_a_read_started_after_its_acknowledgement(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1"])
    entered, release = asyncio.Event(), asyncio.Event()
    coord.data["EV1"].update(plugged=True, charging=False)
    coord.async_start_streaming = AsyncMock()
    coord.async_request_refresh = AsyncMock()

    async def start(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"status": "ok"}

    coord.client.start_charging = start
    coord.client.stop_charging = AsyncMock(return_value={"status": "ok"})
    start_task = asyncio.create_task(
        coord.async_start_charging("EV1", allow_unplugged=True)
    )
    await entered.wait()
    stop_task = asyncio.create_task(coord.async_stop_charging("EV1"))
    await asyncio.sleep(0)
    ledger = coord.control_updates
    before_ack = ledger.read_tokens()
    ledger.observe("charging", "EV1", {"enabled": False}, before_ack)
    assert ledger.updates[("charging", "EV1")].observed == {"enabled": False}
    release.set()
    await asyncio.gather(start_task, stop_task)
    update = ledger.updates[("charging", "EV1")]
    assert update.status == "pending"
    assert update.observed is None
    ledger.observe("charging", "EV1", {"enabled": False}, before_ack)
    assert update.status == "pending"
    ledger.observe("charging", "EV1", {"enabled": False}, ledger.read_tokens())
    assert update.status == "confirmed"


def test_feedback_sources_report_only_fields_returned_by_each_endpoint(
    coordinator_factory,
):
    from custom_components.enphase_ev.control_values import fresh_control_values

    coord = coordinator_factory()
    coord._battery_settings_payload = None
    assert fresh_control_values(coord, "battery_reserve", "battery_settings") == {}
    coord._battery_settings_payload = {
        "data": {
            "profile": "cost_savings",
            "batteryBackupPercentage": 25,
            "operationModeSubType": " Prioritize-Energy ",
            "chargeFromGrid": True,
            "veryLowSoc": 15,
            "stormGuardState": "enabled",
            "evseStormEnabled": None,
            "powerMatchControl": {"enabled": True},
            "chargeFromGridScheduleEnabled": True,
            "chargeBeginTime": 480,
            "chargeEndTime": 540,
            "dtgControl": {"enabled": False, "startTime": 600, "endTime": 660},
        }
    }
    coord.battery_runtime.parse_battery_settings_payload(
        coord._battery_settings_payload
    )
    data = fresh_control_values(coord, "system_profile", "battery_settings")
    assert data["configured_profile"] == "cost_savings"
    assert data["operation_mode_sub_type"] == " Prioritize-Energy "
    assert (
        fresh_control_values(coord, "battery_reserve", "battery_settings")["reserve"]
        == 25
    )
    assert (
        fresh_control_values(
            coord, "savings_use_battery_after_peak", "battery_settings"
        )["enabled"]
        is True
    )
    for control, expected in (
        ("charge_from_grid", {"enabled": True}),
        ("power_match", {"enabled": True}),
        ("battery_shutdown_level", {"level": 15}),
        ("storm_guard", {"enabled": True}),
        ("storm_evse", {"enabled": None}),
        ("cfg_schedule", {"enabled": True, "start_time": "08:00", "end_time": "09:00"}),
        (
            "dtg_schedule",
            {"enabled": False, "start_time": "10:00", "end_time": "11:00"},
        ),
        ("rbd_schedule", {}),
    ):
        assert fresh_control_values(coord, control, "battery_settings") == expected
    coord._battery_settings_payload = {"stormGuardState": "unknown"}
    assert fresh_control_values(coord, "storm_guard", "battery_settings") == {
        "enabled": None
    }
    assert fresh_control_values(coord, "grid_mode", "battery_settings") == {}
    coord._battery_schedules_payload = {"cfg": {"details": None}, "dtg": None}
    assert fresh_control_values(coord, "cfg_schedule", "battery_schedules") == {}
    assert fresh_control_values(coord, "dtg_schedule", "battery_schedules") == {}
    coord._battery_schedules_payload = {
        "cfg": {
            "details": [
                {
                    "scheduleId": "one",
                    "startTime": "08:00:00",
                    "endTime": "09:00",
                    "limit": 80,
                    "isEnabled": False,
                }
            ],
            "scheduleStatus": "pending",
        }
    }
    coord.battery_runtime.parse_battery_schedules_payload(
        coord._battery_schedules_payload
    )
    assert fresh_control_values(coord, "cfg_schedule", "battery_schedules") == {
        "start_time": "08:00",
        "end_time": "09:00",
        "limit": 80,
        "schedule_ready": False,
    }
    coord._battery_schedules_payload["cfg"]["details"][0]["scheduleStatus"] = "active"
    assert (
        fresh_control_values(coord, "cfg_schedule", "battery_schedules")[
            "schedule_ready"
        ]
        is True
    )


@pytest.mark.asyncio
async def test_grid_eligibility_cannot_confirm_cached_relay_state(coordinator_factory):
    coord = coordinator_factory()
    coord._grid_mode_status_supported = True
    coord._grid_mode_status = "on_grid"
    update = coord.control_updates.begin(
        "grid_mode", None, {"mode": "on_grid"}, {"mode": "off_grid"}
    )
    coord.control_updates.finish(("grid_mode", None), update)
    coord.client.grid_control_check = AsyncMock(
        return_value={"disableGridControl": False}
    )
    await coord.battery_runtime.async_refresh_grid_control_check(force=True)
    assert update.status == "pending"
    coord.battery_runtime.grid_envoy_serial = Mock(return_value="gateway")
    coord.client.site_livestream_payload = AsyncMock(
        return_value={"meters": {"gridRelay": "OPER_RELAY_CLOSED"}}
    )
    await coord.battery_runtime.async_refresh_grid_mode_status(force=True)
    assert update.status == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{}, {"dtg": {"details": []}}, {"cfg": {"details": [None]}}]
)
async def test_battery_schedule_deletion_requires_its_family_inventory(
    coordinator_factory, payload
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    runtime.async_apply_schedule_family_settings = AsyncMock()
    coord.client.delete_battery_schedule = AsyncMock()
    await runtime.async_delete_battery_schedule("one", schedule_type="cfg")
    update = coord.control_updates.updates[("battery_schedule_delete", None)]
    coord.client.battery_schedules = AsyncMock(return_value=payload)
    await runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "pending"
    coord.client.battery_schedules.return_value = {"cfg": {"details": []}}
    await runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "confirmed"


@pytest.mark.asyncio
async def test_schedule_confirmation_cannot_infer_missing_enable_flag(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord._battery_charge_from_grid_schedule_enabled = True
    update = coord.control_updates.begin(
        "battery_schedule_update",
        None,
        {"schedule": {"schedule_id": "one", "enabled": True}},
        {"schedules": []},
    )
    coord.control_updates.finish(("battery_schedule_update", None), update)
    detail = {"scheduleId": "one", "startTime": "08:00", "endTime": "09:00"}
    coord.client.battery_schedules = AsyncMock(
        return_value={"cfg": {"details": [detail]}}
    )
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "pending"
    detail["isEnabled"] = True
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "confirmed"


@pytest.mark.asyncio
async def test_profile_confirmation_keeps_unread_companion_values(
    coordinator_factory,
):
    from custom_components.enphase_ev.number import BatteryReserveNumber

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"
    coord._battery_backup_percentage = 20
    coord._battery_show_full_backup = True
    coord._battery_show_battery_backup_percentage = True
    coord._battery_user_is_owner = True
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    await coord.battery_runtime.async_set_system_profile("backup_only")
    coord.client.battery_status = AsyncMock(
        return_value={"storages": [{"id": "one", "battery_mode": "Full Backup"}]}
    )
    await coord.battery_runtime.async_refresh_battery_status(force=True)
    update = coord.control_updates.updates[("system_profile", None)]
    assert update.status == "confirmed"
    assert coord._battery_backup_percentage == 100  # Legacy profile promotion.
    assert BatteryReserveNumber(coord).native_value == 20
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"batteryBackupPercentage": 95}}
    )
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert BatteryReserveNumber(coord).native_value == 95


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control", ["battery_schedule_create", "battery_schedule_update"]
)
async def test_schedule_crud_waits_for_gateway_acknowledgement(
    coordinator_factory, control
):
    coord = coordinator_factory()
    requested = requested_control_values(
        control,
        {"schedule_type": "cfg", "start_time": "08:00", "end_time": "09:00"},
        coord,
    )
    update = coord.control_updates.begin(control, None, requested, {})
    coord.control_updates.finish((control, None), update)
    detail = {
        "scheduleId": "one",
        "startTime": "08:00",
        "endTime": "09:00",
        "scheduleStatus": "pending",
    }
    coord.client.battery_schedules = AsyncMock(
        return_value={"cfg": {"details": [detail]}}
    )
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "pending"
    detail["scheduleStatus"] = "active"
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["cfg", "dtg", "rbd"])
async def test_schedule_enable_confirmation_requires_fresh_family_settings(
    coordinator_factory, schedule_type
):
    coord = coordinator_factory()
    requested = requested_control_values(
        "battery_schedule_create",
        {"schedule_type": schedule_type, "is_enabled": False},
        coord,
    )
    update = coord.control_updates.begin("battery_schedule_create", None, requested, {})
    coord.control_updates.finish(("battery_schedule_create", None), update)
    coord.client.battery_schedules = AsyncMock(
        return_value={
            schedule_type: {
                "details": [{"scheduleId": "one", "isEnabled": True}],
                "scheduleStatus": "active",
            }
        }
    )
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "pending"
    coord.client.battery_settings_details = AsyncMock(return_value={"data": {}})
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert update.status == "pending"
    coord.client.battery_settings_details.return_value = {
        "data": (
            {"chargeFromGridScheduleEnabled": False}
            if schedule_type == "cfg"
            else {schedule_type + "Control": {"enabled": False}}
        )
    }
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert update.status == "confirmed"


def test_empty_schedule_inventory_requires_explicit_count_or_details(
    coordinator_factory,
):
    from custom_components.enphase_ev.control_values import fresh_control_values

    coord = coordinator_factory()
    coord._battery_schedules_payload = {
        "rbd": {"count": 0, "scheduleStatus": "pending"}
    }
    values = fresh_control_values(coord, "battery_schedule_delete", "battery_schedules")
    requested = {"deleted_schedule": "one", "deleted_schedule_type": "rbd"}
    assert not matches_requested(requested, values)
    coord._battery_schedules_payload["rbd"]["scheduleStatus"] = "active"
    values = fresh_control_values(coord, "battery_schedule_delete", "battery_schedules")
    assert matches_requested(requested, values)
    assert not matches_requested(requested, {"schedules": []})


def test_fresh_schedule_inventory_retains_only_returned_fields(coordinator_factory):
    from custom_components.enphase_ev.control_values import fresh_control_values

    coord = coordinator_factory()
    coord._battery_schedules_payload = {"cfg": {"details": [{"scheduleId": "one"}]}}
    values = fresh_control_values(coord, "battery_schedule_update", "battery_schedules")
    assert values["schedules"] == [
        {"schedule_id": "one", "schedule_type": "cfg", "schedule_ready": True}
    ]
    coord._battery_schedules_payload["cfg"]["details"][0].update(
        startTime="08:00:00",
        endTime="09:00",
        limit="80",
        days=[3, 1],
        timezone="Australia/Melbourne",
        isEnabled=False,
    )
    values = fresh_control_values(coord, "battery_schedule_update", "battery_schedules")
    assert values["schedules"][0] == {
        "schedule_id": "one",
        "schedule_type": "cfg",
        "schedule_ready": True,
        "start_time": "08:00",
        "end_time": "09:00",
        "limit": 80,
        "days": [1, 3],
        "timezone": "Australia/Melbourne",
        "enabled": False,
    }


def test_confirmed_group_retains_read_values_for_the_next_request(tracker):
    _coord, runtime, _timers = tracker
    first = runtime.begin("profile", None, {"mode": "new"}, {}, group="battery")
    runtime.hold_related("reserve", None, first, {"reserve": 20})
    runtime.finish(("profile", None), first)
    runtime.observe("profile", None, {"mode": "new"}, runtime.read_tokens())
    assert first.status == "confirmed"
    runtime.hold_related("reserve", None, first, {"reserve": 100})
    next_update = runtime.begin(
        "reserve", None, {"reserve": 25}, {"reserve": 100}, group="battery"
    )
    assert runtime.value("reserve", "reserve", 100) == 20
    runtime.hold_related("reserve", None, next_update, {"reserve": 100})
    runtime.observe("reserve", None, {"reserve": 25}, runtime.read_tokens())
    runtime.finish(("reserve", None), next_update)
    assert runtime.value("reserve", "reserve", 100) == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["cfg", "dtg", "rbd"])
async def test_schedule_toggle_cannot_use_the_entry_enable_echo(
    coordinator_factory, schedule_type
):
    coord = coordinator_factory()
    control = schedule_type + "_schedule"
    update = coord.control_updates.begin(
        control, None, {"enabled": True, "schedule_ready": True}, {"enabled": False}
    )
    coord.control_updates.finish((control, None), update)
    coord.client.battery_schedules = AsyncMock(
        return_value={
            schedule_type: {
                "details": [{"scheduleId": "one", "isEnabled": True}],
                "scheduleStatus": "active",
            }
        }
    )
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "pending"
    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": (
                {"chargeFromGridScheduleEnabled": True}
                if schedule_type == "cfg"
                else {schedule_type + "Control": {"enabled": True}}
            )
        }
    )
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert update.status == "confirmed"


@pytest.mark.asyncio
async def test_internal_mode_enforcement_retains_confirmed_values(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    runtime = coord.evse_runtime
    runtime.set_charge_mode_cache("EV1", "MANUAL_CHARGING")
    coord.client.set_charge_mode = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    await runtime.async_ensure_charge_mode("EV1", "SCHEDULED_CHARGING")
    assert runtime.state._charge_mode_cache["EV1"][0] == "MANUAL_CHARGING"
    assert coord.control_updates.status("EV1") == "pending"
    await runtime.async_ensure_charge_mode("EV1", "GREEN_CHARGING")
    coord.client.set_charge_mode.assert_awaited_once()
    coord.client.charge_mode = AsyncMock(return_value="SCHEDULED_CHARGING")
    await runtime.async_get_charge_mode("EV1")
    assert coord.control_updates.status("EV1") == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response", [None, {}, [{"key": DEFAULT_CHARGE_LEVEL_SETTING}]]
)
async def test_malformed_charger_config_cannot_confirm_a_cached_value(
    coordinator_factory, response
):
    coord = coordinator_factory(serials=["EV1"])
    coord._charger_config_cache["EV1"] = (
        {DEFAULT_CHARGE_LEVEL_SETTING: 16},
        updates_mod.time.monotonic(),
    )
    coord.client.set_default_charge_level = AsyncMock(return_value={})
    coord.async_request_refresh = AsyncMock()
    await coord.evse_runtime.async_set_default_charge_level("EV1", 16)
    coord.client.charger_config = AsyncMock(return_value=response)
    await coord.evse_runtime.async_get_charger_config(
        "EV1", keys=[DEFAULT_CHARGE_LEVEL_SETTING]
    )
    assert coord.control_updates.status("EV1") == "pending"
    coord.client.charger_config.return_value = [
        {"key": DEFAULT_CHARGE_LEVEL_SETTING, "value": 16}
    ]
    coord._charger_config_backoff_until.clear()
    await coord.evse_runtime.async_get_charger_config(
        "EV1", keys=[DEFAULT_CHARGE_LEVEL_SETTING]
    )
    assert coord.control_updates.status("EV1") == "idle"


@pytest.mark.asyncio
async def test_other_charger_config_keys_cannot_confirm_default_charge_level(
    coordinator_factory,
):
    coord = coordinator_factory(serials=["EV1"])
    coord._charger_config_cache["EV1"] = (
        {DEFAULT_CHARGE_LEVEL_SETTING: 16},
        updates_mod.time.monotonic(),
    )
    update = coord.control_updates.begin(
        "default_charge_level", "EV1", {"amps": 16}, {}
    )
    coord.control_updates.finish(("default_charge_level", "EV1"), update)
    coord.client.charger_config = AsyncMock(return_value=[{"key": "other", "value": 1}])
    await coord.evse_runtime.async_get_charger_config("EV1", keys=["other"])
    assert update.status == "pending"


@pytest.mark.asyncio
async def test_storm_alert_confirmation_requires_explicit_fresh_alert_state(
    coordinator_factory,
):
    coord = coordinator_factory()
    coord._storm_alert_active = False
    update = coord.control_updates.begin(
        "storm_alert_opt_out", None, {"active_alerts": False}, {"active_alerts": True}
    )
    coord.control_updates.finish(("storm_alert_opt_out", None), update)
    coord.client.storm_guard_alert = AsyncMock(return_value={})
    await coord.battery_runtime.async_refresh_storm_alert(force=True)
    assert update.status == "pending"
    coord.client.storm_guard_alert.return_value = {"stormAlerts": []}
    await coord.battery_runtime.async_refresh_storm_alert(force=True)
    assert update.status == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["cfg", "dtg", "rbd"])
async def test_disabled_empty_schedule_confirms_after_gateway_acknowledgement(
    coordinator_factory, schedule_type
):
    coord = coordinator_factory()
    control = schedule_type + "_schedule"
    update = coord.control_updates.begin(
        control,
        None,
        requested_control_values(control, {"enabled": False}, coord),
        {"enabled": True},
    )
    coord.control_updates.finish((control, None), update)
    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": (
                {"chargeFromGridScheduleEnabled": False}
                if schedule_type == "cfg"
                else {schedule_type + "Control": {"enabled": False}}
            )
        }
    )
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert update.status == "pending"
    family = {"count": 0, "scheduleStatus": "pending"}
    coord.client.battery_schedules = AsyncMock(return_value={schedule_type: family})
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "pending"
    family["scheduleStatus"] = "active"
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, [], {}, [{"serial_num": "122532006376"}]])
async def test_grid_profile_confirmation_requires_explicit_target_feedback(
    coordinator_factory, response
):
    from tests.components.enphase_ev.test_grid_profile_runtime import (
        _FakeGridProfileClient,
    )
    from custom_components.enphase_ev.grid_profile_runtime import GridProfileRuntime

    client = _FakeGridProfileClient()
    coord = coordinator_factory(serials=[], client=client)
    runtime = GridProfileRuntime(coord)
    coord.grid_profile_runtime = runtime
    runtime._start_pending_refresh = Mock()
    await runtime.async_refresh(force=True)
    await runtime.async_refresh_device_status(force=True)
    client.async_get_activation_device_list = AsyncMock(return_value=response)
    # Applying the cached current profile must still require fresh feedback.
    await runtime.async_apply_grid_profile("agf:common", region_code="VIC")
    update = coord.control_updates.updates[("grid_profile", None)]
    assert update.status == "pending"
    assert update.observed is None
    client.async_get_activation_device_list.return_value = [
        {"serial_num": "122532006376", "grid_profile_id": "agf:common"},
        {"serial_num": "other-gateway", "grid_profile_id": "agf:other"},
    ]
    await runtime.async_refresh_device_status(force=True)
    assert update.status == "confirmed"
    assert update.observed == {
        "profile_id": "agf:common",
        "gateway_serial": "122532006376",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "update", "replace", "enabled"])
async def test_schedule_confirmation_accepts_equivalent_wire_values(
    coordinator_factory, action
):
    coord = coordinator_factory(serials=["EV1"])
    sync = coord.schedule_sync
    sync._sync_enabled = Mock(return_value=True)
    sync._schedule_post_patch_refresh = Mock()
    slot = {
        "id": "123",
        "enabled": True,
        "startTime": "08:00",
        "endTime": "09:30",
        "days": [3, 1],
        "scheduleType": "CUSTOM",
        "chargingLevelAmp": 24,
    }
    sync._slot_cache["EV1"] = {"123": dict(slot, enabled=False)}
    sync._meta_cache["EV1"] = "timestamp"
    coord.client.create_schedule = AsyncMock(return_value={"data": {"id": 123}})
    coord.client.patch_schedule = AsyncMock(return_value={})
    coord.client.patch_schedules = AsyncMock(return_value={})
    coord.client.patch_schedule_states = AsyncMock(return_value={})
    if action == "create":
        sync._slot_cache["EV1"] = {}
        assert await sync.async_upsert_slot(
            "EV1", {k: v for k, v in slot.items() if k != "id"}
        )
        control = "evse_schedule_save"
    elif action == "update":
        assert await sync.async_upsert_slot("EV1", slot)
        control = "evse_schedule_save"
    elif action == "replace":
        assert await sync.async_replace_slots("EV1", [slot])
        control = "evse_schedule_replace"
    else:
        assert await sync.async_set_slot_enabled("EV1", "123", True)
        control = "evse_schedule_enabled"
    update = coord.control_updates.updates[(control, "EV1")]
    assert update.status == "pending"
    feedback = dict(
        slot,
        id=123,
        enabled="true",
        startTime="08:00:00",
        endTime="09:30:00",
        days=[1, 3],
        scheduleType="custom",
        chargingLevelAmp="24",
    )
    coord.client.get_schedules = AsyncMock(return_value={"slots": [feedback]})
    await sync._async_fetch_serial_sync("EV1")
    assert update.status == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reserve", "savings"])
async def test_profile_follow_up_uses_confirmed_companions_after_timeout(
    coordinator_factory, action
):
    from custom_components.enphase_ev.number import BatteryReserveNumber
    from custom_components.enphase_ev.switch import SavingsUseBatteryAfterPeakSwitch
    from custom_components.enphase_ev.control_values import RELATED_CONTROLS

    coord = coordinator_factory()
    coord.last_update_success = True
    coord._battery_profile = "cost_savings"
    coord._battery_backup_percentage = 20
    coord._battery_show_battery_backup_percentage = True
    coord._battery_show_savings_mode = True
    coord._battery_user_is_owner = True
    ledger = coord.control_updates
    update = ledger.begin(
        "system_profile",
        None,
        {"profile_key": "backup_only"},
        confirmed_control_values(coord, "system_profile"),
        group="battery_profile",
    )
    for control in RELATED_CONTROLS["battery_profile"]:
        ledger.hold_related(
            control, None, update, confirmed_control_values(coord, control)
        )
    coord.battery_runtime.set_battery_pending(
        profile="backup_only", reserve=100, sub_type=None, require_exact_settings=False
    )
    ledger.finish(("system_profile", None), update)
    update.status = "unconfirmed"
    assert BatteryReserveNumber(coord).available
    assert BatteryReserveNumber(coord).native_value == 20
    assert SavingsUseBatteryAfterPeakSwitch(coord).available
    coord.async_request_refresh = AsyncMock()
    if action == "reserve":
        coord.client.set_battery_settings_compat = AsyncMock()
        await coord.battery_runtime.async_set_battery_reserve(25)
        coord.client.set_battery_settings_compat.assert_awaited_once_with(
            {"batteryBackupPercentage": 25}, merged_payload=True, strip_devices=True
        )
        assert coord.battery_pending_profile == "cost_savings"
    else:
        coord.client.set_battery_profile = AsyncMock()
        await coord.battery_runtime.async_set_savings_use_battery_after_peak(True)
        coord.client.set_battery_profile.assert_awaited_once_with(
            profile="cost_savings",
            battery_backup_percentage=20,
            operation_mode_sub_type=SAVINGS_OPERATION_MODE_SUBTYPE,
            devices=None,
        )


@pytest.mark.parametrize(
    "feedback",
    [
        None,
        {},
        {"scheduleType": None},
        {"scheduleType": "OFF_PEAK", "days": None},
        {"scheduleType": "OFF_PEAK", "days": "invalid"},
    ],
)
def test_schedule_confirmation_does_not_infer_missing_or_malformed_fields(
    coordinator_factory, feedback
):
    coord = coordinator_factory()
    requested = requested_control_values(
        "evse_schedule_save", {"slot": {"scheduleType": "OFF_PEAK"}}, coord
    )
    assert not matches_requested(requested, {"slots": {"one": feedback}})
    assert matches_requested(
        requested,
        {"slots": {"one": {"scheduleType": "OFF_PEAK", "days": list(range(1, 8))}}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("read_finishes_before_ack", [True, False])
async def test_cfg_toggle_requires_schedule_read_started_after_write_ack(
    coordinator_factory, read_finishes_before_ack
):
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_has_encharge = True
    coord._battery_hide_charge_from_grid = False
    coord._battery_charge_from_grid = True
    coord._battery_charge_from_grid_schedule_enabled = True
    coord._battery_charge_begin_time = 120
    coord._battery_charge_end_time = 300
    coord.async_request_refresh = AsyncMock()
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"chargeFromGridScheduleEnabled": False}}
    )
    write_entered, write_release = asyncio.Event(), asyncio.Event()
    read_entered, read_release = asyncio.Event(), asyncio.Event()

    async def write(payload):
        assert payload["chargeFromGridScheduleEnabled"] is False
        write_entered.set()
        await write_release.wait()

    async def read():
        read_entered.set()
        await read_release.wait()
        return {"cfg": {"count": 0, "scheduleStatus": "active"}}

    coord.client.set_battery_settings = write
    coord.client.battery_schedules = read
    command = asyncio.create_task(
        runtime.async_set_charge_from_grid_schedule_enabled(False)
    )
    await write_entered.wait()
    lookup = asyncio.create_task(runtime.async_refresh_battery_schedules(force=True))
    await read_entered.wait()
    if read_finishes_before_ack:
        read_release.set()
        await lookup
    write_release.set()
    await command
    read_release.set()
    await lookup
    update = coord.control_updates.updates[("cfg_schedule", None)]
    assert update.status == "pending"
    coord.client.battery_schedules = AsyncMock(
        return_value={"cfg": {"count": 0, "scheduleStatus": "pending"}}
    )
    await runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "pending"
    coord.client.battery_schedules.return_value["cfg"]["scheduleStatus"] = "active"
    await runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "confirmed"


@pytest.mark.asyncio
async def test_delete_schedule_service_guards_and_confirms_the_whole_batch(
    hass, config_entry, coordinator_factory, monkeypatch
):
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.core import ServiceCall
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
    from custom_components.enphase_ev.services import async_setup_services
    from tests.components.enphase_ev.test_battery_schedule_editor_parity import (
        _prepare_battery_schedule_coord,
    )

    coord = coordinator_factory()
    _prepare_battery_schedule_coord(coord)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)
    registered = {}

    def register(_self, domain, service, handler, *_args, **_kwargs):
        registered[(domain, service)] = handler

    monkeypatch.setattr(hass.services.__class__, "async_register", register)
    async_setup_services(hass)
    delete = registered[(DOMAIN, "delete_schedule")]
    call = ServiceCall(
        hass,
        DOMAIN,
        "delete_schedule",
        {
            "config_entry_id": config_entry.entry_id,
            "schedule_ids": ["abc123", "def456"],
            "confirm": True,
        },
    )
    ledger = coord.control_updates
    pending = ledger.begin(
        "charge_from_grid", None, {"enabled": True}, {}, group="battery_settings"
    )
    ledger.finish(("charge_from_grid", None), pending)
    with pytest.raises(ServiceValidationError, match="awaiting confirmation"):
        await delete(call)
    coord.client.delete_battery_schedule.assert_not_awaited()
    ledger.cleanup()
    await delete(call)
    assert coord.client.delete_battery_schedule.await_count == 2
    coord.client.delete_battery_schedule.assert_any_await("abc123", schedule_type="cfg")
    coord.client.delete_battery_schedule.assert_any_await("def456", schedule_type="dtg")
    update = ledger.updates[("battery_schedule_delete", None)]
    assert update.status == "pending"
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "pending"  # Still-present records cannot confirm deletion.
    coord.client.battery_schedules = AsyncMock(
        return_value={"cfg": {"count": 0, "scheduleStatus": "active"}}
    )
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "pending"  # Both families must acknowledge deletion.
    coord.client.battery_schedules.return_value["dtg"] = {
        "count": 0,
        "scheduleStatus": "pending",
    }
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "pending"
    coord.client.battery_schedules.return_value["dtg"]["scheduleStatus"] = "active"
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    assert update.status == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_id", ["abc123", "def456"])
async def test_update_schedule_service_tracks_settings_commit_failure(
    hass, config_entry, coordinator_factory, monkeypatch, schedule_id
):
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.core import ServiceCall
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
    from custom_components.enphase_ev.services import async_setup_services
    from tests.components.enphase_ev.test_battery_schedule_editor_parity import (
        _prepare_battery_schedule_coord,
    )

    coord = coordinator_factory()
    payload = _prepare_battery_schedule_coord(coord)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)
    registered = {}

    def register(_self, domain, service, handler, *_args, **_kwargs):
        registered[(domain, service)] = handler

    monkeypatch.setattr(hass.services.__class__, "async_register", register)
    async_setup_services(hass)
    entered, release = asyncio.Event(), asyncio.Event()

    async def commit(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("Settings commit failed")

    coord.client.set_battery_settings = commit
    call = ServiceCall(
        hass,
        DOMAIN,
        "update_schedule",
        {
            "config_entry_id": config_entry.entry_id,
            "schedule_id": schedule_id,
            "start_time": dt_time(4 if schedule_id == "abc123" else 19, 0),
            "end_time": dt_time(5 if schedule_id == "abc123" else 22, 0),
            "limit": 41,
            "days": [2, 4],
            "confirm": True,
        },
    )
    command = asyncio.create_task(registered[(DOMAIN, "update_schedule")](call))
    await asyncio.wait_for(entered.wait(), timeout=5)
    ledger = coord.control_updates
    update = ledger.updates[("battery_schedule_update", None)]
    still_submitting = update.submitting
    family = payload["cfg" if schedule_id == "abc123" else "dtg"]
    family["scheduleStatus"] = "active"
    family["details"][0].update(
        startTime=call.data["start_time"].strftime("%H:%M"),
        endTime=call.data["end_time"].strftime("%H:%M"),
        limit=41,
        days=[2, 4],
    )
    await coord.battery_runtime.async_refresh_battery_schedules(force=True)
    still_pending = update.status == "pending"
    release.set()
    with pytest.raises(RuntimeError, match="Settings commit failed"):
        await command
    assert update.status == "failed"
    assert still_submitting
    assert still_pending
    retry = ledger.begin(
        "charge_from_grid", None, {"enabled": True}, {}, group="battery_settings"
    )
    assert retry.status == "pending"
