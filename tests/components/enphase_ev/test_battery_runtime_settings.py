from __future__ import annotations

import time
from datetime import datetime, time as dt_time, timedelta, timezone
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, call

import aiohttp
import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.enphase_ev.state_models import BatteryControlCapability


class _MonotonicSequence:
    def __init__(self, *values: float) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> float:
        if self._index < len(self._values):
            value = self._values[self._index]
            self._index += 1
            return value
        return self._values[-1]


def test_parse_battery_settings_payload_maps_mode_and_controls(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "batteryGridMode": "ImportExport",
                "hideChargeFromGrid": False,
                "envoySupportsVls": True,
                "chargeFromGrid": True,
                "chargeFromGridScheduleEnabled": True,
                "chargeBeginTime": 120,
                "chargeEndTime": 300,
                "acceptedItcDisclaimer": "2026-02-08T10:00:00+00:00",
                "veryLowSoc": 15,
                "veryLowSocMin": 10,
                "veryLowSocMax": 25,
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "batteryBackupPercentageMin": 8,
                "batteryBackupPercentageMax": 95,
                "stormGuardState": "enabled",
                "cfgControl": {
                    "show": True,
                    "enabled": True,
                    "locked": False,
                    "showDaySchedule": True,
                    "scheduleSupported": True,
                    "forceScheduleSupported": True,
                    "forceScheduleOpted": True,
                },
                "dtgControl": {
                    "show": True,
                    "enabled": False,
                    "locked": True,
                },
                "rbdControl": {
                    "show": True,
                    "enabled": True,
                    "locked": False,
                },
                "powerMatchControl": {
                    "show": True,
                    "enabled": "true",
                },
                "devices": {
                    "iqEvse": {
                        "useBatteryFrSelfConsumption": True,
                    }
                },
                "systemTask": False,
            }
        }
    )

    assert coord.battery_mode_display == "Import and Export"
    assert coord.battery_charge_from_grid_allowed is True
    assert coord.battery_discharge_to_grid_allowed is True
    assert coord.battery_charge_from_grid_enabled is True
    assert coord.battery_charge_from_grid_schedule_enabled is True
    assert coord.battery_charge_from_grid_start_time == dt_time(2, 0)
    assert coord.battery_charge_from_grid_end_time == dt_time(5, 0)
    assert coord.battery_shutdown_level == 15
    assert coord.battery_shutdown_level_min == 10
    assert coord.battery_shutdown_level_max == 25
    assert coord.battery_shutdown_level_available is True
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_effective_backup_percentage == 20
    assert coord.battery_reserve_min == 8
    assert coord.battery_reserve_max == 95
    assert coord.storm_guard_state == "enabled"
    assert coord.battery_cfg_control_show is True
    assert coord.battery_cfg_control_enabled is True
    assert coord.battery_cfg_control_schedule_supported is True
    assert coord.battery_cfg_control_force_schedule_supported is True
    assert coord.battery_cfg_control_locked is False
    assert coord.battery_cfg_control_show_day_schedule is True
    assert coord.battery_cfg_control_force_schedule_opted is True
    assert coord.battery_dtg_control_enabled is False
    assert coord.battery_rbd_control_enabled is True
    assert coord.battery_dtg_control == {
        "show": True,
        "enabled": False,
        "locked": True,
        "show_day_schedule": None,
        "schedule_supported": None,
        "force_schedule_supported": None,
        "force_schedule_opted": None,
    }
    assert coord.battery_rbd_control == {
        "show": True,
        "enabled": True,
        "locked": False,
        "show_day_schedule": None,
        "schedule_supported": None,
        "force_schedule_supported": None,
        "force_schedule_opted": None,
    }
    assert coord.battery_power_match_control == {
        "show": True,
        "enabled": True,
        "locked": None,
        "show_day_schedule": None,
        "schedule_supported": None,
        "force_schedule_supported": None,
        "force_schedule_opted": None,
    }
    assert coord.battery_power_match_enabled is True
    assert coord.power_match_control_available is True
    assert coord.battery_system_task is False
    assert coord.battery_use_battery_for_self_consumption is True


def test_power_match_capability_requires_authoritative_usable_control(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001

    runtime.parse_battery_settings_payload(
        {"data": {"powerMatchControl": {"show": True, "enabled": False}}},
        clear_missing_controls=True,
    )
    assert coord.battery_power_match_enabled is False
    assert coord.power_match_control_available is True

    runtime.parse_battery_settings_payload({"data": {"chargeFromGrid": True}})
    assert coord.battery_power_match_enabled is False

    runtime.parse_battery_settings_payload(
        {"data": {"powerMatchControl": {"enabled": True}}}
    )
    assert coord.battery_power_match_enabled is True
    assert coord.battery_power_match_control["show"] is True
    assert coord.power_match_control_available is True

    runtime.parse_battery_settings_payload(
        {"data": {"powerMatchControl": {"show": False, "enabled": True}}},
        clear_missing_controls=True,
    )
    assert coord.power_match_control_available is False

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "powerMatchControl": {"show": True, "enabled": True, "locked": True}
            }
        },
        clear_missing_controls=True,
    )
    assert coord.power_match_control_available is False

    runtime.parse_battery_settings_payload(
        {"data": {"powerMatchControl": {"show": True, "enabled": "unknown"}}},
        clear_missing_controls=True,
    )
    assert coord.battery_power_match_enabled is None
    assert coord.power_match_control_available is False

    runtime.parse_battery_settings_payload(
        {"data": {"powerMatchControl": 42}}, clear_missing_controls=True
    )
    assert coord.battery_power_match_control is None

    runtime.parse_battery_settings_payload(
        {"data": {"powerMatchControl": {"show": True, "enabled": True}}},
        clear_missing_controls=True,
    )
    runtime.parse_battery_settings_payload(
        {"data": {"chargeFromGrid": False}}, clear_missing_controls=True
    )
    assert coord.battery_power_match_control is None


def test_dtg_and_rbd_control_enabled_are_distinct_from_schedule_enabled(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "batteryGridMode": "ImportExport",
                "dtgControl": {
                    "show": True,
                    "enabled": False,
                    "locked": False,
                    "showDaySchedule": True,
                    "scheduleSupported": True,
                    "startTime": 960,
                    "endTime": 1140,
                },
                "rbdControl": {
                    "show": True,
                    "enabled": False,
                    "locked": False,
                    "showDaySchedule": True,
                    "scheduleSupported": True,
                },
            }
        }
    )
    runtime.parse_battery_schedules_payload(
        {
            "dtg": {
                "details": [
                    {
                        "scheduleId": "sched-dtg",
                        "startTime": "18:00",
                        "endTime": "23:00",
                        "limit": 5,
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "timezone": "Europe/London",
                        "isEnabled": True,
                    }
                ]
            },
            "rbd": {
                "details": [
                    {
                        "scheduleId": "sched-rbd",
                        "startTime": "01:00",
                        "endTime": "16:00",
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "timezone": "Europe/London",
                        "isEnabled": True,
                    }
                ]
            },
        }
    )

    assert coord.battery_dtg_control_enabled is False
    assert coord.battery_rbd_control_enabled is False
    assert coord.battery_discharge_to_grid_schedule_enabled is False
    assert coord.battery_restrict_battery_discharge_schedule_enabled is False


def test_normalize_schedule_minutes_falls_back_without_coordinator_helpers(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.normalize_minutes_of_day = None
    coord._normalize_minutes_of_day = None  # noqa: SLF001
    runtime = coord.battery_runtime

    assert runtime._normalize_schedule_minutes(None) is None  # noqa: SLF001
    assert runtime._normalize_schedule_minutes("abc") is None  # noqa: SLF001
    assert runtime._normalize_schedule_minutes(1440) is None  # noqa: SLF001
    assert runtime._normalize_schedule_minutes("75") == 75  # noqa: SLF001
    assert runtime._normalize_schedule_minutes("01:15") == 75  # noqa: SLF001


def test_normalize_schedule_minutes_rejects_bad_hhmm_values(
    coordinator_factory,
) -> None:
    runtime = coordinator_factory().battery_runtime

    assert runtime._normalize_schedule_minutes("aa:15") is None  # noqa: SLF001
    assert runtime._normalize_schedule_minutes("24:00") is None  # noqa: SLF001


def test_schedule_family_helper_defaults_and_cfg_window(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_timezone = "Australia/Sydney"  # noqa: SLF001
    coord._battery_very_low_soc = 8  # noqa: SLF001
    runtime = coord.battery_runtime

    assert runtime._current_battery_schedule_window_for_type("cfg") == (
        120,
        300,
    )  # noqa: SLF001
    assert runtime._schedule_default_limit_for_create("cfg") == 100  # noqa: SLF001
    assert runtime._schedule_default_limit_for_create("dtg") == 8  # noqa: SLF001
    assert (
        runtime._schedule_family_timezone("dtg") == "Australia/Sydney"
    )  # noqa: SLF001
    assert runtime._schedule_default_window_for_create("rbd") == (
        60,
        960,
    )  # noqa: SLF001


def test_battery_control_refresh_helpers_cover_pending_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001
    assert runtime._battery_control_state_settling() is True  # noqa: SLF001
    assert (
        runtime._battery_control_refresh_success_ttl_seconds(300.0) == 0.0
    )  # noqa: SLF001

    coord._battery_cfg_schedule_status = "active"  # noqa: SLF001
    coord._battery_rbd_schedule_status = "pending"  # noqa: SLF001
    assert runtime._battery_control_state_settling() is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_create_or_update_schedule_family_rejects_missing_create_support(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_schedules_payload = None  # noqa: SLF001

    with pytest.raises(
        ServiceValidationError,
        match="No existing discharge to grid schedule is available",
    ):
        await runtime._async_create_or_update_schedule_family(  # noqa: SLF001
            "dtg",
            start_minutes=60,
            end_minutes=120,
            limit=5,
            is_enabled=True,
        )


@pytest.mark.asyncio
async def test_dtg_schedule_enabled_rejects_guard_branches(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=False,
    )
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.async_set_discharge_to_grid_schedule_enabled(True)

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
    )
    with pytest.raises(ServiceValidationError, match="time is invalid"):
        await coord.async_set_discharge_to_grid_schedule_enabled(True)

    coord._battery_dtg_control_begin_time = 60  # noqa: SLF001
    coord._battery_dtg_control_end_time = 60  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.async_set_discharge_to_grid_schedule_enabled(True)


@pytest.mark.asyncio
async def test_cfg_schedule_enabled_rejects_pending_and_equal_window(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
    )
    coord._battery_cfg_control_force_schedule_supported = True  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="pending Envoy sync"):
        await coord.battery_runtime._async_set_schedule_family_enabled(  # noqa: SLF001
            "cfg", True
        )

    coord._battery_cfg_schedule_status = "active"  # noqa: SLF001
    coord._battery_charge_begin_time = 60  # noqa: SLF001
    coord._battery_charge_end_time = 60  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.battery_runtime._async_set_schedule_family_enabled(  # noqa: SLF001
            "cfg", True
        )


@pytest.mark.asyncio
async def test_rbd_schedule_time_uses_default_window_when_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
    )
    coord.client.create_battery_schedule = AsyncMock(return_value={})
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.client.battery_schedules = AsyncMock(return_value={})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord._battery_schedules_payload = {}  # noqa: SLF001

    await coord.async_set_restrict_battery_discharge_schedule_time(start=dt_time(2, 0))

    coord.client.create_battery_schedule.assert_awaited_once_with(
        schedule_type="RBD",
        start_time="02:00",
        end_time="16:00",
        limit=None,
        days=[1, 2, 3, 4, 5, 6, 7],
        timezone="UTC",
        is_enabled=None,
    )
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "rbdControl": {
                "enabled": False,
                "show": True,
                "locked": False,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 120,
                "endTime": 960,
            }
        },
        schedule_type="rbd",
    )
    coord.client.create_battery_schedule.reset_mock()
    coord.client.set_battery_settings.reset_mock()
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
    )
    coord.client.create_battery_schedule = AsyncMock(return_value={})
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.client.battery_schedules = AsyncMock(return_value={})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord._battery_schedules_payload = {}  # noqa: SLF001

    await coord.async_set_restrict_battery_discharge_schedule_time(end=dt_time(15, 0))

    coord.client.create_battery_schedule.assert_awaited_once_with(
        schedule_type="RBD",
        start_time="01:00",
        end_time="15:00",
        limit=None,
        days=[1, 2, 3, 4, 5, 6, 7],
        timezone="UTC",
        is_enabled=None,
    )
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "rbdControl": {
                "enabled": False,
                "show": True,
                "locked": False,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 60,
                "endTime": 900,
            }
        },
        schedule_type="rbd",
    )


@pytest.mark.asyncio
async def test_dtg_schedule_enabled_allows_toggle_while_schedule_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord._battery_dtg_schedule_status = "pending"  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={})
    coord.battery_runtime._async_verify_schedule_family_toggle_applied = (
        AsyncMock()
    )  # noqa: SLF001

    await coord.async_set_discharge_to_grid_schedule_enabled(False)

    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": False,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 1080,
                "endTime": 1380,
            }
        },
        schedule_type="dtg",
    )


@pytest.mark.asyncio
async def test_dtg_schedule_time_rejects_guard_branches(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=False,
    )
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.async_set_discharge_to_grid_schedule_time(start=dt_time(1, 0))

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
    )
    coord._battery_dtg_schedule_status = "pending"  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="pending Envoy sync"):
        await coord.async_set_discharge_to_grid_schedule_time(start=dt_time(1, 0))

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
    )
    with pytest.raises(ServiceValidationError, match="time is invalid"):
        await coord.async_set_discharge_to_grid_schedule_time(start=dt_time(1, 0))

    coord._battery_dtg_control_begin_time = 60  # noqa: SLF001
    coord._battery_dtg_control_end_time = 120  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.async_set_discharge_to_grid_schedule_time(
            start=dt_time(2, 0), end=dt_time(2, 0)
        )


@pytest.mark.asyncio
async def test_dtg_schedule_limit_rejects_guard_branches(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=False,
    )
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.async_set_discharge_to_grid_schedule_limit(25)

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
    )
    coord._battery_dtg_schedule_status = "pending"  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="pending Envoy sync"):
        await coord.async_set_discharge_to_grid_schedule_limit(25)

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
    )
    with pytest.raises(ServiceValidationError, match="time is invalid"):
        await coord.async_set_discharge_to_grid_schedule_limit(25)

    coord._battery_dtg_control_begin_time = 60  # noqa: SLF001
    coord._battery_dtg_control_end_time = 120  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="between 5 and 100"):
        await coord.async_set_discharge_to_grid_schedule_limit(4)

    coord._battery_very_low_soc = 40  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="at least 40%"):
        await coord.async_set_discharge_to_grid_schedule_limit(25)


def test_battery_schedule_support_helpers_cover_false_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = False  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(show=True)
    assert coord.discharge_to_grid_schedule_supported is False

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_system_task = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(show=True)
    assert coord.discharge_to_grid_schedule_supported is False

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(show=False)
    assert coord.discharge_to_grid_schedule_supported is False

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(show=True, locked=True)
    assert coord.discharge_to_grid_schedule_supported is False

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(show=True)
    assert coord.discharge_to_grid_schedule_supported is False

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=False,
        schedule_supported=True,
    )
    assert coord.discharge_to_grid_schedule_supported is False

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_dtg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=None,
    )
    assert coord.discharge_to_grid_schedule_supported is False
    assert coord.discharge_to_grid_schedule_available is False


def test_rbd_time_properties_fall_back_to_control_window(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_rbd_control_begin_time = 60  # noqa: SLF001
    coord._battery_rbd_control_end_time = 120  # noqa: SLF001

    assert coord.battery_restrict_battery_discharge_start_time == dt_time(1, 0)
    assert coord.battery_restrict_battery_discharge_end_time == dt_time(2, 0)


def test_parse_battery_settings_payload_clears_pending_when_matching(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 35  # noqa: SLF001
    coord._battery_operation_mode_sub_type = None  # noqa: SLF001

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "profile": "cost_savings",
                "batteryBackupPercentage": 20,
                "operationModeSubType": "prioritize-energy",
            }
        }
    )

    assert coord.battery_profile_pending is False
    assert coord.battery_pending_profile is None


def test_battery_soc_min_floor_applies_to_reserve_and_shutdown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc_min = None  # noqa: SLF001
    coord._battery_very_low_soc_max = None  # noqa: SLF001

    assert coord.battery_reserve_min == 5
    assert coord.battery_shutdown_level_min == 5
    assert coord.battery_shutdown_level_available is True
    assert (
        coord._normalize_battery_reserve_for_profile("self-consumption", 1) == 5
    )  # noqa: SLF001

    coord._battery_very_low_soc_min = 7  # noqa: SLF001
    assert coord.battery_reserve_min == 7
    assert coord.battery_shutdown_level_min == 7
    assert (
        coord._normalize_battery_reserve_for_profile("cost_savings", 1) == 7
    )  # noqa: SLF001

    coord._battery_profile = "backup_only"  # noqa: SLF001
    assert coord.battery_reserve_min == 100
    assert coord.battery_reserve_max == 100


def test_parse_battery_settings_unknown_grid_mode_uses_none_permissions(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"batteryGridMode": "RegionalSpecial"}}
    )

    assert coord.battery_grid_mode == "RegionalSpecial"
    assert coord.battery_mode_display == "RegionalSpecial"
    assert coord.battery_charge_from_grid_allowed is None
    assert coord.battery_discharge_to_grid_allowed is None


@pytest.mark.asyncio
async def test_parse_battery_settings_localizes_grid_mode_label(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.labels import async_prime_label_translations

    coord = coordinator_factory()
    coord.hass.config.language = "fr"
    await async_prime_label_translations(coord.hass)

    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"batteryGridMode": "ImportExport"}}
    )

    assert coord.battery_mode_display == "Importation et exportation"


@pytest.mark.asyncio
async def test_refresh_battery_settings_caches_and_redacts(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": {"batteryGridMode": "ImportOnly", "chargeFromGrid": False},
            "userId": "123",
            "token": "secret-token",
        }
    )

    await coord.battery_runtime.async_refresh_battery_settings(force=True)

    assert coord.battery_grid_mode == "ImportOnly"
    assert coord._battery_settings_payload is not None  # noqa: SLF001
    assert coord._battery_settings_payload["userId"] == "[redacted]"  # noqa: SLF001
    assert coord._battery_settings_payload["token"] == "[redacted]"  # noqa: SLF001

    coord._battery_settings_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.battery_settings_details.reset_mock()
    await coord.battery_runtime.async_refresh_battery_settings()
    coord.client.battery_settings_details.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_battery_settings_preserves_pending_cfg_values_when_backend_stale(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = False  # noqa: SLF001
    coord._battery_charge_begin_time = None  # noqa: SLF001
    coord._battery_charge_end_time = None  # noqa: SLF001

    monotonic_now = 100.0
    monkeypatch.setattr(time, "monotonic", lambda: monotonic_now)
    runtime.set_cfg_settings_pending_from_payload({"chargeFromGrid": True})

    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"chargeFromGrid": False}}
    )

    await runtime.async_refresh_battery_settings(force=True)

    assert coord.battery_charge_from_grid_enabled is True
    assert (
        coord.battery_state._battery_cfg_pending_expires_mono == 160.0
    )  # noqa: SLF001

    monotonic_now = 200.0
    runtime.sync_cfg_settings_pending()
    assert coord.battery_state._battery_cfg_pending_expires_mono is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_settings_bypasses_cache_when_profile_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_settings_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_backup_percentage = 100  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
            }
        }
    )

    await coord.battery_runtime.async_refresh_battery_settings()

    coord.client.battery_settings_details.assert_awaited_once()
    assert coord.battery_profile_pending is False


@pytest.mark.asyncio
async def test_refresh_battery_settings_cache_ttl_tracks_polling_cadence(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.update_interval = timedelta(seconds=30)
    coord._configured_slow_poll_interval = 120  # noqa: SLF001
    coord._battery_polling_interval_s = 60  # noqa: SLF001
    coord._endpoint_family_should_run = lambda *args, **kwargs: True  # noqa: SLF001
    coord._note_endpoint_family_success = MagicMock()  # noqa: SLF001
    coord._note_endpoint_family_failure = lambda *args, **kwargs: None  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(
        side_effect=[
            {
                "data": {
                    "profile": "self-consumption",
                    "batteryBackupPercentage": 20,
                }
            },
            {
                "data": {
                    "profile": "backup_only",
                    "batteryBackupPercentage": 100,
                }
            },
        ]
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.time.monotonic",
        _MonotonicSequence(1000.0, 1059.0, 1061.0),
    )

    await coord.battery_runtime.async_refresh_battery_settings(force=True)

    assert coord.client.battery_settings_details.await_count == 1
    assert coord._battery_settings_cache_until == 1060.0  # noqa: SLF001
    coord._note_endpoint_family_success.assert_called_once_with(  # noqa: SLF001
        "battery_settings",
        success_ttl_s=60.0,
    )

    await coord.battery_runtime.async_refresh_battery_settings()

    assert coord.client.battery_settings_details.await_count == 1

    await coord.battery_runtime.async_refresh_battery_settings()

    assert coord.client.battery_settings_details.await_count == 2
    assert coord.battery_profile == "backup_only"


@pytest.mark.asyncio
async def test_refresh_battery_settings_uses_zero_success_ttl_while_settling(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._endpoint_family_should_run = lambda *args, **kwargs: True  # noqa: SLF001
    coord._note_endpoint_family_success = MagicMock()  # noqa: SLF001
    coord._note_endpoint_family_failure = lambda *args, **kwargs: None  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
            }
        }
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.time.monotonic",
        lambda: 1000.0,
    )
    coord._battery_settings_last_write_mono = 995.0  # noqa: SLF001

    await coord.battery_runtime.async_refresh_battery_settings(force=True)

    assert coord._battery_settings_cache_until == 1000.0  # noqa: SLF001
    coord._note_endpoint_family_success.assert_called_once_with(  # noqa: SLF001
        "battery_settings",
        success_ttl_s=0.0,
    )


@pytest.mark.asyncio
async def test_refresh_battery_settings_clears_schedule_times_when_null_or_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": {
                "chargeFromGrid": True,
                "chargeBeginTime": None,
                "chargeEndTime": None,
            }
        }
    )
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert coord._battery_charge_begin_time is None  # noqa: SLF001
    assert coord._battery_charge_end_time is None  # noqa: SLF001
    assert coord.charge_from_grid_schedule_supported is False

    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"chargeFromGrid": True}}
    )
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert coord._battery_charge_begin_time is None  # noqa: SLF001
    assert coord._battery_charge_end_time is None  # noqa: SLF001
    assert coord.charge_from_grid_schedule_supported is False


@pytest.mark.asyncio
async def test_set_charge_from_grid_enable_uses_disclaimer_ack_and_bool_marker(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = None  # noqa: SLF001
    coord._battery_charge_begin_time = None  # noqa: SLF001
    coord._battery_charge_end_time = None  # noqa: SLF001
    coord._battery_accepted_itc_disclaimer = "2026-02-08T10:00:00+00:00"  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.client.accept_battery_settings_disclaimer = AsyncMock(
        return_value={"message": "success"}
    )
    coord.client.validate_battery_schedule = AsyncMock(return_value={"isValid": True})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    fixed_now = datetime(2026, 2, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow", lambda: fixed_now
    )

    await coord.battery_runtime.async_set_charge_from_grid(True)

    args = coord.client.set_battery_settings.await_args.args
    payload = args[0]
    assert payload["chargeFromGrid"] is True
    assert payload["acceptedItcDisclaimer"] is True
    assert "chargeBeginTime" not in payload
    assert "chargeEndTime" not in payload
    assert "chargeFromGridScheduleEnabled" not in payload
    coord.client.accept_battery_settings_disclaimer.assert_awaited_once_with("itc")
    coord.client.validate_battery_schedule.assert_awaited_once_with("cfg")
    assert coord.battery_charge_from_grid_enabled is True
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_charge_from_grid_disable_omits_disclaimer(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.client.accept_battery_settings_disclaimer = AsyncMock(
        return_value={"message": "success"}
    )
    coord.client.validate_battery_schedule = AsyncMock(return_value={"isValid": True})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord.battery_runtime.async_set_charge_from_grid(False)

    payload = coord.client.set_battery_settings.await_args.args[0]
    assert payload == {"chargeFromGrid": False}
    coord.client.accept_battery_settings_disclaimer.assert_not_awaited()
    coord.client.validate_battery_schedule.assert_awaited_once_with("cfg")
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_power_match_uses_fresh_merged_settings_and_confirms(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(
        side_effect=[
            {
                "data": {
                    "powerMatchControl": {"show": True, "enabled": False},
                    "systemTask": False,
                }
            },
            {
                "data": {
                    "powerMatchControl": {"show": True, "enabled": True},
                    "systemTask": False,
                }
            },
        ]
    )
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.publish_runtime_state_update = MagicMock()

    await coord.battery_runtime.async_set_power_match(True)

    coord.client.set_battery_settings_compat.assert_awaited_once_with(
        {"powerMatchControl": {"enabled": True}},
        schedule_type="cfg",
        include_source=True,
        merged_payload=True,
        strip_devices=True,
    )
    assert coord.client.battery_settings_details.await_count == 2
    assert coord.battery_power_match_enabled is True
    assert coord.battery_settings_write_pending is False
    coord.async_request_refresh.assert_not_awaited()
    coord.publish_runtime_state_update.assert_called_once_with("power_match")


@pytest.mark.asyncio
async def test_set_power_match_noops_when_fresh_state_matches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(
        return_value={
            "data": {
                "powerMatchControl": {"show": True, "enabled": True},
                "systemTask": False,
            }
        }
    )
    coord.client.battery_site_settings = AsyncMock(
        return_value={"data": {"userDetails": {"isOwner": True, "isInstaller": False}}}
    )
    coord.client.set_battery_settings_compat = AsyncMock()
    coord.publish_runtime_state_update = MagicMock()

    await coord.battery_runtime.async_set_power_match(True)

    coord.client.battery_site_settings.assert_awaited_once_with()
    assert coord.battery_write_access_confirmed is True
    coord.client.set_battery_settings_compat.assert_not_awaited()
    coord.publish_runtime_state_update.assert_called_once_with("power_match")


@pytest.mark.asyncio
async def test_set_power_match_rejects_failed_or_unusable_preflight(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(ServiceValidationError, match="PowerMatch setting"):
        await coord.battery_runtime.async_set_power_match(True)

    coord.client.battery_settings_details = AsyncMock(
        return_value={"data": {"powerMatchControl": {"show": False, "enabled": False}}}
    )
    with pytest.raises(ServiceValidationError, match="PowerMatch setting"):
        await coord.battery_runtime.async_set_power_match(True)


@pytest.mark.asyncio
async def test_set_power_match_rejects_unconfirmed_server_state(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    stale = {
        "data": {
            "powerMatchControl": {"show": True, "enabled": False},
            "systemTask": False,
        }
    }
    coord.client.battery_settings_details = AsyncMock(return_value=stale)
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.publish_runtime_state_update = MagicMock()
    sleep = AsyncMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.asyncio.sleep", sleep
    )

    with pytest.raises(ServiceValidationError, match="was not applied"):
        await coord.battery_runtime.async_set_power_match(True)

    assert coord.client.battery_settings_details.await_count == 5
    assert sleep.await_count == 3
    assert coord.battery_power_match_enabled is False
    assert coord.battery_settings_write_pending is False
    coord.publish_runtime_state_update.assert_called_once_with("power_match")


@pytest.mark.asyncio
async def test_set_power_match_restores_authoritative_state_when_confirmation_fails(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord.client.battery_settings_details = AsyncMock(
        side_effect=[
            {
                "data": {
                    "powerMatchControl": {
                        "show": True,
                        "enabled": False,
                        "locked": False,
                    },
                    "systemTask": False,
                }
            },
            RuntimeError("confirmation unavailable"),
            RuntimeError("confirmation unavailable"),
            RuntimeError("confirmation unavailable"),
            RuntimeError("confirmation unavailable"),
        ]
    )
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.publish_runtime_state_update = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.asyncio.sleep", AsyncMock()
    )

    with pytest.raises(ServiceValidationError, match="was not applied"):
        await coord.battery_runtime.async_set_power_match(True)

    assert coord.battery_power_match_enabled is False
    assert coord.battery_power_match_control == {
        "show": True,
        "enabled": False,
        "locked": False,
        "show_day_schedule": None,
        "schedule_supported": None,
        "force_schedule_supported": None,
        "force_schedule_opted": None,
    }
    assert coord.battery_settings_write_pending is False
    coord.async_request_refresh.assert_not_awaited()
    coord.publish_runtime_state_update.assert_called_once_with("power_match")


@pytest.mark.asyncio
async def test_validate_cfg_schedule_commit_is_optional_for_missing_or_failing_client(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.validate_battery_schedule = None

    await coord.battery_runtime._async_validate_cfg_schedule_commit()  # noqa: SLF001

    coord.client.validate_battery_schedule = AsyncMock(
        side_effect=[
            aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=500,
                message="boom",
            ),
            RuntimeError("boom"),
        ]
    )

    await coord.battery_runtime._async_validate_cfg_schedule_commit()  # noqa: SLF001
    await coord.battery_runtime._async_validate_cfg_schedule_commit()  # noqa: SLF001

    coord.client.validate_battery_schedule = AsyncMock(return_value=["unexpected"])

    await coord.battery_runtime._async_validate_cfg_schedule_commit()  # noqa: SLF001


@pytest.mark.asyncio
async def test_validate_cfg_schedule_commit_raises_detail_when_backend_rejects(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.validate_battery_schedule = AsyncMock(
        return_value={"isValid": False, "message": "Invalid schedule"}
    )

    with pytest.raises(ServiceValidationError, match="Invalid schedule"):
        await coord.battery_runtime._async_validate_cfg_schedule_commit()  # noqa: SLF001


@pytest.mark.asyncio
async def test_validate_cfg_schedule_commit_raises_generic_error_without_detail(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.validate_battery_schedule = AsyncMock(return_value={"isValid": False})

    with pytest.raises(
        ServiceValidationError, match="Battery schedule validation was rejected"
    ):
        await coord.battery_runtime._async_validate_cfg_schedule_commit()  # noqa: SLF001


@pytest.mark.asyncio
async def test_accept_charge_from_grid_disclaimer_ignores_missing_endpoint(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.accept_battery_settings_disclaimer = None

    await coord.battery_runtime._async_accept_charge_from_grid_disclaimer()  # noqa: SLF001

    coord.client.accept_battery_settings_disclaimer = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=404,
            message="missing",
        )
    )

    await coord.battery_runtime._async_accept_charge_from_grid_disclaimer()  # noqa: SLF001


@pytest.mark.asyncio
async def test_accept_charge_from_grid_disclaimer_reraises_other_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.accept_battery_settings_disclaimer = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime._async_accept_charge_from_grid_disclaimer()  # noqa: SLF001


def test_cfg_schedule_commit_payload_uses_state_fallback_and_enable_intent(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = None  # noqa: SLF001
    coord.battery_state._battery_charge_from_grid = False  # noqa: SLF001

    disabled_payload = (
        coord.battery_runtime._cfg_schedule_commit_payload()
    )  # noqa: SLF001
    assert disabled_payload == {"chargeFromGrid": False}

    enabled_payload = (
        coord.battery_runtime._cfg_schedule_commit_payload(  # noqa: SLF001
            schedule_enabled=True
        )
    )
    assert enabled_payload["chargeFromGrid"] is True
    assert isinstance(enabled_payload["acceptedItcDisclaimer"], str)


@pytest.mark.asyncio
async def test_cfg_schedule_commit_write_reraises_non_forbidden_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    err = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=409,
        message="conflict",
    )
    coord.client.validate_battery_schedule = AsyncMock(return_value={"isValid": True})
    coord.client.set_battery_settings = AsyncMock(side_effect=err)
    coord.battery_runtime.raise_schedule_update_validation_error = (
        MagicMock()
    )  # noqa: SLF001

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime.async_commit_cfg_schedule_write()

    coord.battery_runtime.raise_schedule_update_validation_error.assert_called_once_with(  # noqa: SLF001
        err
    )


@pytest.mark.asyncio
async def test_cfg_schedule_commit_translates_compat_fallback_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    primary_err = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=403,
        message="forbidden",
    )
    compat_err = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=409,
        message="conflict",
    )
    coord.client.validate_battery_schedule = AsyncMock(return_value={"isValid": True})
    coord.client.set_battery_settings = AsyncMock(side_effect=primary_err)
    coord.client.set_battery_settings_compat = AsyncMock(side_effect=compat_err)
    coord.battery_runtime.raise_schedule_update_validation_error = (
        MagicMock()
    )  # noqa: SLF001

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime.async_commit_cfg_schedule_write()

    coord.battery_runtime.raise_schedule_update_validation_error.assert_called_once_with(  # noqa: SLF001
        compat_err
    )


@pytest.mark.asyncio
async def test_cfg_schedule_commit_rejects_valid_false_validation_response(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.validate_battery_schedule = AsyncMock(
        return_value={"valid": False, "message": "bad schedule"}
    )
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})

    with pytest.raises(ServiceValidationError, match="bad schedule"):
        await coord.battery_runtime.async_commit_cfg_schedule_write()

    coord.client.set_battery_settings.assert_not_awaited()


def test_schedule_family_effective_enabled_uses_cfg_control_fallback(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._battery_schedule_effective_enabled = (
        lambda schedule_type: None
    )  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = None  # noqa: SLF001
    coord.battery_state._battery_charge_from_grid_schedule_enabled = (
        None  # noqa: SLF001
    )
    monkeypatch.setattr(
        type(coord),
        "battery_cfg_control_force_schedule_opted",
        property(lambda self: True),
        raising=False,
    )

    assert (
        coord.battery_runtime._schedule_family_effective_enabled("cfg", None) is True
    )  # noqa: SLF001


def test_parse_battery_settings_payload_keeps_shutdown_level_when_values_present(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "batteryGridMode": "ImportExport",
                "hideChargeFromGrid": False,
                "envoySupportsVls": True,
                "chargeFromGrid": True,
                "veryLowSoc": 15,
                "veryLowSocMin": 10,
                "veryLowSocMax": 25,
                "cfgControl": {"show": True, "enabled": True, "locked": False},
                "systemTask": False,
            }
        }
    )
    runtime.parse_battery_site_settings_payload(
        {
            "data": {
                "batteryLimitSupport": False,
            }
        }
    )

    assert coord.battery_shutdown_level == 15
    assert coord.battery_shutdown_level_available is True


def test_cfg_settings_pending_helpers_overlay_and_clear(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    runtime.set_cfg_settings_pending_from_payload(
        {
            "chargeFromGrid": True,
            "chargeFromGridScheduleEnabled": False,
            "chargeBeginTime": 120,
            "chargeEndTime": 300,
        }
    )

    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord._battery_charge_begin_time = None  # noqa: SLF001
    coord._battery_charge_end_time = None  # noqa: SLF001

    runtime.sync_cfg_settings_pending()

    assert coord.battery_charge_from_grid_enabled is True
    assert coord.battery_charge_from_grid_schedule_enabled is False
    assert coord._battery_charge_begin_time == 120  # noqa: SLF001
    assert coord._battery_charge_end_time == 300  # noqa: SLF001
    assert (
        coord.battery_state._battery_cfg_pending_expires_mono == 160.0
    )  # noqa: SLF001

    runtime.sync_cfg_settings_pending()

    assert (
        coord.battery_state._battery_cfg_pending_charge_from_grid is None
    )  # noqa: SLF001
    assert (
        coord.battery_state._battery_cfg_pending_schedule_enabled is None
    )  # noqa: SLF001
    assert coord.battery_state._battery_cfg_pending_begin_time is None  # noqa: SLF001
    assert coord.battery_state._battery_cfg_pending_end_time is None  # noqa: SLF001
    assert coord.battery_state._battery_cfg_pending_expires_mono is None  # noqa: SLF001


def test_cfg_settings_pending_helpers_clear_on_empty_payload_and_expiry(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    monkeypatch.setattr(time, "monotonic", lambda: 50.0)
    runtime.set_cfg_settings_pending_from_payload({"chargeFromGrid": True})
    assert (
        coord.battery_state._battery_cfg_pending_charge_from_grid is True
    )  # noqa: SLF001

    runtime.set_cfg_settings_pending_from_payload({})
    assert (
        coord.battery_state._battery_cfg_pending_charge_from_grid is None
    )  # noqa: SLF001

    runtime.set_cfg_settings_pending_from_payload({"chargeFromGrid": False})
    monkeypatch.setattr(time, "monotonic", lambda: 200.0)
    runtime.sync_cfg_settings_pending()
    assert coord.battery_state._battery_cfg_pending_expires_mono is None  # noqa: SLF001


def test_cfg_settings_pending_helpers_ignore_non_dict_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    runtime.set_cfg_settings_pending_from_payload(None)  # type: ignore[arg-type]

    assert (
        coord.battery_state._battery_cfg_pending_charge_from_grid is None
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_schedule_toggle_and_time_updates_validate_and_allow_overnight(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    fixed_now = datetime(2026, 2, 8, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: fixed_now)

    await coord.battery_runtime.async_set_charge_from_grid_schedule_enabled(True)
    payload = coord.client.set_battery_settings.await_args.args[0]
    assert payload["chargeFromGrid"] is True
    assert payload["chargeFromGridScheduleEnabled"] is True

    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord.client.set_battery_settings.reset_mock()
    await coord.battery_runtime.async_set_charge_from_grid_schedule_enabled(False)
    args = coord.client.set_battery_settings.await_args.args
    payload = args[0]
    assert payload["chargeFromGridScheduleEnabled"] is False
    assert payload["chargeBeginTime"] == 120
    assert payload["chargeEndTime"] == 300

    coord._battery_settings_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord.client.set_battery_settings.reset_mock()
    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(2, 0)
    )
    args = coord.client.set_battery_settings.await_args.args
    payload = args[0]
    assert payload["chargeBeginTime"] == 1380
    assert payload["chargeEndTime"] == 120

    coord.client.set_battery_settings.reset_mock()
    coord._battery_settings_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(2, 0), end=dt_time(2, 0)
        )


@pytest.mark.asyncio
async def test_set_battery_shutdown_level_validation_and_write_guard(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001
    coord._battery_very_low_soc_max = 25  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord.battery_runtime.async_set_battery_shutdown_level(20)
    args = coord.client.set_battery_settings.await_args.args
    assert args[0] == {"veryLowSoc": 20}

    with pytest.raises(ServiceValidationError, match="between 10 and 25"):
        await coord.battery_runtime.async_set_battery_shutdown_level(9)

    coord._battery_envoy_supports_vls = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.battery_runtime.async_set_battery_shutdown_level(12)


@pytest.mark.asyncio
async def test_battery_settings_write_lock_and_debounce(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    await coord._battery_settings_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord.battery_runtime.async_apply_battery_settings(
                {"chargeFromGrid": True}
            )
    finally:
        coord._battery_settings_write_lock.release()  # noqa: SLF001

    coord._battery_settings_last_write_mono = time.monotonic()  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="too quickly"):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )


def test_battery_helper_edge_cases_cover_fallback_paths(coordinator_factory) -> None:
    coord = coordinator_factory()

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    class BadTime:
        @property
        def hour(self):
            raise ValueError("boom")

    assert coord._normalize_battery_grid_mode(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_battery_grid_mode("   ") is None  # noqa: SLF001
    assert coord._battery_grid_mode_key(BadStr()) is None  # noqa: SLF001
    assert coord._battery_grid_mode_label(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_minutes_of_day(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_minutes_of_day(1440) is None  # noqa: SLF001
    assert coord._minutes_of_day_to_time(None) is None  # noqa: SLF001
    assert coord._minutes_of_day_to_time(2000) is None  # noqa: SLF001
    assert coord._time_to_minutes_of_day(None) is None  # noqa: SLF001
    assert coord._time_to_minutes_of_day(BadTime()) is None  # noqa: SLF001


def test_charge_from_grid_control_unavailable_when_no_battery(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = False  # noqa: SLF001
    assert coord.charge_from_grid_control_available is False


def test_charge_from_grid_control_unavailable_for_read_only_user(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    assert coord.charge_from_grid_control_available is False


def test_charge_from_grid_control_uses_cfg_control_when_present(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_cfg_control_show = True  # noqa: SLF001
    coord._battery_cfg_control_enabled = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = True  # noqa: SLF001

    assert coord.charge_from_grid_control_available is True


def test_charge_from_grid_control_honors_cfg_control_false(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_cfg_control_show = True  # noqa: SLF001
    coord._battery_cfg_control_enabled = False  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001

    assert coord.charge_from_grid_control_available is True


def test_charge_from_grid_control_honors_cfg_control_show_false(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_cfg_control_show = False  # noqa: SLF001
    coord._battery_cfg_control_enabled = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001

    assert coord.charge_from_grid_control_available is False


def test_charge_from_grid_control_honors_cfg_control_locked(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=False,
        locked=True,
    )

    assert coord.charge_from_grid_control_available is False


def test_charge_from_grid_schedule_supported_uses_capability_flags(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=False,
        locked=False,
        show_day_schedule=False,
        schedule_supported=True,
        force_schedule_supported=True,
    )
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    assert coord.charge_from_grid_schedule_supported is False

    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=False,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
        force_schedule_supported=False,
    )
    assert coord.charge_from_grid_schedule_supported is True
    assert coord.charge_from_grid_force_schedule_supported is False

    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=False,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
        force_schedule_supported=True,
    )
    assert coord.charge_from_grid_schedule_supported is True
    assert coord.charge_from_grid_force_schedule_supported is True


def test_charge_from_grid_schedule_supported_uses_capability_without_times(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=False,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
        force_schedule_supported=False,
    )
    coord._battery_charge_begin_time = None  # noqa: SLF001
    coord._battery_charge_end_time = None  # noqa: SLF001

    assert coord.charge_from_grid_schedule_supported is True
    assert coord.charge_from_grid_force_schedule_supported is False


def test_charge_from_grid_schedule_availability_does_not_require_enabled_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=False,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
        force_schedule_supported=True,
    )
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    assert coord.charge_from_grid_schedule_available is True
    assert coord.charge_from_grid_force_schedule_available is True


def test_charge_from_grid_control_falls_back_to_legacy_hide_when_cfg_control_absent(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_cfg_control_show = None  # noqa: SLF001
    coord._battery_cfg_control_enabled = None  # noqa: SLF001
    coord._battery_hide_charge_from_grid = True  # noqa: SLF001

    assert coord.charge_from_grid_control_available is False


def test_charge_from_grid_control_enabled_state_does_not_hide_writable_control(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=False,
        locked=False,
    )

    assert coord.charge_from_grid_control_available is True


def test_charge_from_grid_control_available_without_live_toggle_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = None  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=False,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
        force_schedule_supported=True,
    )
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    assert coord.charge_from_grid_control_available is True


@pytest.mark.parametrize(
    ("attr_name", "attr_value"),
    [
        ("_battery_charge_from_grid_schedule_enabled", False),
        ("_battery_cfg_schedule_limit", 85),
        ("_battery_cfg_schedule_id", "cfg-schedule"),
    ],
)
def test_charge_from_grid_control_available_with_schedule_evidence(
    coordinator_factory,
    attr_name: str,
    attr_value: object,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = None  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=False,
        locked=False,
    )
    coord._battery_charge_begin_time = None  # noqa: SLF001
    coord._battery_charge_end_time = None  # noqa: SLF001
    setattr(coord, attr_name, attr_value)

    assert coord.charge_from_grid_control_available is True


def test_battery_settings_write_age_seconds_handles_monotonic_errors(
    coordinator_factory,
) -> None:
    class BadFloat(float):
        def __float__(self) -> float:
            raise RuntimeError("boom")

    coord = coordinator_factory()
    coord._battery_settings_last_write_mono = BadFloat(1.0)  # noqa: SLF001

    assert coord.battery_settings_write_age_seconds is None
    assert coord.battery_settings_write_pending is False


def test_battery_control_capability_helpers_cover_none_inputs(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_cfg_control_show = True  # noqa: SLF001
    coord._battery_cfg_control_enabled = True  # noqa: SLF001
    coord._battery_cfg_control_schedule_supported = True  # noqa: SLF001
    coord._battery_cfg_control_force_schedule_supported = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001

    assert runtime._parse_battery_control_capability(None) is None  # noqa: SLF001
    assert (
        runtime._apply_battery_control_state("_battery_cfg_control", None) is None
    )  # noqa: SLF001
    assert coord._battery_cfg_control_show is None  # noqa: SLF001
    assert coord._battery_cfg_control_enabled is None  # noqa: SLF001
    assert coord._battery_cfg_control_schedule_supported is None  # noqa: SLF001
    assert coord._battery_cfg_control_force_schedule_supported is None  # noqa: SLF001

    runtime._apply_battery_permission_payload(["bad"])  # noqa: SLF001
    assert coord.battery_user_is_owner is True


def test_parse_battery_settings_payload_handles_non_dict_and_bad_disclaimer(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_accepted_itc_disclaimer = "2026-02-08T10:00:00+00:00"  # noqa: SLF001

    coord.battery_runtime.parse_battery_settings_payload(["bad"])

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"acceptedItcDisclaimer": BadStr()}}
    )
    assert coord._battery_accepted_itc_disclaimer is None  # noqa: SLF001

    coord._battery_accepted_itc_disclaimer = "2026-02-08T10:00:00+00:00"  # noqa: SLF001
    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"acceptedItcDisclaimer": True}}
    )
    assert (
        coord._battery_accepted_itc_disclaimer == "2026-02-08T10:00:00+00:00"
    )  # noqa: SLF001


def test_parse_battery_settings_payload_clears_missing_reserve_bounds(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_backup_percentage_min = 8  # noqa: SLF001
    coord._battery_backup_percentage_max = 95  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001

    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"batteryBackupPercentage": 20}}
    )

    assert coord.battery_reserve_min == 10
    assert coord.battery_reserve_max == 100


def test_parse_battery_settings_payload_keeps_reserve_bounds_for_partial_update(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_backup_percentage_min = 8  # noqa: SLF001
    coord._battery_backup_percentage_max = 95  # noqa: SLF001

    coord.battery_runtime.parse_battery_settings_payload(
        {"chargeFromGrid": True},
        clear_missing_reserve_bounds=False,
    )

    assert coord.battery_reserve_min == 8
    assert coord.battery_reserve_max == 95


def test_parse_battery_settings_payload_updates_schedule_enabled_from_control_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_dtg_schedule_enabled = False  # noqa: SLF001
    coord._battery_rbd_schedule_enabled = True  # noqa: SLF001

    coord.battery_runtime.parse_battery_settings_payload(
        {
            "data": {
                "dtgControl": {"enabled": True},
                "rbdControl": {"enabled": False},
            }
        },
        clear_missing_schedule_times=False,
        clear_missing_reserve_bounds=False,
    )

    assert coord._battery_dtg_schedule_enabled is True  # noqa: SLF001
    assert coord._battery_rbd_schedule_enabled is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_settings_handles_non_dict_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_settings_details = AsyncMock(return_value=["unexpected"])
    await coord.battery_runtime.async_refresh_battery_settings(force=True)
    assert coord._battery_settings_payload == {"value": ["unexpected"]}  # noqa: SLF001


@pytest.mark.asyncio
async def test_apply_battery_settings_rejects_empty_payload(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    with pytest.raises(ServiceValidationError, match="payload is unavailable"):
        await coord.battery_runtime.async_apply_battery_settings({})


@pytest.mark.asyncio
async def test_apply_battery_settings_partial_payload_keeps_schedule_times(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord.battery_runtime.async_apply_battery_settings({"chargeFromGrid": False})

    assert coord._battery_charge_begin_time == 120  # noqa: SLF001
    assert coord._battery_charge_end_time == 300  # noqa: SLF001
    assert coord.charge_from_grid_schedule_supported is True


@pytest.mark.asyncio
async def test_battery_settings_forbidden_translates_to_validation_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )


@pytest.mark.asyncio
async def test_battery_settings_forbidden_read_only_user_translates_to_permission_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )
    coord.client.set_battery_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_settings_write_blocked_when_system_task_active(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_system_task = True  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock()

    with pytest.raises(
        ServiceValidationError, match="Battery settings updates are unavailable"
    ):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )
    coord.client.set_battery_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_settings_write_blocked_when_refresh_discovers_system_task(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(
        return_value={
            "data": {
                "userDetails": {"isOwner": True, "isInstaller": False},
                "systemTask": True,
            }
        }
    )
    coord.client.set_battery_settings = AsyncMock()

    with pytest.raises(
        ServiceValidationError, match="Battery settings updates are unavailable"
    ):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )
    coord.client.set_battery_settings.assert_not_awaited()


def test_battery_settings_feature_writable_rejects_read_only_user(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001

    with pytest.raises(
        ServiceValidationError,
        match="Battery settings updates are not permitted for this account",
    ):
        coord.battery_runtime._assert_battery_settings_feature_writable(  # noqa: SLF001
            "Charge from grid setting is unavailable."
        )


@pytest.mark.asyncio
async def test_set_battery_reserve_rejects_when_reserve_not_editable(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        locked=True,
    )

    with pytest.raises(ServiceValidationError, match="Battery reserve is unavailable"):
        await coord.async_set_battery_reserve(25)


@pytest.mark.asyncio
async def test_set_savings_use_battery_after_peak_rejects_when_switch_unavailable(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_show_savings_mode = False  # noqa: SLF001

    with pytest.raises(
        ServiceValidationError,
        match="Savings profile settings are unavailable",
    ):
        await coord.async_set_savings_use_battery_after_peak(True)


@pytest.mark.asyncio
async def test_set_charge_from_grid_schedule_enabled_rejects_without_force_support(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
        force_schedule_supported=False,
    )

    with pytest.raises(
        ServiceValidationError,
        match="Charge from grid schedule is unavailable",
    ):
        await coord.async_set_charge_from_grid_schedule_enabled(True)


@pytest.mark.asyncio
async def test_battery_settings_unknown_role_rejects_when_refresh_unresolved(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(
        return_value={"data": {"userDetails": {}}}
    )
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})

    with pytest.raises(ServiceValidationError, match="could not be confirmed"):
        await coord.async_set_charge_from_grid(False)

    coord.client.battery_site_settings.assert_awaited_once()
    coord.client.set_battery_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_write_access_refresh_failure_rejects_when_unconfirmed(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(ServiceValidationError, match="could not be confirmed"):
        await coord.battery_runtime.async_ensure_battery_write_access_confirmed()


@pytest.mark.asyncio
async def test_battery_write_access_refresh_uses_payload_fallback_and_custom_denial(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord.redact_battery_payload = MagicMock(return_value="redacted")  # type: ignore[method-assign]
    coord.client.battery_site_settings = AsyncMock(
        return_value={"userDetails": {"isOwner": False, "isInstaller": False}}
    )

    with pytest.raises(ServiceValidationError, match="custom denied"):
        await coord.battery_runtime.async_ensure_battery_write_access_confirmed(
            denied_message="custom denied"
        )

    assert coord.battery_state._battery_site_settings_payload == {
        "value": "redacted"
    }  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_settings_unauthorized_translates_to_reauth_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )


@pytest.mark.asyncio
async def test_battery_settings_unexpected_http_error_reraises(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime.async_apply_battery_settings(
            {"chargeFromGrid": False}
        )


@pytest.mark.asyncio
async def test_battery_settings_service_validation_paths(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()

    coord._battery_has_encharge = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="setting is unavailable"):
        await coord.battery_runtime.async_set_charge_from_grid(True)
    with pytest.raises(ServiceValidationError, match="schedule is unavailable"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_enabled(True)
    with pytest.raises(ServiceValidationError, match="schedule is unavailable"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(1, 0)
        )

    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 120  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_enabled(True)

    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="enabled first"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(1, 0)
        )

    coord._battery_charge_from_grid = True  # noqa: SLF001

    class BadTime:
        @property
        def hour(self):
            raise ValueError("boom")

    with pytest.raises(ServiceValidationError, match="time is invalid"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=BadTime()
        )


@pytest.mark.asyncio
async def test_battery_shutdown_level_invalid_type_raises_validation(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    class BadInt:
        def __int__(self):
            raise ValueError("boom")

    coord = coordinator_factory()
    coord._battery_envoy_supports_vls = True  # noqa: SLF001
    coord._battery_very_low_soc = 15  # noqa: SLF001
    coord._battery_very_low_soc_min = 10  # noqa: SLF001
    coord._battery_very_low_soc_max = 25  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="level is invalid"):
        await coord.battery_runtime.async_set_battery_shutdown_level(BadInt())


def test_parse_battery_schedules_payload_uses_entry_timezone_and_clears_stale_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_cfg_schedule_limit = 55  # noqa: SLF001
    coord._battery_cfg_schedule_id = "stale"  # noqa: SLF001
    coord._battery_cfg_schedule_days = [1, 2]  # noqa: SLF001
    coord._battery_cfg_schedule_timezone = "UTC"  # noqa: SLF001

    runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "details": [
                    {
                        "scheduleId": "sched-2",
                        "startTime": "00:00",
                        "endTime": "07:30",
                        "limit": 90,
                        "days": [1, 7],
                        "timezone": "Europe/Lisbon",
                        "isEnabled": True,
                    }
                ]
            }
        }
    )

    assert coord._battery_charge_begin_time == 0  # noqa: SLF001
    assert coord._battery_charge_end_time == 450  # noqa: SLF001
    assert coord.battery_cfg_schedule_limit == 90
    assert coord._battery_cfg_schedule_id == "sched-2"  # noqa: SLF001
    assert coord._battery_cfg_schedule_days == [1, 7]  # noqa: SLF001
    assert coord._battery_cfg_schedule_timezone == "Europe/Lisbon"  # noqa: SLF001

    runtime.parse_battery_schedules_payload({"cfg": {"details": []}})

    assert coord.battery_cfg_schedule_limit is None
    assert coord._battery_cfg_schedule_id is None  # noqa: SLF001
    assert coord._battery_cfg_schedule_days is None  # noqa: SLF001
    assert coord._battery_cfg_schedule_timezone is None  # noqa: SLF001
    assert coord._battery_charge_begin_time == 0  # noqa: SLF001
    assert coord._battery_charge_end_time == 450  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_schedules_handles_non_dict_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_schedules = AsyncMock(return_value="bad")

    await coord.battery_runtime.async_refresh_battery_schedules()

    assert coord._battery_schedules_payload is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_refresh_battery_schedules_stores_non_dict_redacted_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_schedules = AsyncMock(
        return_value={
            "cfg": {
                "details": [
                    {
                        "scheduleId": "sched-2",
                        "startTime": "01:00",
                        "endTime": "05:00",
                        "limit": 85,
                        "days": [1, 2, 3],
                        "timezone": "Europe/Lisbon",
                        "isEnabled": True,
                    }
                ]
            }
        }
    )
    coord._redact_battery_payload = MagicMock(return_value="masked")  # noqa: SLF001

    await coord.battery_runtime.async_refresh_battery_schedules()

    assert coord._battery_schedules_payload == {"value": "masked"}  # noqa: SLF001
    assert coord.battery_cfg_schedule_limit == 85


@pytest.mark.asyncio
async def test_refresh_battery_schedules_stores_dict_redacted_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_schedules = AsyncMock(
        return_value={
            "cfg": {
                "details": [
                    {
                        "scheduleId": "sched-2",
                        "startTime": "01:00",
                        "endTime": "05:00",
                        "limit": 85,
                        "days": [1, 2, 3],
                        "timezone": "Europe/Lisbon",
                        "isEnabled": True,
                    }
                ]
            }
        }
    )
    coord._redact_battery_payload = MagicMock(  # noqa: SLF001
        return_value={"value": "masked"}
    )

    await coord.battery_runtime.async_refresh_battery_schedules()

    assert coord._battery_schedules_payload == {"value": "masked"}  # noqa: SLF001
    assert coord.battery_cfg_schedule_limit == 85


@pytest.mark.asyncio
async def test_refresh_battery_schedules_uses_zero_success_ttl_while_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._endpoint_family_should_run = lambda *args, **kwargs: True  # noqa: SLF001
    coord._note_endpoint_family_success = MagicMock()  # noqa: SLF001
    coord._note_endpoint_family_failure = lambda *args, **kwargs: None  # noqa: SLF001
    coord._battery_dtg_schedule_status = "pending"  # noqa: SLF001
    coord.client.battery_schedules = AsyncMock(
        return_value={
            "dtg": {
                "details": [
                    {
                        "scheduleId": "sched-dtg",
                        "startTime": "00:00",
                        "endTime": "23:59",
                        "limit": 22,
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "timezone": "Australia/Melbourne",
                        "isEnabled": False,
                        "scheduleStatus": "pending",
                    }
                ]
            }
        }
    )

    await coord.battery_runtime.async_refresh_battery_schedules()

    coord._note_endpoint_family_success.assert_called_once_with(  # noqa: SLF001
        "battery_schedules",
        success_ttl_s=0.0,
    )


def test_parse_battery_schedules_payload_handles_invalid_shapes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_schedules_payload(None)
    assert coord.battery_cfg_schedule_limit is None

    coord.battery_runtime.parse_battery_schedules_payload({"cfg": None})
    assert coord.battery_cfg_schedule_limit is None

    coord.battery_runtime.parse_battery_schedules_payload({"cfg": {"details": [None]}})
    assert coord.battery_cfg_schedule_limit is None


def test_parse_battery_schedules_payload_handles_invalid_times_and_top_level_timezone(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "timezone": "America/Los_Angeles",
            "cfg": {
                "details": [
                    {
                        "scheduleId": "sched-3",
                        "startTime": "aa:bb",
                        "endTime": "11:xx",
                        "limit": 75,
                        "days": [1],
                    }
                ]
            },
        }
    )

    assert coord._battery_charge_begin_time is None  # noqa: SLF001
    assert coord._battery_charge_end_time is None  # noqa: SLF001
    assert coord._battery_cfg_schedule_timezone == "America/Los_Angeles"  # noqa: SLF001


def test_parse_battery_schedules_payload_tracks_dtg_and_rbd_families(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "dtg": {
                "scheduleStatus": "Active",
                "details": [
                    {
                        "scheduleId": "sched-dtg",
                        "startTime": "18:00",
                        "endTime": "23:59",
                        "limit": 5,
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "timezone": "Europe/London",
                        "scheduleStatus": "active",
                        "isEnabled": True,
                    }
                ],
            },
            "rbd": {
                "scheduleStatus": "Active",
                "details": [
                    {
                        "scheduleId": "sched-rbd",
                        "startTime": "01:00",
                        "endTime": "16:00",
                        "limit": 100,
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "timezone": "Europe/London",
                        "scheduleStatus": "active",
                        "isEnabled": True,
                    }
                ],
            },
        }
    )

    assert coord._battery_dtg_begin_time == 1080  # noqa: SLF001
    assert coord._battery_dtg_end_time == 1439  # noqa: SLF001
    assert coord._battery_dtg_schedule_id == "sched-dtg"  # noqa: SLF001
    assert coord._battery_dtg_schedule_limit == 5  # noqa: SLF001
    assert coord._battery_dtg_schedule_days == [1, 2, 3, 4, 5, 6, 7]  # noqa: SLF001
    assert coord._battery_dtg_schedule_timezone == "Europe/London"  # noqa: SLF001
    assert coord.battery_discharge_to_grid_schedule_enabled is True
    assert coord.battery_dtg_schedule_status == "active"
    assert coord._battery_rbd_begin_time == 60  # noqa: SLF001
    assert coord._battery_rbd_end_time == 960  # noqa: SLF001
    assert coord._battery_rbd_schedule_id == "sched-rbd"  # noqa: SLF001
    assert coord._battery_rbd_schedule_limit == 100  # noqa: SLF001
    assert coord._battery_rbd_schedule_days == [1, 2, 3, 4, 5, 6, 7]  # noqa: SLF001
    assert coord._battery_rbd_schedule_timezone == "Europe/London"  # noqa: SLF001
    assert coord.battery_restrict_battery_discharge_schedule_enabled is True
    assert coord.battery_rbd_schedule_status == "active"


def test_parse_battery_schedules_payload_preserves_enabled_state_from_controls(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_settings_payload(
        {
            "data": {
                "chargeFromGridScheduleEnabled": False,
                "dtgControl": {"enabled": False},
                "rbdControl": {"enabled": True},
            }
        },
        clear_missing_schedule_times=False,
        clear_missing_reserve_bounds=False,
    )
    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "details": [
                    {
                        "scheduleId": "sched-cfg",
                        "startTime": "00:00",
                        "endTime": "07:30",
                        "limit": 90,
                        "days": [1, 7],
                        "timezone": "Europe/Lisbon",
                        "isEnabled": True,
                    }
                ]
            },
            "dtg": {
                "details": [
                    {
                        "scheduleId": "sched-dtg",
                        "startTime": "18:00",
                        "endTime": "23:59",
                        "limit": 5,
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "timezone": "Europe/London",
                        "isEnabled": True,
                    }
                ]
            },
            "rbd": {
                "details": [
                    {
                        "scheduleId": "sched-rbd",
                        "startTime": "01:00",
                        "endTime": "16:00",
                        "limit": 100,
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "timezone": "Europe/London",
                        "isEnabled": False,
                    }
                ]
            },
        }
    )

    assert coord.battery_charge_from_grid_schedule_enabled is False
    assert coord.battery_discharge_to_grid_schedule_enabled is False
    assert coord.battery_restrict_battery_discharge_schedule_enabled is True


def test_parse_battery_schedules_payload_prefers_control_matching_detail(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_settings_payload(
        {
            "data": {
                "chargeFromGridScheduleEnabled": False,
                "dtgControl": {"enabled": False},
                "rbdControl": {"enabled": True},
            }
        },
        clear_missing_schedule_times=False,
        clear_missing_reserve_bounds=False,
    )
    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "details": [
                    {
                        "scheduleId": "cfg-temp",
                        "startTime": "00:10",
                        "endTime": "00:40",
                        "limit": 100,
                        "days": [6],
                        "timezone": "Australia/Melbourne",
                        "isEnabled": True,
                    },
                    {
                        "scheduleId": "cfg-real",
                        "startTime": "02:00",
                        "endTime": "05:00",
                        "limit": 100,
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "timezone": "Australia/Melbourne",
                        "isEnabled": False,
                    },
                ]
            },
            "dtg": {
                "details": [
                    {
                        "scheduleId": "dtg-temp",
                        "startTime": "00:15",
                        "endTime": "00:45",
                        "limit": 21,
                        "days": [6],
                        "timezone": "Australia/Melbourne",
                        "isEnabled": True,
                    },
                    {
                        "scheduleId": "dtg-real",
                        "startTime": "18:15",
                        "endTime": "23:45",
                        "limit": 21,
                        "days": [1, 2, 3, 4, 5],
                        "timezone": "Australia/Melbourne",
                        "isEnabled": False,
                    },
                ]
            },
            "rbd": {
                "details": [
                    {
                        "scheduleId": "rbd-temp",
                        "startTime": "00:50",
                        "endTime": "01:20",
                        "limit": 100,
                        "days": [6],
                        "timezone": "Australia/Melbourne",
                        "isEnabled": False,
                    },
                    {
                        "scheduleId": "rbd-real",
                        "startTime": "06:30",
                        "endTime": "11:00",
                        "limit": 100,
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "timezone": "Australia/Melbourne",
                        "isEnabled": True,
                    },
                ]
            },
        }
    )

    assert coord._battery_cfg_schedule_id == "cfg-real"  # noqa: SLF001
    assert coord._battery_charge_begin_time == 120  # noqa: SLF001
    assert coord._battery_charge_end_time == 300  # noqa: SLF001
    assert coord._battery_dtg_schedule_id == "dtg-real"  # noqa: SLF001
    assert coord._battery_dtg_begin_time == 1095  # noqa: SLF001
    assert coord._battery_dtg_end_time == 1425  # noqa: SLF001
    assert coord._battery_rbd_schedule_id == "rbd-real"  # noqa: SLF001
    assert coord._battery_rbd_begin_time == 390  # noqa: SLF001
    assert coord._battery_rbd_end_time == 660  # noqa: SLF001


# ---------------------------------------------------------------------------
# CFG schedule CRUD – lock/debounce, restore-on-failure, availability guards
# ---------------------------------------------------------------------------


def _seed_cfg_schedule(coord):
    """Populate coordinator with a valid parsed CFG schedule."""
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_cfg_schedule_id = "sched-1"  # noqa: SLF001
    coord._battery_cfg_schedule_limit = 80  # noqa: SLF001
    coord._battery_cfg_schedule_days = [1, 2, 3, 4, 5, 6, 7]  # noqa: SLF001
    coord._battery_cfg_schedule_timezone = "Europe/Lisbon"  # noqa: SLF001
    coord._battery_schedules_payload = {"cfg": {"details": [{}]}}  # noqa: SLF001
    coord.client.delete_battery_schedule = AsyncMock(return_value={})
    coord.client.create_battery_schedule = AsyncMock(return_value={})
    coord.client.update_battery_schedule = AsyncMock(return_value={})
    coord.client._bp_xsrf_token = "tok"  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.client.validate_battery_schedule = AsyncMock(return_value={"isValid": True})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()


def _seed_schedule_family(coord, schedule_type: str) -> None:
    coord._battery_has_encharge = True  # noqa: SLF001
    control = coord.battery_runtime._parse_battery_control_capability(  # noqa: SLF001
        {
            "show": True,
            "showDaySchedule": True,
            "scheduleSupported": True,
            "enabled": True,
        }
    )
    if schedule_type == "dtg":
        coord._battery_dtg_control = control  # noqa: SLF001
        coord._battery_dtg_begin_time = 1080  # noqa: SLF001
        coord._battery_dtg_end_time = 1380  # noqa: SLF001
        coord._battery_dtg_schedule_id = "sched-dtg"  # noqa: SLF001
        coord._battery_dtg_schedule_limit = 5  # noqa: SLF001
        coord._battery_dtg_schedule_days = [1, 2, 3, 4, 5, 6, 7]  # noqa: SLF001
        coord._battery_dtg_schedule_timezone = "Europe/London"  # noqa: SLF001
        coord._battery_dtg_schedule_enabled = True  # noqa: SLF001
    else:
        coord._battery_rbd_control = control  # noqa: SLF001
        coord._battery_rbd_begin_time = 60  # noqa: SLF001
        coord._battery_rbd_end_time = 960  # noqa: SLF001
        coord._battery_rbd_schedule_id = "sched-rbd"  # noqa: SLF001
        coord._battery_rbd_schedule_limit = 100  # noqa: SLF001
        coord._battery_rbd_schedule_days = [1, 2, 3, 4, 5, 6, 7]  # noqa: SLF001
        coord._battery_rbd_schedule_timezone = "Europe/London"  # noqa: SLF001
        coord._battery_rbd_schedule_enabled = True  # noqa: SLF001
    coord.client.update_battery_schedule = AsyncMock(return_value={})
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()


def _seed_no_schedule_family(coord, schedule_type: str) -> None:
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    control_payload = {
        "show": True,
        "showDaySchedule": True,
        "scheduleSupported": True,
        "enabled": False,
        "locked": False,
    }
    if schedule_type == "dtg":
        control_payload["startTime"] = 1140
        control_payload["endTime"] = 1320
        coord.battery_runtime.parse_battery_settings_payload(
            {
                "data": {
                    "batteryGridMode": "ImportExport",
                    "dtgControl": control_payload,
                }
            }
        )
    else:
        control_payload["startTime"] = 60
        control_payload["endTime"] = 960
        coord.battery_runtime.parse_battery_settings_payload(
            {
                "data": {
                    "batteryGridMode": "ImportExport",
                    "rbdControl": control_payload,
                }
            }
        )
    coord._battery_schedules_payload = {schedule_type: {"details": []}}  # noqa: SLF001
    coord.client.create_battery_schedule = AsyncMock(return_value={})
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_acquires_write_lock(
    coordinator_factory,
) -> None:
    """Schedule time update via /schedules must use the battery write lock."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    # Manually hold the lock → should raise "already in progress".
    await coord._battery_settings_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
                start=dt_time(23, 0), end=dt_time(6, 0)
            )
    finally:
        coord._battery_settings_write_lock.release()  # noqa: SLF001


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_respects_debounce(
    coordinator_factory,
) -> None:
    """Schedule time update via /schedules must respect the debounce window."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    coord._battery_settings_last_write_mono = time.monotonic()  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="too quickly"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_defaults_missing_limit_to_100(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_limit = None  # noqa: SLF001

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(6, 0)
    )

    call = coord.client.update_battery_schedule.await_args
    assert call.kwargs["limit"] == 100


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_uses_in_place_put(
    coordinator_factory,
) -> None:
    """Schedule time update must use in-place PUT, not delete+create."""
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(6, 0)
    )

    coord.client.update_battery_schedule.assert_awaited_once()
    call = coord.client.update_battery_schedule.await_args
    assert call.args[0] == "sched-1"
    assert call.kwargs["start_time"] == "23:00"
    assert call.kwargs["end_time"] == "06:00"
    assert call.kwargs["schedule_type"] == "CFG"
    assert call.kwargs["timezone"] == "Europe/Lisbon"
    # delete+create should NOT be used.
    coord.client.delete_battery_schedule.assert_not_awaited()
    coord.client.create_battery_schedule.assert_not_awaited()
    coord.client.set_battery_settings.assert_awaited_once()
    payload = coord.client.set_battery_settings.await_args.args[0]
    assert payload["chargeFromGrid"] is True
    assert isinstance(payload["acceptedItcDisclaimer"], str)
    coord.client.validate_battery_schedule.assert_awaited_once_with("cfg")


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_forbidden_translates_to_validation_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_unexpected_client_error_reraises(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="Server Error",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )
    assert err.value.status == 500


@pytest.mark.asyncio
async def test_cfg_schedule_limit_rejects_without_existing_schedule(
    coordinator_factory,
) -> None:
    """Setting CFG limit must fail when no parsed schedule exists."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.update_battery_schedule = AsyncMock()

    # No schedule ID → should reject.
    with pytest.raises(ServiceValidationError, match="No existing"):
        await coord.battery_runtime.async_set_cfg_schedule_limit(90)


@pytest.mark.asyncio
async def test_cfg_schedule_limit_rejects_without_schedule_api(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client = MagicMock(spec=())

    with pytest.raises(ServiceValidationError, match="Schedule API not available"):
        await coord.battery_runtime.async_set_cfg_schedule_limit(90)


@pytest.mark.asyncio
async def test_cfg_schedule_limit_rejects_when_force_schedule_explicitly_unsupported(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        locked=False,
        force_schedule_supported=False,
    )
    coord.client.update_battery_schedule = AsyncMock()

    with pytest.raises(
        ServiceValidationError,
        match="Charge from grid schedule is unavailable",
    ):
        await coord.battery_runtime.async_set_cfg_schedule_limit(90)


@pytest.mark.asyncio
async def test_cfg_schedule_limit_acquires_write_lock(
    coordinator_factory,
) -> None:
    """CFG limit update must use the battery write lock."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    await coord._battery_settings_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord.battery_runtime.async_set_cfg_schedule_limit(90)
    finally:
        coord._battery_settings_write_lock.release()  # noqa: SLF001


@pytest.mark.asyncio
async def test_cfg_schedule_limit_uses_in_place_put(
    coordinator_factory,
) -> None:
    """CFG limit update must use in-place PUT, not delete+create."""
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    await coord.battery_runtime.async_set_cfg_schedule_limit(95)

    coord.client.update_battery_schedule.assert_awaited_once()
    call = coord.client.update_battery_schedule.await_args
    assert call.args[0] == "sched-1"
    assert call.kwargs["limit"] == 95
    assert call.kwargs["start_time"] == "02:00"
    assert call.kwargs["end_time"] == "05:00"
    assert call.kwargs["schedule_type"] == "CFG"
    assert call.kwargs["timezone"] == "Europe/Lisbon"
    assert call.kwargs["days"] == [1, 2, 3, 4, 5, 6, 7]
    # delete+create should NOT be used.
    coord.client.delete_battery_schedule.assert_not_awaited()
    coord.client.create_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_cfg_schedule_limit_updates_state_on_success(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    await coord.battery_runtime.async_set_cfg_schedule_limit(95)

    assert coord._battery_cfg_schedule_limit == 95  # noqa: SLF001
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_unauthorized_translates_to_reauth_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )


@pytest.mark.asyncio
async def test_cfg_schedule_time_update_reraises_unexpected_http_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )


async def test_cfg_schedule_limit_unauthorized_translates_to_reauth_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord.battery_runtime.async_set_cfg_schedule_limit(95)


@pytest.mark.asyncio
async def test_cfg_schedule_limit_reraises_unexpected_http_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime.async_set_cfg_schedule_limit(95)


@pytest.mark.asyncio
async def test_atomic_cfg_schedule_update_rejects_limit_below_shutdown_floor(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_very_low_soc = 60  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="at least 60%"):
        await coord.async_update_cfg_schedule(limit=55)

    coord.client.update_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_atomic_cfg_schedule_update_unauthorized_translates_to_reauth_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord.async_update_cfg_schedule(limit=95)


@pytest.mark.asyncio
async def test_atomic_cfg_schedule_update_validation_paths(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    class BadTime:
        @property
        def hour(self):
            raise ValueError("boom")

    coord = coordinator_factory()

    coord.client = MagicMock(spec=())
    with pytest.raises(ServiceValidationError, match="Schedule API not available"):
        await coord.async_update_cfg_schedule(limit=95)

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="pending Envoy sync"):
        await coord.async_update_cfg_schedule(limit=95)

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_id = None  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="No existing"):
        await coord.async_update_cfg_schedule(limit=95)

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._current_charge_from_grid_schedule_window = MagicMock(  # noqa: SLF001
        return_value=(None, None)
    )
    with pytest.raises(
        ServiceValidationError, match="Current schedule times are not available"
    ):
        await coord.async_update_cfg_schedule(limit=95)

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    with pytest.raises(ServiceValidationError, match="time is invalid"):
        await coord.async_update_cfg_schedule(start=BadTime())

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    with pytest.raises(ServiceValidationError, match="must be different"):
        await coord.async_update_cfg_schedule(start=dt_time(2, 0), end=dt_time(2, 0))

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    with pytest.raises(ServiceValidationError, match="between 5 and 100"):
        await coord.async_update_cfg_schedule(limit=4)


@pytest.mark.asyncio
async def test_atomic_cfg_schedule_update_reraises_unexpected_http_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.update_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.async_update_cfg_schedule(limit=95)


@pytest.mark.asyncio
async def test_atomic_cfg_schedule_update_updates_state_on_success(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)

    await coord.async_update_cfg_schedule(
        start=dt_time(23, 0), end=dt_time(6, 0), limit=95
    )

    call = coord.client.update_battery_schedule.await_args
    assert call.args[0] == "sched-1"
    assert call.kwargs["start_time"] == "23:00"
    assert call.kwargs["end_time"] == "06:00"
    assert call.kwargs["limit"] == 95
    assert coord._battery_charge_begin_time == 1380  # noqa: SLF001
    assert coord._battery_charge_end_time == 360  # noqa: SLF001
    assert coord._battery_cfg_schedule_limit == 95  # noqa: SLF001
    assert coord._battery_settings_cache_until is None  # noqa: SLF001
    coord.client.set_battery_settings.assert_awaited_once()
    coord.async_request_refresh.assert_awaited_once()
    coord.kick_fast.assert_called_once()


@pytest.mark.asyncio
async def test_dtg_schedule_enabled_uses_in_place_put(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord.client.set_battery_settings = AsyncMock(return_value={})
    coord.battery_runtime._async_verify_schedule_family_toggle_applied = (
        AsyncMock()
    )  # noqa: SLF001

    await coord.async_set_discharge_to_grid_schedule_enabled(False)

    coord.client.update_battery_schedule.assert_not_awaited()
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": False,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 1080,
                "endTime": 1380,
            }
        },
        schedule_type="dtg",
    )
    assert coord._battery_dtg_schedule_enabled is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_dtg_schedule_enabled_true_uses_richer_battery_settings_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord.client.set_battery_settings = AsyncMock(return_value={})
    coord.battery_runtime._async_verify_schedule_family_toggle_applied = (
        AsyncMock()
    )  # noqa: SLF001

    await coord.async_set_discharge_to_grid_schedule_enabled(True)

    coord.client.update_battery_schedule.assert_not_awaited()
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": True,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 1080,
                "endTime": 1380,
            }
        },
        schedule_type="dtg",
    )
    assert coord._battery_dtg_schedule_enabled is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_dtg_schedule_time_update_omits_enabled_flag(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")

    await coord.async_set_discharge_to_grid_schedule_time(start=dt_time(17, 30))

    call = coord.client.update_battery_schedule.await_args
    assert call.args[0] == "sched-dtg"
    assert call.kwargs["schedule_type"] == "DTG"
    assert call.kwargs["start_time"] == "17:30"
    assert call.kwargs["end_time"] == "23:00"
    assert call.kwargs["is_enabled"] is None
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": True,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 1050,
                "endTime": 1380,
            }
        },
        schedule_type="dtg",
    )
    assert coord._battery_dtg_begin_time == 1050  # noqa: SLF001


@pytest.mark.asyncio
async def test_dtg_schedule_time_update_still_omits_enabled_flag_when_disabled(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord._battery_dtg_schedule_enabled = False  # noqa: SLF001

    await coord.async_set_discharge_to_grid_schedule_time(start=dt_time(17, 30))

    call = coord.client.update_battery_schedule.await_args
    assert call.kwargs["is_enabled"] is None


@pytest.mark.asyncio
async def test_rbd_schedule_limit_update_uses_in_place_put(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "rbd")

    await coord.async_set_restrict_battery_discharge_schedule_limit(80)

    call = coord.client.update_battery_schedule.await_args
    assert call.args[0] == "sched-rbd"
    assert call.kwargs["schedule_type"] == "RBD"
    assert call.kwargs["start_time"] == "01:00"
    assert call.kwargs["end_time"] == "16:00"
    assert call.kwargs["limit"] == 80
    assert call.kwargs["timezone"] == "Europe/London"
    assert call.kwargs["is_enabled"] is None
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "rbdControl": {
                "enabled": True,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 60,
                "endTime": 960,
            }
        },
        schedule_type="rbd",
    )
    assert coord._battery_rbd_schedule_limit == 80  # noqa: SLF001


@pytest.mark.asyncio
async def test_rbd_schedule_update_uses_compat_apply_after_forbidden_primary_write(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "rbd")
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    await coord.battery_runtime.async_update_battery_schedule(
        "sched-rbd",
        schedule_type="RBD",
        start_time="01:15",
        end_time="16:15",
        limit=100,
        days=[1, 2, 3, 4, 5, 6, 7],
        timezone="Europe/London",
    )

    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "rbdControl": {
                "enabled": True,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 75,
                "endTime": 975,
            }
        },
        schedule_type="rbd",
    )
    coord.client.set_battery_settings_compat.assert_awaited_once_with(
        {
            "rbdControl": {
                "enabled": True,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 75,
                "endTime": 975,
            }
        },
        schedule_type="rbd",
        include_source=False,
        merged_payload=True,
        strip_devices=True,
    )


@pytest.mark.asyncio
async def test_cfg_schedule_update_uses_compat_apply_after_forbidden_primary_write(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    await coord.battery_runtime.async_update_battery_schedule(
        "sched-1",
        schedule_type="CFG",
        start_time="23:00",
        end_time="06:00",
        limit=95,
        days=[1, 2, 3, 4, 5, 6, 7],
        timezone="Europe/Lisbon",
    )

    coord.client.set_battery_settings.assert_awaited_once()
    payload = coord.client.set_battery_settings.await_args.args[0]
    assert payload["chargeFromGrid"] is True
    assert isinstance(payload["acceptedItcDisclaimer"], str)
    coord.client.validate_battery_schedule.assert_awaited_once_with("cfg")
    coord.client.set_battery_settings_compat.assert_awaited_once_with(
        payload,
        schedule_type="cfg",
        include_source=False,
        merged_payload=True,
        strip_devices=True,
    )


@pytest.mark.asyncio
async def test_delete_schedule_family_applies_disabled_family_settings(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord.client.delete_battery_schedule = AsyncMock(return_value={})

    await coord.battery_runtime.async_delete_battery_schedule(
        "sched-dtg",
        schedule_type="dtg",
        enabled=False,
    )

    coord.client.delete_battery_schedule.assert_awaited_once_with(
        "sched-dtg",
        schedule_type="dtg",
    )
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": False,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 1080,
                "endTime": 1380,
            }
        },
        schedule_type="dtg",
    )


@pytest.mark.asyncio
async def test_delete_schedule_family_translates_client_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord.client.delete_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="403 Forbidden"):
        await coord.battery_runtime.async_delete_battery_schedule(
            "sched-dtg",
            schedule_type="dtg",
        )


@pytest.mark.asyncio
async def test_delete_schedule_family_reraises_client_error_when_helper_does_not(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    err = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=500,
        message="boom",
    )
    coord.client.delete_battery_schedule = AsyncMock(side_effect=err)
    coord.battery_runtime.raise_schedule_update_validation_error = (
        MagicMock()
    )  # noqa: SLF001

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime.async_delete_battery_schedule(
            "sched-dtg",
            schedule_type="dtg",
        )


def test_schedule_family_settings_payload_cfg_guard_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(
        show=True,
        enabled=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
        force_schedule_supported=True,
    )
    runtime = coord.battery_runtime

    disabled_without_window = runtime._schedule_family_settings_payload(  # noqa: SLF001
        "cfg",
        start_minutes=None,
        end_minutes=120,
        enabled=False,
    )
    assert disabled_without_window["chargeFromGrid"] is True
    assert disabled_without_window["chargeFromGridScheduleEnabled"] is False
    assert "chargeBeginTime" not in disabled_without_window
    assert "chargeEndTime" not in disabled_without_window
    assert disabled_without_window["cfgControl"]["forceScheduleOpted"] is False
    assert (
        runtime._schedule_family_settings_payload(  # noqa: SLF001
            "cfg",
            start_minutes=None,
            end_minutes=120,
            enabled=True,
        )
        is None
    )

    with pytest.raises(ServiceValidationError, match="must be different"):
        runtime._schedule_family_settings_payload(  # noqa: SLF001
            "cfg",
            start_minutes=120,
            end_minutes=120,
            enabled=False,
        )

    payload = runtime._schedule_family_settings_payload(  # noqa: SLF001
        "cfg",
        start_minutes=120,
        end_minutes=180,
        enabled=False,
    )
    assert payload["chargeFromGrid"] is True
    assert payload["cfgControl"] == {
        "show": True,
        "enabled": True,
        "locked": False,
        "showDaySchedule": True,
        "scheduleSupported": True,
        "forceScheduleSupported": True,
        "forceScheduleOpted": False,
    }
    assert (
        runtime._schedule_family_settings_payload(  # noqa: SLF001
            "unknown",
            start_minutes=120,
            end_minutes=180,
            enabled=False,
        )
        is None
    )


@pytest.mark.asyncio
async def test_delete_last_cfg_schedule_disables_schedule_without_window(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_begin_time = None  # noqa: SLF001
    coord._battery_charge_end_time = None  # noqa: SLF001
    coord.battery_state._battery_charge_begin_time = None  # noqa: SLF001
    coord.battery_state._battery_charge_end_time = None  # noqa: SLF001
    coord._battery_accepted_itc_disclaimer = "accepted-at"  # noqa: SLF001
    runtime = coord.battery_runtime
    coord.client.delete_battery_schedule = AsyncMock(return_value={})
    coord.client.set_battery_settings = AsyncMock(return_value={})

    await runtime.async_delete_battery_schedule(
        "sched-cfg",
        schedule_type="cfg",
        enabled=False,
    )

    coord.client.delete_battery_schedule.assert_awaited_once_with(
        "sched-cfg",
        schedule_type="cfg",
    )
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "chargeFromGrid": False,
            "chargeFromGridScheduleEnabled": False,
            "acceptedItcDisclaimer": "accepted-at",
        },
        schedule_type="cfg",
    )


@pytest.mark.asyncio
async def test_async_apply_schedule_family_settings_handles_noop_and_non_forbidden_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord.client.set_battery_settings = AsyncMock(return_value={})

    await runtime.async_apply_schedule_family_settings("unknown")
    coord.client.set_battery_settings.assert_not_awaited()

    runtime._schedule_family_settings_payload = MagicMock(  # noqa: SLF001
        return_value=None
    )
    await runtime.async_apply_schedule_family_settings("cfg", enabled=False)
    coord.client.set_battery_settings.assert_not_awaited()

    coord = coordinator_factory()
    runtime = coord.battery_runtime
    runtime.raise_schedule_update_validation_error = MagicMock()  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )
    with pytest.raises(aiohttp.ClientResponseError):
        await runtime.async_apply_schedule_family_settings(
            "cfg",
            start_time="02:00",
            end_time="05:00",
            enabled=False,
        )
    runtime.raise_schedule_update_validation_error.assert_called_once()  # noqa: SLF001


@pytest.mark.asyncio
async def test_rbd_schedule_enabled_uses_battery_settings(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "rbd")
    coord.client.set_battery_settings = AsyncMock(return_value={})
    coord.battery_runtime._async_verify_schedule_family_toggle_applied = (
        AsyncMock()
    )  # noqa: SLF001

    await coord.async_set_restrict_battery_discharge_schedule_enabled(False)

    coord.client.update_battery_schedule.assert_not_awaited()
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "rbdControl": {
                "enabled": False,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 60,
                "endTime": 960,
            }
        },
        schedule_type="rbd",
    )
    assert coord._battery_rbd_schedule_enabled is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_rbd_schedule_enabled_allows_toggle_while_schedule_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "rbd")
    coord._battery_rbd_schedule_status = "pending"  # noqa: SLF001
    coord.client.set_battery_settings = AsyncMock(return_value={})
    coord.battery_runtime._async_verify_schedule_family_toggle_applied = (
        AsyncMock()
    )  # noqa: SLF001

    await coord.async_set_restrict_battery_discharge_schedule_enabled(False)

    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "rbdControl": {
                "enabled": False,
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 60,
                "endTime": 960,
            }
        },
        schedule_type="rbd",
    )


@pytest.mark.asyncio
async def test_dtg_schedule_enabled_without_schedule_uses_battery_settings_toggle(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_no_schedule_family(coord, "dtg")
    coord.client.set_battery_settings = AsyncMock(return_value={})
    coord.battery_runtime._async_verify_schedule_family_toggle_applied = (
        AsyncMock()
    )  # noqa: SLF001

    await coord.async_set_discharge_to_grid_schedule_enabled(True)

    coord.client.create_battery_schedule.assert_not_awaited()
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "dtgControl": {
                "enabled": True,
                "show": True,
                "locked": False,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 1140,
                "endTime": 1320,
            }
        },
        schedule_type="dtg",
    )
    assert coord._battery_dtg_schedule_enabled is True  # noqa: SLF001
    assert coord._battery_dtg_schedule_id is None  # noqa: SLF001
    assert coord._battery_dtg_begin_time is None  # noqa: SLF001
    assert coord._battery_dtg_end_time is None  # noqa: SLF001
    assert coord.battery_discharge_to_grid_start_time == dt_time(19, 0)
    assert coord.battery_discharge_to_grid_end_time == dt_time(22, 0)


@pytest.mark.asyncio
async def test_rbd_schedule_disabled_without_schedule_uses_battery_settings_toggle(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_no_schedule_family(coord, "rbd")
    coord.client.set_battery_settings = AsyncMock(return_value={})
    coord.battery_runtime._async_verify_schedule_family_toggle_applied = (
        AsyncMock()
    )  # noqa: SLF001

    await coord.async_set_restrict_battery_discharge_schedule_enabled(False)

    coord.client.create_battery_schedule.assert_not_awaited()
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "rbdControl": {
                "enabled": False,
                "show": True,
                "locked": False,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 60,
                "endTime": 960,
            }
        },
        schedule_type="rbd",
    )
    assert coord._battery_rbd_schedule_enabled is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_rbd_schedule_enabled_without_schedule_uses_battery_settings_toggle(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_no_schedule_family(coord, "rbd")
    with pytest.raises(
        ServiceValidationError,
        match="Create a restrict battery discharge schedule in the IQ Battery scheduler before enabling it.",
    ):
        await coord.async_set_restrict_battery_discharge_schedule_enabled(True)


@pytest.mark.asyncio
async def test_rbd_schedule_enabled_without_schedule_or_window_uses_battery_settings_toggle(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord.battery_runtime.parse_battery_settings_payload(
        {
            "data": {
                "batteryGridMode": "ImportExport",
                "rbdControl": {
                    "show": True,
                    "showDaySchedule": True,
                    "scheduleSupported": True,
                    "enabled": False,
                    "locked": False,
                },
            }
        }
    )
    coord._battery_schedules_payload = {
        "rbd": {"count": 0, "scheduleStatus": "active"}
    }  # noqa: SLF001
    with pytest.raises(
        ServiceValidationError,
        match="Create a restrict battery discharge schedule in the IQ Battery scheduler before enabling it.",
    ):
        await coord.async_set_restrict_battery_discharge_schedule_enabled(True)


@pytest.mark.asyncio
async def test_rbd_schedule_limit_update_still_omits_enabled_flag_when_disabled(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "rbd")
    coord._battery_rbd_schedule_enabled = False  # noqa: SLF001

    await coord.async_set_restrict_battery_discharge_schedule_limit(80)

    call = coord.client.update_battery_schedule.await_args
    assert call.kwargs["is_enabled"] is None


@pytest.mark.asyncio
async def test_dtg_schedule_toggle_forbidden_uses_schedule_validation_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="Forbidden",
        )
    )
    coord.client.set_battery_settings_compat = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="Forbidden",
        )
    )

    with pytest.raises(
        ServiceValidationError,
        match="Battery settings update was rejected by Enphase",
    ):
        await coord.async_set_discharge_to_grid_schedule_enabled(False)


@pytest.mark.asyncio
async def test_rbd_schedule_toggle_unauthorized_uses_schedule_validation_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_schedule_family(coord, "rbd")
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.UNAUTHORIZED,
            message="Unauthorized",
        )
    )

    with pytest.raises(
        ServiceValidationError, match="Schedule update could not be authenticated"
    ):
        await coord.async_set_restrict_battery_discharge_schedule_enabled(False)


def test_schedule_family_toggle_effective_state_prefers_explicit_disabled_signals(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    _seed_schedule_family(coord, "dtg")
    coord._battery_dtg_control = (
        runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "enabled": True,
            }
        )
    )
    coord._battery_dtg_schedule_enabled = False  # noqa: SLF001
    assert (
        runtime._schedule_family_toggle_effective_state("dtg") is False
    )  # noqa: SLF001

    _seed_schedule_family(coord, "rbd")
    coord._battery_rbd_control = (
        runtime._parse_battery_control_capability(  # noqa: SLF001
            {
                "show": True,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "enabled": False,
            }
        )
    )
    coord._battery_rbd_schedule_enabled = True  # noqa: SLF001
    assert (
        runtime._schedule_family_toggle_effective_state("rbd") is False
    )  # noqa: SLF001


def test_schedule_family_toggle_helper_branches_cover_cfg_unknown_and_status_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    coord._battery_cfg_schedule_enabled = True  # noqa: SLF001
    assert runtime._schedule_control_enabled_value("cfg") is True  # noqa: SLF001
    assert runtime._schedule_control_enabled_value("unknown") is None  # noqa: SLF001

    coord._battery_dtg_toggle_target_enabled = True  # noqa: SLF001
    assert (
        runtime._schedule_family_toggle_effective_state("dtg") is True
    )  # noqa: SLF001
    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"dtgControl": {"enabled": False}}}
    )
    assert (
        runtime._schedule_family_toggle_effective_state("dtg") is False
    )  # noqa: SLF001
    coord._battery_dtg_control = None  # noqa: SLF001

    coord._battery_cfg_schedule_enabled = None  # noqa: SLF001
    coord._battery_cfg_schedule_id = "sched-cfg"  # noqa: SLF001
    assert (
        runtime._schedule_family_toggle_effective_state("cfg") is None
    )  # noqa: SLF001

    coord._battery_dtg_toggle_target_enabled = None  # noqa: SLF001
    coord._battery_dtg_schedule_id = None  # noqa: SLF001
    coord._battery_dtg_schedule_enabled = None  # noqa: SLF001
    coord._battery_dtg_control = None  # noqa: SLF001
    coord._battery_dtg_schedule_status = "active"  # noqa: SLF001
    assert (
        runtime._schedule_family_toggle_effective_state("dtg") is False
    )  # noqa: SLF001

    coord._battery_dtg_schedule_status = None  # noqa: SLF001
    coord._battery_dtg_schedule_id = "sched-dtg"  # noqa: SLF001
    coord._battery_dtg_schedule_enabled = True  # noqa: SLF001
    assert (
        runtime._schedule_family_toggle_effective_state("dtg") is True
    )  # noqa: SLF001

    coord._battery_dtg_schedule_id = None  # noqa: SLF001
    assert (
        runtime._schedule_family_toggle_effective_state("dtg") is True
    )  # noqa: SLF001

    coord._battery_dtg_schedule_enabled = None  # noqa: SLF001
    assert (
        runtime._schedule_family_toggle_effective_state("dtg") is None
    )  # noqa: SLF001


def test_raise_schedule_update_validation_error_parses_conflict_payloads(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    conflict_cases = {
        "CONFLICTING_SCHEDULE_DTG": "existing discharge-to-grid schedule",
        "CONFLICTING_SCHEDULE_RBD": "existing restrict-battery-discharge schedule",
        "CONFLICTING_SCHEDULE_CFG": "existing charge-from-grid schedule",
    }
    for backend_status, expected in conflict_cases.items():
        err = aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.CONFLICT,
            message=f'{{"error": {{"status": "{backend_status}"}}}}',
        )
        with pytest.raises(ServiceValidationError, match=expected):
            runtime.raise_schedule_update_validation_error(err)  # noqa: SLF001

    err = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=HTTPStatus.CONFLICT,
        message='{"error": {"message": "Backend conflict"}}',
    )
    with pytest.raises(ServiceValidationError, match="Backend conflict\\."):
        runtime.raise_schedule_update_validation_error(err)  # noqa: SLF001

    err = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=HTTPStatus.CONFLICT,
        message="raw backend conflict",
    )
    with pytest.raises(ServiceValidationError, match="raw backend conflict\\."):
        runtime.raise_schedule_update_validation_error(err)  # noqa: SLF001

    err = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=HTTPStatus.CONFLICT,
        message='{"error": {}}',
    )
    with pytest.raises(
        ServiceValidationError,
        match="conflicts with an existing battery schedule",
    ):
        runtime.raise_schedule_update_validation_error(err)  # noqa: SLF001


def test_is_already_processed_profile_cancel_error_parsing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    assert (
        runtime._is_already_processed_profile_cancel_error(  # noqa: SLF001
            aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=HTTPStatus.BAD_REQUEST,
                message='{"error":{"status":"ALREADY_PROCESSED"}}',
            )
        )
        is False
    )
    assert (
        runtime._is_already_processed_profile_cancel_error(  # noqa: SLF001
            aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=HTTPStatus.CONFLICT,
                message="",
            )
        )
        is False
    )
    assert (
        runtime._is_already_processed_profile_cancel_error(  # noqa: SLF001
            aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=HTTPStatus.CONFLICT,
                message="ALREADY_PROCESSED raw conflict",
            )
        )
        is True
    )
    assert (
        runtime._is_already_processed_profile_cancel_error(  # noqa: SLF001
            aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=HTTPStatus.CONFLICT,
                message='{"error": []}',
            )
        )
        is False
    )


@pytest.mark.asyncio
async def test_async_apply_battery_settings_compat_handles_payload_and_auth_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    with pytest.raises(ServiceValidationError, match="payload is unavailable"):
        await runtime.async_apply_battery_settings_compat({})

    coord.client.set_battery_settings_compat = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.UNAUTHORIZED,
            message="Unauthorized",
        )
    )
    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await runtime.async_apply_battery_settings_compat({"chargeFromGrid": False})

    runtime.clear_battery_settings_write_pending()  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="boom",
        )
    )
    with pytest.raises(aiohttp.ClientResponseError):
        await runtime.async_apply_battery_settings_compat({"chargeFromGrid": False})


@pytest.mark.asyncio
async def test_dtg_schedule_toggle_unexpected_client_error_reraises(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord.client.set_battery_settings = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.async_set_discharge_to_grid_schedule_enabled(False)


@pytest.mark.asyncio
async def test_cfg_schedule_enabled_helper_delegates_to_cfg_settings_toggle(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
        force_schedule_supported=True,
    )
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord.battery_runtime.async_set_charge_from_grid_schedule_enabled = (
        AsyncMock()
    )  # noqa: SLF001

    await coord.battery_runtime._async_set_schedule_family_enabled(  # noqa: SLF001
        "cfg", False
    )

    coord.battery_runtime.async_set_charge_from_grid_schedule_enabled.assert_awaited_once_with(  # noqa: SLF001
        False
    )


@pytest.mark.asyncio
async def test_schedule_family_public_wrappers_delegate_to_generic_helpers(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime
    runtime._async_set_schedule_family_limit = AsyncMock()  # noqa: SLF001
    runtime._async_set_schedule_family_enabled = AsyncMock()  # noqa: SLF001

    await runtime.async_set_discharge_to_grid_schedule_limit(25)
    await runtime.async_set_restrict_battery_discharge_schedule_enabled(True)

    runtime._async_set_schedule_family_limit.assert_awaited_once_with(
        "dtg", 25
    )  # noqa: SLF001
    runtime._async_set_schedule_family_enabled.assert_awaited_once_with(  # noqa: SLF001
        "rbd", True
    )


@pytest.mark.asyncio
async def test_dtg_schedule_time_create_uses_control_window_defaults(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_no_schedule_family(coord, "dtg")

    await coord.async_set_discharge_to_grid_schedule_time(start=dt_time(17, 30))

    call = coord.client.create_battery_schedule.await_args
    assert call.kwargs["schedule_type"] == "DTG"
    assert call.kwargs["start_time"] == "17:30"
    assert call.kwargs["end_time"] == "22:00"
    assert call.kwargs["limit"] == 5
    assert call.kwargs["is_enabled"] is False
    assert coord._battery_dtg_begin_time == 1050  # noqa: SLF001
    assert coord._battery_dtg_end_time == 1320  # noqa: SLF001


@pytest.mark.asyncio
async def test_charge_from_grid_toggle_raises_when_backend_never_applies_change(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord.client.accept_battery_settings_disclaimer = AsyncMock(
        return_value={"message": "success"}
    )
    coord.client.validate_battery_schedule = AsyncMock(return_value={"isValid": True})
    coord.battery_runtime.async_apply_battery_settings = AsyncMock()  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.battery_runtime.async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.asyncio.sleep",
        AsyncMock(),
    )

    with pytest.raises(ServiceValidationError, match="toggle was not applied"):
        await coord.battery_runtime.async_set_charge_from_grid(True)


@pytest.mark.asyncio
async def test_charge_from_grid_schedule_enabled_uses_cfg_payload_and_compat_retry(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord.battery_runtime.parse_battery_settings_payload(
        {
            "data": {
                "cfgControl": {
                    "show": True,
                    "enabled": False,
                    "locked": False,
                    "showDaySchedule": True,
                    "scheduleSupported": True,
                    "forceScheduleSupported": True,
                }
            }
        }
    )
    runtime = coord.battery_runtime
    runtime.async_apply_battery_settings = AsyncMock()  # noqa: SLF001
    runtime.async_apply_battery_settings_compat = AsyncMock()  # noqa: SLF001

    refresh_calls = {"count": 0}

    async def _refresh(*, force: bool = False) -> None:
        refresh_calls["count"] += 1
        if refresh_calls["count"] >= 5:
            coord._battery_charge_from_grid = True  # noqa: SLF001
            coord._battery_charge_from_grid_schedule_enabled = True  # noqa: SLF001

    runtime.async_refresh_battery_settings = AsyncMock(
        side_effect=_refresh
    )  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.asyncio.sleep",
        AsyncMock(),
    )

    await runtime.async_set_charge_from_grid_schedule_enabled(True)

    payload = runtime.async_apply_battery_settings.await_args.args[0]
    assert payload == {
        "chargeFromGrid": True,
        "chargeFromGridScheduleEnabled": True,
        "chargeBeginTime": 120,
        "chargeEndTime": 300,
        "cfgControl": {
            "show": True,
            "enabled": False,
            "locked": False,
            "showDaySchedule": True,
            "scheduleSupported": True,
            "forceScheduleSupported": True,
            "forceScheduleOpted": True,
        },
        "acceptedItcDisclaimer": payload["acceptedItcDisclaimer"],
    }
    assert isinstance(payload["acceptedItcDisclaimer"], str)
    runtime.async_apply_battery_settings_compat.assert_awaited_once()


@pytest.mark.asyncio
async def test_charge_from_grid_schedule_enabled_raises_when_never_verified(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=True,
        force_schedule_supported=True,
    )
    runtime = coord.battery_runtime
    runtime.async_apply_battery_settings = AsyncMock()  # noqa: SLF001
    runtime.async_apply_battery_settings_compat = AsyncMock()  # noqa: SLF001
    runtime.async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.asyncio.sleep",
        AsyncMock(),
    )

    with pytest.raises(ServiceValidationError, match="toggle was not applied"):
        await runtime.async_set_charge_from_grid_schedule_enabled(False)


def test_schedule_family_toggle_validation_error_messages(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    assert (
        "Create a restrict battery discharge schedule"
        in runtime._schedule_family_toggle_validation_error(  # noqa: SLF001
            "rbd",
            enabled=True,
            schedule_id=None,
            current_start=None,
            current_end=None,
        )
    )

    _seed_schedule_family(coord, "rbd")
    coord._battery_rbd_control = None  # noqa: SLF001
    assert (
        "not currently exposed by Enphase"
        in runtime._schedule_family_toggle_validation_error(  # noqa: SLF001
            "rbd",
            enabled=True,
            schedule_id="sched-rbd",
            current_start=60,
            current_end=960,
        )
    )

    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._battery_dtg_control = None  # noqa: SLF001
    assert (
        "not currently exposed by Enphase"
        in runtime._schedule_family_toggle_validation_error(  # noqa: SLF001
            "dtg",
            enabled=True,
            schedule_id="sched-dtg",
            current_start=60,
            current_end=120,
        )
    )
    assert (
        runtime._schedule_family_toggle_validation_error(  # noqa: SLF001
            "cfg",
            enabled=False,
            schedule_id="sched-cfg",
            current_start=60,
            current_end=120,
        )
        == "Charge from grid toggle was not applied by Enphase."
    )


@pytest.mark.asyncio
async def test_verify_schedule_family_toggle_applied_cfg_and_dtg_raise_on_mismatch(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    runtime = coord.battery_runtime
    runtime.async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.asyncio.sleep",
        AsyncMock(),
    )

    with pytest.raises(
        ServiceValidationError, match="Charge from grid toggle was not applied"
    ):
        await runtime._async_verify_schedule_family_toggle_applied(  # noqa: SLF001
            "cfg",
            enabled=False,
        )

    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord._battery_dtg_control = None  # noqa: SLF001
    coord._battery_dtg_schedule_enabled = None  # noqa: SLF001
    coord._battery_dtg_schedule_status = None  # noqa: SLF001
    runtime = coord.battery_runtime
    runtime.async_refresh_battery_settings = AsyncMock()  # noqa: SLF001
    runtime.async_refresh_battery_schedules = AsyncMock()  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.asyncio.sleep",
        AsyncMock(),
    )

    with pytest.raises(
        ServiceValidationError, match="Discharge to grid is not currently exposed"
    ):
        await runtime._async_verify_schedule_family_toggle_applied(  # noqa: SLF001
            "dtg",
            enabled=True,
        )


@pytest.mark.asyncio
async def test_verify_schedule_family_toggle_applied_returns_on_matched_states(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    runtime = coord.battery_runtime

    async def _cfg_refresh(*, force: bool = False) -> None:
        coord._battery_cfg_schedule_enabled = True  # noqa: SLF001

    runtime.async_refresh_battery_settings = AsyncMock(
        side_effect=_cfg_refresh
    )  # noqa: SLF001
    await runtime._async_verify_schedule_family_toggle_applied(
        "cfg", enabled=True
    )  # noqa: SLF001

    async def _cfg_refresh_off(*, force: bool = False) -> None:
        coord._battery_cfg_schedule_enabled = False  # noqa: SLF001

    runtime.async_refresh_battery_settings = AsyncMock(
        side_effect=_cfg_refresh_off
    )  # noqa: SLF001
    await runtime._async_verify_schedule_family_toggle_applied(
        "cfg", enabled=False
    )  # noqa: SLF001


@pytest.mark.asyncio
async def test_create_schedule_family_uses_validation_error_on_client_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_no_schedule_family(coord, "dtg")
    coord.client.create_battery_schedule = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="403 Forbidden"):
        await coord.battery_runtime._async_create_or_update_schedule_family(  # noqa: SLF001
            "dtg",
            start_minutes=60,
            end_minutes=120,
            limit=10,
            is_enabled=True,
        )


@pytest.mark.asyncio
async def test_create_schedule_family_rejects_local_overlap_before_client_call(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_no_schedule_family(coord, "dtg")
    coord._battery_schedules_payload = {  # noqa: SLF001
        "cfg": {
            "details": [
                {
                    "scheduleId": "sched-cfg",
                    "startTime": "02:00",
                    "endTime": "05:00",
                    "limit": 100,
                    "days": [1, 2, 3, 4, 5, 6, 7],
                    "timezone": "Australia/Melbourne",
                    "isEnabled": False,
                }
            ]
        },
        "dtg": {"details": []},
        "rbd": {"details": []},
    }

    with pytest.raises(
        ServiceValidationError, match="existing charge from grid schedule"
    ):
        await coord.battery_runtime._async_create_or_update_schedule_family(  # noqa: SLF001
            "dtg",
            start_minutes=150,
            end_minutes=240,
            limit=10,
            is_enabled=False,
        )

    coord.client.create_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_schedule_family_reraises_client_error_when_helper_does_not(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_no_schedule_family(coord, "dtg")
    err = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="boom",
    )
    coord.client.create_battery_schedule = AsyncMock(side_effect=err)
    coord.battery_runtime.raise_schedule_update_validation_error = (
        MagicMock()
    )  # noqa: SLF001

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime._async_create_or_update_schedule_family(  # noqa: SLF001
            "dtg",
            start_minutes=60,
            end_minutes=120,
            limit=10,
            is_enabled=True,
        )


@pytest.mark.asyncio
async def test_async_update_battery_schedule_rejects_local_overlap_before_client_call(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord._battery_schedules_payload = {  # noqa: SLF001
        "cfg": {
            "details": [
                {
                    "scheduleId": "sched-cfg",
                    "startTime": "02:00",
                    "endTime": "05:00",
                    "limit": 100,
                    "days": [1, 2, 3, 4, 5, 6, 7],
                    "timezone": "Australia/Melbourne",
                    "isEnabled": False,
                }
            ]
        },
        "dtg": {
            "details": [
                {
                    "scheduleId": "sched-dtg",
                    "startTime": "18:00",
                    "endTime": "23:00",
                    "limit": 5,
                    "days": [1, 2, 3, 4, 5, 6, 7],
                    "timezone": "Europe/London",
                    "isEnabled": True,
                }
            ]
        },
        "rbd": {"details": []},
    }

    with pytest.raises(
        ServiceValidationError, match="existing charge from grid schedule"
    ):
        await coord.battery_runtime.async_update_battery_schedule(
            "sched-dtg",
            schedule_type="DTG",
            start_time="02:30",
            end_time="04:00",
            limit=5,
            days=[1],
            timezone="Europe/London",
        )

    coord.client.update_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_rbd_enable_requires_exposed_control_when_schedule_exists(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "rbd")
    coord._battery_rbd_control = None  # noqa: SLF001

    with pytest.raises(
        ServiceValidationError, match="not currently exposed by Enphase"
    ):
        await coord.battery_runtime._async_set_schedule_family_enabled(  # noqa: SLF001
            "rbd",
            True,
        )


@pytest.mark.asyncio
async def test_dtg_toggle_uses_compat_retry_after_service_validation_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "dtg")
    coord.client.set_battery_settings = AsyncMock(return_value={})
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})
    coord.battery_runtime._async_verify_schedule_family_toggle_applied = (
        AsyncMock(  # noqa: SLF001
            side_effect=[
                ServiceValidationError("primary write rejected"),
                None,
            ]
        )
    )

    await coord.battery_runtime._async_set_schedule_family_enabled(  # noqa: SLF001
        "dtg",
        False,
    )

    coord.client.set_battery_settings_compat.assert_awaited_once()
    assert (
        coord.battery_runtime._async_verify_schedule_family_toggle_applied.await_count  # noqa: SLF001
        == 2
    )
    assert coord._battery_dtg_toggle_target_enabled is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_rbd_toggle_uses_compat_retry_after_service_validation_error(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_schedule_family(coord, "rbd")
    coord.client.set_battery_settings = AsyncMock(return_value={})
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})
    coord.battery_runtime._async_verify_schedule_family_toggle_applied = (
        AsyncMock(  # noqa: SLF001
            side_effect=[
                ServiceValidationError("primary write rejected"),
                None,
            ]
        )
    )

    await coord.battery_runtime._async_set_schedule_family_enabled(  # noqa: SLF001
        "rbd",
        False,
    )

    coord.client.set_battery_settings.assert_awaited_once()
    coord.client.set_battery_settings_compat.assert_awaited_once()
    assert (
        coord.battery_runtime._async_verify_schedule_family_toggle_applied.await_count  # noqa: SLF001
        == 2
    )
    assert coord._battery_rbd_toggle_target_enabled is None  # noqa: SLF001


def test_coordinator_schedule_support_and_effective_enabled_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    assert (
        coord._battery_schedule_supported(  # noqa: SLF001
            None,
            schedule_id=None,
            start_minutes=None,
            end_minutes=None,
            schedule_status="active",
        )
        is True
    )
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001

    coord._battery_dtg_toggle_target_enabled = True  # noqa: SLF001
    assert coord._battery_schedule_effective_enabled("dtg") is True  # noqa: SLF001
    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"dtgControl": {"enabled": False}}}
    )
    assert coord._battery_schedule_effective_enabled("dtg") is False  # noqa: SLF001

    coord._battery_dtg_toggle_target_enabled = None  # noqa: SLF001
    coord._battery_dtg_control = None  # noqa: SLF001
    coord._battery_dtg_schedule_status = None  # noqa: SLF001
    coord.battery_runtime.parse_battery_settings_payload(
        {"data": {"cfgControl": {"forceScheduleOpted": True}}}
    )
    assert coord._battery_schedule_effective_enabled("cfg") is True  # noqa: SLF001

    coord.battery_runtime.parse_battery_settings_payload({"data": {"cfgControl": {}}})
    coord._battery_cfg_schedule_id = "sched-cfg"  # noqa: SLF001
    assert coord._battery_schedule_effective_enabled("cfg") is None  # noqa: SLF001

    coord._battery_dtg_control = None  # noqa: SLF001
    coord._battery_dtg_schedule_id = None  # noqa: SLF001
    coord._battery_dtg_schedule_enabled = None  # noqa: SLF001
    coord._battery_dtg_schedule_status = "active"  # noqa: SLF001
    assert coord.battery_discharge_to_grid_schedule_enabled is False

    assert coord._battery_schedule_effective_enabled("unknown") is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_rbd_schedule_time_create_omits_limit_when_unknown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    _seed_no_schedule_family(coord, "rbd")

    await coord.async_set_restrict_battery_discharge_schedule_time(end=dt_time(15, 0))

    call = coord.client.create_battery_schedule.await_args
    assert call.kwargs["schedule_type"] == "RBD"
    assert call.kwargs["start_time"] == "01:00"
    assert call.kwargs["end_time"] == "15:00"
    assert call.kwargs["limit"] is None
    assert call.kwargs["is_enabled"] is False
    coord.client.set_battery_settings.assert_awaited_once_with(
        {
            "rbdControl": {
                "enabled": False,
                "show": True,
                "locked": False,
                "showDaySchedule": True,
                "scheduleSupported": True,
                "startTime": 60,
                "endTime": 900,
            }
        },
        schedule_type="rbd",
    )


async def test_async_update_data_site_only_ignores_battery_schedule_refresh_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("boom")
    )
    coord._async_refresh_storm_guard_profile = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_devices_inventory = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(return_value=None)  # noqa: SLF001

    assert await coord._async_update_data() == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_async_update_data_ignores_battery_schedule_refresh_errors(
    coordinator_factory,
) -> None:
    from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

    coord = coordinator_factory()
    coord.summary = MagicMock()
    coord.summary.prepare_refresh = MagicMock(return_value=False)
    coord.summary.async_fetch = AsyncMock(return_value=[])
    coord.summary.invalidate = MagicMock()
    coord.session_history = MagicMock()
    coord.session_history.get_cache_view = MagicMock(
        return_value=MagicMock(sessions=[], needs_refresh=False, blocked=False)
    )
    coord.session_history.sum_energy = MagicMock(return_value=0.0)
    coord.client.status = AsyncMock(
        return_value={
            "ts": "2026-02-28T00:00:00Z",
            "evChargerData": [
                {
                    "sn": RANDOM_SERIAL,
                    "name": "EV",
                    "connectors": [{}],
                    "pluggedIn": False,
                    "charging": False,
                    "faulted": False,
                    "session_d": {},
                }
            ],
        }
    )
    coord.energy._async_refresh_site_energy = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_site_settings = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_status = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_backup_history = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_battery_settings = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_battery_schedules = AsyncMock(  # noqa: SLF001
        side_effect=aiohttp.ClientError("boom")
    )
    coord._async_refresh_storm_guard_profile = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_storm_alert = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_grid_control_check = AsyncMock(
        return_value=None
    )  # noqa: SLF001
    coord._async_refresh_inverters = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_hems_devices = AsyncMock(return_value=None)  # noqa: SLF001
    coord._async_refresh_heatpump_power = AsyncMock(return_value=None)  # noqa: SLF001

    result = await coord._async_update_data()  # noqa: SLF001

    assert RANDOM_SERIAL in result


# ---------------------------------------------------------------------------
# CFG schedule creation – no existing schedule
# ---------------------------------------------------------------------------


def _seed_no_cfg_schedule(coord):
    """Populate coordinator for an EMEA site with no existing CFG schedule."""
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_from_grid_schedule_enabled = False  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_cfg_schedule_id = None  # noqa: SLF001
    coord._battery_cfg_schedule_limit = None  # noqa: SLF001
    coord._battery_schedules_payload = {"cfg": {"details": []}}  # noqa: SLF001
    coord.client.create_battery_schedule = AsyncMock(return_value={})
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.client.validate_battery_schedule = AsyncMock(return_value={"isValid": True})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()


@pytest.mark.asyncio
async def test_cfg_schedule_creates_when_none_exists(
    coordinator_factory,
) -> None:
    """When no CFG schedule exists but the API is available, create one."""
    coord = coordinator_factory()
    _seed_no_cfg_schedule(coord)

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(22, 0), end=dt_time(8, 0)
    )

    coord.client.create_battery_schedule.assert_awaited_once_with(
        schedule_type="CFG",
        start_time="22:00",
        end_time="08:00",
        limit=100,
        days=[1, 2, 3, 4, 5, 6, 7],
        timezone="UTC",
        is_enabled=False,
    )
    coord.client.set_battery_settings.assert_awaited_once()
    payload = coord.client.set_battery_settings.await_args.args[0]
    assert payload["chargeFromGrid"] is True
    assert isinstance(payload["acceptedItcDisclaimer"], str)
    coord.client.validate_battery_schedule.assert_awaited_once_with("cfg")
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_cfg_schedule_create_uses_stored_timezone(
    coordinator_factory,
) -> None:
    """New CFG schedule should use the timezone from a previous poll."""
    coord = coordinator_factory()
    _seed_no_cfg_schedule(coord)
    coord._battery_cfg_schedule_timezone = "Europe/Lisbon"  # noqa: SLF001

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(22, 0), end=dt_time(8, 0)
    )

    call = coord.client.create_battery_schedule.await_args
    assert call.kwargs["timezone"] == "Europe/Lisbon"


@pytest.mark.asyncio
async def test_cfg_schedule_create_acquires_write_lock(
    coordinator_factory,
) -> None:
    """Creating a new CFG schedule must use the battery write lock."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_no_cfg_schedule(coord)

    await coord._battery_settings_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
                start=dt_time(22, 0), end=dt_time(8, 0)
            )
    finally:
        coord._battery_settings_write_lock.release()  # noqa: SLF001


@pytest.mark.asyncio
async def test_legacy_fallback_when_create_api_unavailable(
    coordinator_factory,
) -> None:
    """Without the /schedules API, the legacy batterySettings PUT is used."""
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_hide_charge_from_grid = False  # noqa: SLF001
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001
    coord._battery_cfg_schedule_id = None  # noqa: SLF001
    # No create_battery_schedule on client — legacy path should fire.
    coord.client.set_battery_settings = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(7, 0)
    )

    coord.client.set_battery_settings.assert_awaited_once()
    args = coord.client.set_battery_settings.await_args.args
    payload = args[0]
    assert payload["chargeBeginTime"] == 1380
    assert payload["chargeEndTime"] == 420


# ---------------------------------------------------------------------------
# Pending state tracking – write guards and status parsing
# ---------------------------------------------------------------------------


def test_parse_battery_schedules_captures_entry_status(
    coordinator_factory,
) -> None:
    """Schedule status from individual entry is parsed and lowercased."""
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001

    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "details": [
                    {
                        "isEnabled": True,
                        "startTime": "22:00",
                        "endTime": "08:00",
                        "limit": 100,
                        "scheduleId": "sched-1",
                        "scheduleStatus": "Pending",
                    }
                ],
            }
        }
    )

    assert coord.battery_cfg_schedule_status == "pending"
    assert coord.battery_cfg_schedule_pending is True


def test_parse_battery_schedules_captures_family_status(
    coordinator_factory,
) -> None:
    """Schedule status from the cfg family level is used as fallback."""
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001

    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "scheduleStatus": "Active",
                "details": [
                    {
                        "isEnabled": True,
                        "startTime": "22:00",
                        "endTime": "08:00",
                        "limit": 100,
                        "scheduleId": "sched-1",
                    }
                ],
            }
        }
    )

    assert coord.battery_cfg_schedule_status == "active"
    assert coord.battery_cfg_schedule_pending is False


def test_parse_battery_schedules_resets_stale_status(
    coordinator_factory,
) -> None:
    """A previously cached pending status is cleared when the next refresh omits it."""
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001

    # Refresh with a payload that has no scheduleStatus anywhere.
    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "details": [
                    {
                        "isEnabled": True,
                        "startTime": "22:00",
                        "endTime": "08:00",
                        "limit": 100,
                        "scheduleId": "sched-1",
                    }
                ],
            }
        }
    )

    assert coord.battery_cfg_schedule_status is None
    assert coord.battery_cfg_schedule_pending is False


def test_parse_battery_schedules_ignores_blank_status(
    coordinator_factory,
) -> None:
    """Blank or whitespace-only scheduleStatus is treated as absent."""
    coord = coordinator_factory()
    coord._battery_has_encharge = True  # noqa: SLF001

    coord.battery_runtime.parse_battery_schedules_payload(
        {
            "cfg": {
                "scheduleStatus": "  ",
                "details": [
                    {
                        "isEnabled": True,
                        "startTime": "22:00",
                        "endTime": "08:00",
                        "limit": 100,
                        "scheduleId": "sched-1",
                    }
                ],
            }
        }
    )

    assert coord.battery_cfg_schedule_status is None
    assert coord.battery_cfg_schedule_pending is False


def test_battery_cfg_schedule_status_defaults_to_none(
    coordinator_factory,
) -> None:
    """Without any schedule data, status properties return sensible defaults."""
    coord = coordinator_factory()

    assert coord.battery_cfg_schedule_status is None
    assert coord.battery_cfg_schedule_pending is False


@pytest.mark.asyncio
async def test_schedule_time_write_guard_rejects_when_pending(
    coordinator_factory,
) -> None:
    """Setting schedule time must fail while a change is pending Envoy sync."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="pending Envoy sync"):
        await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
            start=dt_time(23, 0), end=dt_time(6, 0)
        )

    # No API calls should have been made.
    coord.client.update_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_time_write_guard_allows_when_active(
    coordinator_factory,
) -> None:
    """Setting schedule time must succeed when status is active."""
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_status = "active"  # noqa: SLF001

    await coord.battery_runtime.async_set_charge_from_grid_schedule_time(
        start=dt_time(23, 0), end=dt_time(6, 0)
    )

    coord.client.update_battery_schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_cfg_schedule_limit_write_guard_rejects_when_pending(
    coordinator_factory,
) -> None:
    """Setting CFG limit must fail while a change is pending Envoy sync."""
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_status = "pending"  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="pending Envoy sync"):
        await coord.battery_runtime.async_set_cfg_schedule_limit(90)

    coord.client.update_battery_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_cfg_schedule_limit_write_guard_allows_when_active(
    coordinator_factory,
) -> None:
    """Setting CFG limit must succeed when status is active."""
    coord = coordinator_factory()
    _seed_cfg_schedule(coord)
    coord._battery_cfg_schedule_status = "active"  # noqa: SLF001

    await coord.battery_runtime.async_set_cfg_schedule_limit(90)

    coord.client.update_battery_schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_battery_runtime_refresh_storm_guard_profile_caches_and_handles_bad_locale(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class BadConfig:
        @property
        def language(self):
            raise RuntimeError("boom")

    coord.hass.config = BadConfig()
    coord.client.storm_guard_profile = AsyncMock(
        side_effect=[
            {"data": {"stormGuardState": "active", "evseStormEnabled": False}},
            {"data": {"stormGuardState": "disabled", "evseStormEnabled": True}},
        ]
    )

    await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.storm_guard_state == "enabled"
    assert coord.storm_evse_enabled is False

    await coord.battery_runtime.async_refresh_storm_guard_profile()
    assert coord.client.storm_guard_profile.await_count == 1

    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    await coord.battery_runtime.async_refresh_storm_guard_profile()
    assert coord.client.storm_guard_profile.await_count == 2
    assert coord.storm_guard_state == "disabled"
    assert coord.storm_evse_enabled is True


@pytest.mark.asyncio
async def test_battery_runtime_refresh_storm_alert_parses_payloads_and_cache_short_circuits(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.storm_guard_alert = AsyncMock(
        side_effect=[
            {"criticalAlertActive": False, "criticalAlertsOverride": True},
            {
                "stormAlerts": [
                    {"type": "wind", "severity": "critical"},
                    "legacy-alert",
                ]
            },
            {
                "criticalAlertActive": False,
                "stormAlerts": [
                    {"id": "IDV21037", "name": "Severe Weather", "status": "opted-out"},
                ],
            },
        ]
    )

    await coord.battery_runtime.async_refresh_storm_alert(force=True)
    assert coord.storm_alert_active is False
    assert coord.storm_alert_critical_override is True

    await coord.battery_runtime.async_refresh_storm_alert(force=True)
    assert coord.storm_alert_active is True
    assert coord.storm_alerts[0]["type"] == "wind"
    assert coord.storm_alerts[1]["value"] == "legacy-alert"

    await coord.battery_runtime.async_refresh_storm_alert(force=True)
    assert coord.storm_alert_active is False
    assert coord.storm_alerts[0]["status"] == "opted-out"

    coord.client.storm_guard_alert.reset_mock()
    await coord.battery_runtime.async_refresh_storm_alert()
    coord.client.storm_guard_alert.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_storm_guard_profile_cache_ttl_tracks_polling_cadence(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    coord.update_interval = timedelta(seconds=120)
    coord._configured_slow_poll_interval = 120  # noqa: SLF001
    coord._endpoint_family_should_run = lambda *args, **kwargs: True  # noqa: SLF001
    coord._note_endpoint_family_success = lambda *args, **kwargs: None  # noqa: SLF001
    coord._note_endpoint_family_failure = lambda *args, **kwargs: None  # noqa: SLF001
    coord.client.storm_guard_profile = AsyncMock(
        side_effect=[
            {"data": {"stormGuardState": "enabled", "evseStormEnabled": False}},
            {"data": {"stormGuardState": "disabled", "evseStormEnabled": True}},
        ]
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.time.monotonic",
        _MonotonicSequence(200.0, 319.0, 321.0),
    )

    await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)

    assert coord.client.storm_guard_profile.await_count == 1
    assert coord._storm_guard_cache_until == 320.0  # noqa: SLF001

    await coord.battery_runtime.async_refresh_storm_guard_profile()

    assert coord.client.storm_guard_profile.await_count == 1

    await coord.battery_runtime.async_refresh_storm_guard_profile()

    assert coord.client.storm_guard_profile.await_count == 2
    assert coord.storm_guard_state == "disabled"
    assert coord.storm_evse_enabled is True


@pytest.mark.asyncio
async def test_refresh_storm_guard_profile_respects_endpoint_family_gate(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._endpoint_family_should_run = lambda *args, **kwargs: False  # noqa: SLF001
    coord.client.storm_guard_profile = AsyncMock()

    await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)

    coord.client.storm_guard_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_runtime_async_opt_out_all_storm_alerts_targets_active_alerts(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.storm_guard_alert = AsyncMock(
        side_effect=[
            {
                "stormAlerts": [
                    {"id": "IDV21037", "name": "Severe Weather", "status": "active"},
                    {"id": "IDV21038", "name": "Flood Warning"},
                    {"id": "IDV21037", "name": "Duplicate", "status": "active"},
                    {"id": "IDV21039", "name": "Cleared", "status": "inactive"},
                ]
            },
            {"stormAlerts": []},
        ]
    )
    coord.client.opt_out_storm_alert = AsyncMock(return_value={"message": "success"})
    coord.kick_fast = MagicMock()
    coord.async_request_refresh = AsyncMock()

    await coord.battery_runtime.async_opt_out_all_storm_alerts()

    assert coord.client.opt_out_storm_alert.await_args_list == [
        call(alert_id="IDV21037", name="Severe Weather"),
        call(alert_id="IDV21038", name="Flood Warning"),
    ]
    assert coord.client.storm_guard_alert.await_count == 2
    coord.kick_fast.assert_called_once()
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_battery_runtime_async_opt_out_all_storm_alerts_maps_failures(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.storm_guard_alert = AsyncMock(
        side_effect=[
            {"stormAlerts": [{"id": "IDV21037", "name": "Severe Weather"}]},
            RuntimeError("refresh"),
        ]
    )
    coord.client.opt_out_storm_alert = AsyncMock(side_effect=RuntimeError("boom"))
    coord.kick_fast = MagicMock()
    coord.async_request_refresh = AsyncMock()

    with pytest.raises(
        ServiceValidationError,
        match=r"Storm Alert opt-out failed for 1 alert\(s\)\.",
    ):
        await coord.battery_runtime.async_opt_out_all_storm_alerts()

    coord.kick_fast.assert_not_called()
    coord.async_request_refresh.assert_not_awaited()

    coord.client.storm_guard_alert = AsyncMock(
        return_value={
            "stormAlerts": [
                {"id": "IDV21037", "name": "Severe Weather", "status": "opted-out"}
            ]
        }
    )
    coord.client.opt_out_storm_alert = AsyncMock(return_value={"message": "success"})
    await coord.battery_runtime.async_opt_out_all_storm_alerts()
    coord.client.opt_out_storm_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_runtime_async_set_storm_guard_enabled_success_and_error_paths(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._storm_evse_enabled = True  # noqa: SLF001
    coord.client.storm_guard_profile = AsyncMock(
        side_effect=[
            {"data": {"stormGuardState": "disabled", "evseStormEnabled": True}},
            {"data": {"stormGuardState": "enabled", "evseStormEnabled": True}},
        ]
    )
    coord.client.set_storm_guard = AsyncMock(return_value={"message": "success"})

    await coord.battery_runtime.async_set_storm_guard_enabled(True)
    assert coord.storm_guard_update_pending is True
    await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)
    assert coord.storm_guard_update_pending is False

    coord._storm_evse_enabled = None  # noqa: SLF001
    coord.client.storm_guard_profile = AsyncMock(return_value={"data": {}})
    with pytest.raises(
        ServiceValidationError, match="Storm Guard settings are unavailable"
    ):
        await coord.battery_runtime.async_set_storm_guard_enabled(True)

    coord._storm_evse_enabled = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.storm_guard_profile = AsyncMock(
        return_value={"data": {"stormGuardState": "disabled", "evseStormEnabled": True}}
    )
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="forbidden",
        )
    )
    with pytest.raises(
        ServiceValidationError,
        match="Storm Guard updates are not permitted for this account.",
    ):
        await coord.battery_runtime.async_set_storm_guard_enabled(True)

    coord._battery_user_is_owner = True  # noqa: SLF001
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.UNAUTHORIZED,
            message="unauthorized",
        )
    )
    with pytest.raises(
        ServiceValidationError,
        match="could not be authenticated",
    ):
        await coord.battery_runtime.async_set_storm_guard_enabled(True)


@pytest.mark.asyncio
async def test_battery_runtime_async_set_storm_evse_enabled_success_and_error_paths(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord.client.storm_guard_profile = AsyncMock(
        return_value={"data": {"stormGuardState": "enabled", "evseStormEnabled": False}}
    )
    coord.client.set_storm_guard = AsyncMock(return_value={"message": "success"})

    await coord.battery_runtime.async_set_storm_evse_enabled(True)
    assert coord.storm_evse_enabled is True

    coord._storm_guard_state = None  # noqa: SLF001
    coord.client.storm_guard_profile = AsyncMock(return_value={"data": {}})
    with pytest.raises(
        ServiceValidationError, match="Storm Guard settings are unavailable"
    ):
        await coord.battery_runtime.async_set_storm_evse_enabled(True)

    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="forbidden",
        )
    )
    with pytest.raises(
        ServiceValidationError,
        match="Storm Guard update was rejected by Enphase",
    ):
        await coord.battery_runtime.async_set_storm_evse_enabled(True)

    coord._battery_user_is_owner = False  # noqa: SLF001
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="forbidden",
        )
    )
    with pytest.raises(
        ServiceValidationError,
        match="Storm Guard updates are not permitted for this account.",
    ):
        await coord.battery_runtime.async_set_storm_evse_enabled(True)


def test_battery_runtime_storm_alert_and_guard_helper_edge_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    class BadText:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert runtime.normalize_storm_guard_state(True) == "enabled"
    assert runtime.normalize_storm_guard_state(0) == "disabled"
    assert runtime.normalize_storm_guard_state(" yes ") == "enabled"
    assert runtime.normalize_storm_guard_state("active") == "enabled"
    assert runtime.normalize_storm_guard_state("off") == "disabled"
    assert runtime.storm_alert_status_is_inactive(None) is False
    assert runtime.storm_alert_is_active({"active": True}) is True
    assert runtime.parse_storm_alert("bad") is None

    runtime.set_storm_guard_pending("enabled")
    assert coord.storm_guard_update_pending is True
    runtime.set_storm_guard_pending("not-a-state")
    assert coord.storm_guard_update_pending is False

    assert (
        runtime.parse_storm_alert(
            {
                "stormAlerts": [
                    {"priority": 1, "enabled": True},
                    {},
                    BadText(),
                ]
            }
        )
        is True
    )
    assert coord.storm_alerts[0] == {"priority": 1, "enabled": True}
    assert coord.storm_alerts[1] == {"active": True}
    assert coord.storm_alerts[2] == {"active": True}


@pytest.mark.asyncio
async def test_battery_runtime_storm_alert_opt_out_validation_and_refresh_reraise(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.storm_guard_alert = AsyncMock(
        side_effect=[
            {
                "stormAlerts": [
                    "legacy",
                    {"id": "IDV21037", "name": "Severe Weather", "status": "active"},
                ]
            },
            RuntimeError("refresh"),
        ]
    )
    coord.client.opt_out_storm_alert = None

    with pytest.raises(ServiceValidationError, match="opt-out is unavailable"):
        await coord.battery_runtime.async_opt_out_all_storm_alerts()

    coord.client.storm_guard_alert = AsyncMock(
        side_effect=[
            {"stormAlerts": [{"id": "IDV21037", "name": "Severe Weather"}]},
            RuntimeError("refresh"),
        ]
    )
    coord.client.opt_out_storm_alert = AsyncMock(return_value={"message": "success"})

    with pytest.raises(RuntimeError, match="refresh"):
        await coord.battery_runtime.async_opt_out_all_storm_alerts()

    coord._storm_alerts = [
        "legacy",
        {"id": "IDV21037", "name": "Severe Weather"},
    ]  # noqa: SLF001
    coord.async_refresh_storm_alert = AsyncMock(return_value=None)  # type: ignore[method-assign]
    coord.client.opt_out_storm_alert = None

    with pytest.raises(ServiceValidationError, match="opt-out is unavailable"):
        await coord.battery_runtime.async_opt_out_all_storm_alerts()


@pytest.mark.asyncio
async def test_battery_runtime_storm_guard_and_evse_error_mapping_edges(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._storm_evse_enabled = True  # noqa: SLF001
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.storm_guard_profile = AsyncMock(
        return_value={"data": {"stormGuardState": "enabled", "evseStormEnabled": True}}
    )

    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="forbidden",
        )
    )
    with pytest.raises(
        ServiceValidationError,
        match="Storm Guard update was rejected by Enphase",
    ):
        await coord.battery_runtime.async_set_storm_guard_enabled(False)
    assert coord.storm_guard_update_pending is False

    coord.client.set_storm_guard = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await coord.battery_runtime.async_set_storm_guard_enabled(True)
    assert coord.storm_guard_update_pending is False

    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="forbidden",
        )
    )
    with pytest.raises(
        ServiceValidationError,
        match="not permitted for this account",
    ):
        await coord.battery_runtime.async_set_storm_guard_enabled(True)

    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.UNAUTHORIZED,
            message="unauthorized",
        )
    )
    with pytest.raises(ServiceValidationError, match="could not be authenticated"):
        await coord.battery_runtime.async_set_storm_evse_enabled(True)

    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="forbidden",
        )
    )
    with pytest.raises(
        ServiceValidationError,
        match="not permitted for this account",
    ):
        await coord.battery_runtime.async_set_storm_evse_enabled(True)

    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="boom",
        )
    )
    with pytest.raises(aiohttp.ClientResponseError):
        await coord.battery_runtime.async_set_storm_evse_enabled(True)


@pytest.mark.asyncio
async def test_battery_runtime_storm_guard_direct_error_branch_coverage(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._storm_evse_enabled = True  # noqa: SLF001
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    monkeypatch.setattr(
        runtime,
        "async_ensure_battery_write_access_confirmed",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        coord,
        "async_refresh_storm_guard_profile",
        AsyncMock(return_value=None),
    )
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="forbidden",
        )
    )

    with pytest.raises(
        ServiceValidationError,
        match="not permitted for this account",
    ):
        await runtime.async_set_storm_guard_enabled(True)

    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await runtime.async_set_storm_guard_enabled(True)


@pytest.mark.asyncio
async def test_battery_runtime_storm_alert_loop_skips_non_dict_entries(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    runtime = coord.battery_runtime
    monkeypatch.setattr(
        type(coord),
        "storm_alerts",
        property(lambda _self: ["legacy", {"id": "IDV21037", "name": "Storm"}]),
    )
    monkeypatch.setattr(
        coord,
        "async_refresh_storm_alert",
        AsyncMock(return_value=None),
    )
    coord.client.opt_out_storm_alert = None

    with pytest.raises(ServiceValidationError, match="opt-out is unavailable"):
        await runtime.async_opt_out_all_storm_alerts()


@pytest.mark.asyncio
async def test_battery_runtime_storm_evse_forbidden_owner_branch_coverage(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    runtime = coord.battery_runtime
    coord._storm_guard_state = "enabled"  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    monkeypatch.setattr(
        runtime,
        "async_ensure_battery_write_access_confirmed",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        coord,
        "async_refresh_storm_guard_profile",
        AsyncMock(return_value=None),
    )
    coord.client.set_storm_guard = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=HTTPStatus.FORBIDDEN,
            message="forbidden",
        )
    )

    with pytest.raises(
        ServiceValidationError,
        match="not permitted for this account",
    ):
        await runtime.async_set_storm_evse_enabled(True)
