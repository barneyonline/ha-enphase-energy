from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.enphase_ev.const import FAST_TOGGLE_POLL_HOLD_S
from custom_components.enphase_ev.state_models import BatteryControlCapability


def _profile_payload(
    *,
    profile: str = "self-consumption",
    reserve: int = 20,
    subtype: str | None = None,
) -> dict:
    data: dict[str, object] = {
        "profile": profile,
        "batteryBackupPercentage": reserve,
        "stormGuardState": "disabled",
        "evseStormEnabled": False,
        "devices": {
            "iqEvse": [{"uuid": "evse-1", "chargeMode": "MANUAL", "enable": False}]
        },
    }
    if subtype is not None:
        data["operationModeSubType"] = subtype
    return {"data": data}


@pytest.mark.asyncio
async def test_refresh_battery_site_settings_parses_flags(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.battery_site_settings = AsyncMock(
        return_value={
            "data": {
                "showProduction": True,
                "showConsumption": True,
                "showChargeFromGrid": True,
                "showSavingsMode": True,
                "showAiOptiSavingsMode": True,
                "showStormGuard": True,
                "showFullBackup": False,
                "showBatteryBackupPercentage": True,
                "isChargingModesEnabled": True,
                "hasEncharge": True,
                "hasEnpower": False,
                "countryCode": "US",
                "region": "CA",
                "locale": "en-US",
                "timezone": "America/Los_Angeles",
                "featureDetails": {"HEMS_EV_Custom_Schedule": True},
                "userDetails": {"isOwner": True, "isInstaller": False},
                "siteStatus": {"code": "normal", "text": "Normal", "severity": "info"},
            }
        }
    )

    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001

    assert coord.battery_has_encharge is True
    assert coord.battery_has_enpower is False
    assert coord.battery_is_charging_modes_enabled is True
    assert coord.battery_show_storm_guard is True
    assert coord.battery_show_production is True
    assert coord.battery_show_consumption is True
    assert coord.battery_country_code == "US"
    assert coord.battery_region == "CA"
    assert coord.battery_locale == "en-US"
    assert coord.battery_timezone == "America/Los_Angeles"
    assert coord.battery_feature_details == {"HEMS_EV_Custom_Schedule": True}
    assert coord.battery_user_is_owner is True
    assert coord.battery_user_is_installer is False
    assert coord.battery_site_status_code == "normal"
    assert coord.battery_profile_option_keys == [
        "self-consumption",
        "cost_savings",
        "ai_optimisation",
    ]


@pytest.mark.asyncio
async def test_refresh_battery_site_settings_parses_ai_optimisation_flag(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_site_settings = AsyncMock(
        return_value={
            "data": {
                "showChargeFromGrid": True,
                "showSavingsMode": False,
                "showAiOptiSavingsMode": True,
                "isEmea": False,
                "showFullBackup": True,
                "showBatteryBackupPercentage": True,
                "hasEncharge": True,
                "hasEnpower": True,
                "userDetails": {"isOwner": True, "isInstaller": False},
            }
        }
    )

    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001

    assert coord._battery_show_ai_optimisation_mode is True  # noqa: SLF001
    assert coord.battery_is_emea is False
    assert coord.battery_profile_option_keys == [
        "self-consumption",
        "ai_optimisation",
        "backup_only",
    ]
    assert coord.battery_profile_option_labels["ai_optimisation"] == "AI Optimisation"


@pytest.mark.asyncio
async def test_set_system_profile_uses_remembered_reserve(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_ai_opti_savings_mode = True  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_profile_reserve_memory["cost_savings"] = 35  # noqa: SLF001
    coord._battery_profile_reserve_memory["ai_optimisation"] = 12  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})

    await coord.battery_runtime.async_set_system_profile("cost_savings")

    coord.client.set_battery_profile.assert_awaited_once()
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["profile"] == "cost_savings"
    assert kwargs["battery_backup_percentage"] == 35
    assert kwargs["devices"] is None
    assert coord.battery_pending_profile == "cost_savings"
    assert coord.battery_pending_backup_percentage == 35
    assert coord._battery_pending_require_exact_settings is False  # noqa: SLF001

    # Unknown regional profile should remain selectable as passthrough.
    coord.client.set_battery_profile.reset_mock()
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_profile = "regional_special"  # noqa: SLF001
    await coord.battery_runtime.async_set_system_profile("regional_special")
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["profile"] == "regional_special"
    assert kwargs["battery_backup_percentage"] == 20

    coord.client.set_battery_profile.reset_mock()
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    await coord.battery_runtime.async_set_system_profile("ai_optimisation")
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["profile"] == "ai_optimisation"
    assert kwargs["battery_backup_percentage"] == 12
    assert kwargs["operation_mode_sub_type"] == "prioritize-energy"


@pytest.mark.asyncio
async def test_savings_subtype_payload_on_and_off(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_backup_percentage = 21  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})

    await coord.async_set_savings_use_battery_after_peak(True)
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["operation_mode_sub_type"] == "prioritize-energy"
    assert coord._battery_pending_require_exact_settings is True  # noqa: SLF001

    coord.client.set_battery_profile.reset_mock()
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    await coord.async_set_savings_use_battery_after_peak(False)
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["operation_mode_sub_type"] is None


@pytest.mark.asyncio
async def test_cancel_pending_profile_change(
    coordinator_factory, mock_issue_registry
) -> None:
    from custom_components.enphase_ev.const import (
        DOMAIN,
        ISSUE_BATTERY_PROFILE_PENDING,
    )

    coord = coordinator_factory()
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client.cancel_battery_profile_update = AsyncMock(
        return_value={"message": "success"}
    )

    await coord.async_cancel_pending_profile_change()

    coord.client.cancel_battery_profile_update.assert_awaited_once()
    assert coord.battery_profile_pending is False

    # Idempotent: no pending => no backend call, pending issue is cleared.
    coord.client.cancel_battery_profile_update.reset_mock()
    coord._battery_profile_issue_reported = True  # noqa: SLF001
    await coord.async_cancel_pending_profile_change()
    coord.client.cancel_battery_profile_update.assert_not_called()
    assert (DOMAIN, ISSUE_BATTERY_PROFILE_PENDING) in mock_issue_registry.deleted


@pytest.mark.asyncio
async def test_cancel_pending_profile_change_ignores_already_processed_conflict(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client.cancel_battery_profile_update = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=409,
            message='{"error":{"status":"ALREADY_PROCESSED","message":"Changes already processed."}}',
        )
    )

    await coord.async_cancel_pending_profile_change()

    coord.client.cancel_battery_profile_update.assert_awaited_once()
    assert coord.battery_profile_pending is False


@pytest.mark.asyncio
async def test_cancel_pending_profile_change_reraises_non_benign_conflict(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client.cancel_battery_profile_update = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=409,
            message='{"error":{"status":"NOT_ALLOWED","message":"Denied."}}',
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord.async_cancel_pending_profile_change()


def test_pending_profile_timeout_issue_lifecycle(
    coordinator_factory, mock_issue_registry
) -> None:
    from custom_components.enphase_ev.const import (
        BATTERY_PROFILE_PENDING_TIMEOUT_S,
        DOMAIN,
        ISSUE_BATTERY_PROFILE_PENDING,
    )

    coord = coordinator_factory()
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(
        timezone.utc
    ) - timedelta(  # noqa: SLF001
        seconds=BATTERY_PROFILE_PENDING_TIMEOUT_S + 30
    )
    coord._hems_auth_failure_count = 25  # noqa: SLF001
    coord._hems_auth_last_endpoint = "hems_consumption_lifetime"  # noqa: SLF001
    coord._hems_auth_last_status = 200  # noqa: SLF001
    coord._hems_auth_last_reason = "login_wall"  # noqa: SLF001
    coord._hems_auth_backoff_until = time.monotonic() + 3600  # noqa: SLF001
    coord._hems_auth_backoff_ends_utc = datetime.now(
        timezone.utc
    ) + timedelta(  # noqa: SLF001
        hours=1
    )
    coord._last_error = "hems_auth_degraded"  # noqa: SLF001

    coord._sync_battery_profile_pending_issue()  # noqa: SLF001

    assert mock_issue_registry.created
    domain, issue_id, payload = mock_issue_registry.created[-1]
    assert domain == DOMAIN
    assert issue_id == ISSUE_BATTERY_PROFILE_PENDING
    placeholders = payload["translation_placeholders"]
    assert placeholders["pending_timeout_minutes"] == "15"
    assert not {key for key in placeholders if key.startswith("hems_")}
    assert {"failure_count", "reason"}.isdisjoint(placeholders)
    metrics = payload["data"]["site_metrics"]
    assert not {key for key in metrics if key.startswith("hems_auth_")}
    assert metrics.get("last_error") != "hems_auth_degraded"
    assert "hems_auth" not in metrics["degraded_services"]

    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._sync_battery_profile_pending_issue()  # noqa: SLF001
    assert (DOMAIN, ISSUE_BATTERY_PROFILE_PENDING) in mock_issue_registry.deleted


@pytest.mark.asyncio
async def test_battery_profile_write_lock_blocks_parallel_updates(
    coordinator_factory,
) -> None:
    import asyncio

    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    await coord._battery_profile_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord._async_apply_battery_profile(  # noqa: SLF001
                profile="self-consumption",
                reserve=20,
            )
    finally:
        coord._battery_profile_write_lock.release()  # noqa: SLF001

    # Concurrent writes: second caller should be rejected while first is in flight.
    gate = asyncio.Event()
    coord._battery_profile_last_write_mono = None  # noqa: SLF001

    async def _slow_set(**_kwargs):
        await gate.wait()
        return {"message": "success"}

    coord.client.set_battery_profile = AsyncMock(side_effect=_slow_set)
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    task1 = asyncio.create_task(
        coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=20,
        )
    )
    await asyncio.sleep(0)
    with pytest.raises(ServiceValidationError, match="already in progress|too quickly"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="cost_savings",
            reserve=30,
        )
    gate.set()
    await task1
    assert coord.client.set_battery_profile.await_count == 1


@pytest.mark.asyncio
async def test_battery_profile_write_debounce_applies_to_set_and_cancel(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.client.cancel_battery_profile_update = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord._battery_profile_last_write_mono = time.monotonic()  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="too quickly"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=20,
        )

    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="too quickly"):
        await coord.async_cancel_pending_profile_change()

    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    await coord.async_cancel_pending_profile_change()
    coord.client.cancel_battery_profile_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_site_only_update_refreshes_battery_profile_and_settings(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord.site_only = True
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.energy._async_refresh_site_energy = AsyncMock()  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(
        return_value={
            "data": {
                "showChargeFromGrid": True,
                "showSavingsMode": True,
                "showAiOptiSavingsMode": True,
                "showFullBackup": True,
                "showBatteryBackupPercentage": True,
                "hasEncharge": True,
            }
        }
    )
    coord.client.storm_guard_profile = AsyncMock(
        return_value=_profile_payload(profile="self-consumption", reserve=20)
    )
    coord.client.storm_guard_alert = AsyncMock(
        return_value={"criticalAlertActive": False, "stormAlerts": []}
    )

    result = await coord._async_update_data()  # noqa: SLF001

    assert result == {}
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_profile_option_keys == [
        "self-consumption",
        "cost_savings",
        "ai_optimisation",
        "backup_only",
    ]
    # Stale caches should keep site-only updates stable without extra fetches.
    coord._battery_site_settings_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord._storm_guard_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord._storm_alert_cache_until = time.monotonic() + 300  # noqa: SLF001
    coord.client.battery_site_settings.reset_mock()
    coord.client.storm_guard_profile.reset_mock()
    coord.client.storm_guard_alert.reset_mock()
    result_cached = await coord._async_update_data()  # noqa: SLF001
    assert result_cached == {}
    coord.client.battery_site_settings.assert_not_called()
    coord.client.storm_guard_profile.assert_not_called()
    coord.client.storm_guard_alert.assert_not_called()
    assert coord.battery_controls_available is True


@pytest.mark.asyncio
async def test_set_battery_reserve_rejects_full_backup(coordinator_factory) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_backup_percentage = 100  # noqa: SLF001

    with pytest.raises(ServiceValidationError, match="fixed at 100%"):
        await coord.async_set_battery_reserve(50)


@pytest.mark.asyncio
async def test_battery_profile_write_blocked_for_read_only_user(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )

    assert coord.battery_reserve_editable is False
    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord.async_set_battery_reserve(30)
    coord.client.set_battery_settings_compat.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_reserve_write_succeeds_when_role_unknown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord.async_set_battery_reserve(30)

    coord.client.set_battery_settings_compat.assert_awaited_once_with(
        {"batteryBackupPercentage": 30},
        merged_payload=True,
        strip_devices=True,
    )


@pytest.mark.asyncio
async def test_battery_profile_forbidden_translates_to_validation_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await coord.async_set_battery_reserve(30)


@pytest.mark.asyncio
async def test_battery_profile_forbidden_read_only_user_translates_to_permission_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=30,
        )


@pytest.mark.asyncio
async def test_battery_profile_forbidden_after_permission_change_returns_permission_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001

    async def _forbidden_after_role_change(**_kwargs):
        coord._battery_user_is_owner = False  # noqa: SLF001
        coord._battery_user_is_installer = False  # noqa: SLF001
        raise aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )

    coord.client.set_battery_profile = AsyncMock(
        side_effect=_forbidden_after_role_change
    )

    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=30,
        )


@pytest.mark.asyncio
async def test_battery_reserve_write_calls_client_set_battery_settings_compat(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "evse-1", "enable": False, "chargeMode": "MANUAL"}
    ]
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )

    await coord.async_set_battery_reserve(30)

    coord.client.set_battery_settings_compat.assert_awaited_once_with(
        {"batteryBackupPercentage": 30},
        merged_payload=True,
        strip_devices=True,
    )


@pytest.mark.asyncio
async def test_battery_profile_write_forbidden_translates_from_client_call(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await coord.async_set_battery_reserve(30)


@pytest.mark.asyncio
async def test_battery_profile_write_read_only_user_translates_permission_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord.async_set_battery_reserve(30)


@pytest.mark.asyncio
async def test_battery_reserve_write_direct_helper_rejects_missing_profile(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()

    with pytest.raises(ServiceValidationError, match="Battery profile is unavailable"):
        await coord.battery_runtime.async_apply_battery_reserve_only(
            profile="",
            reserve=30,
        )


@pytest.mark.asyncio
async def test_battery_reserve_write_direct_helper_forbidden_read_only_user(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord.battery_runtime.async_apply_battery_reserve_only(
            profile="self-consumption",
            reserve=30,
        )


@pytest.mark.asyncio
async def test_battery_reserve_write_direct_helper_forbidden_after_permission_change(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001

    async def _forbidden_after_role_change(*_args, **_kwargs):
        coord._battery_user_is_owner = False  # noqa: SLF001
        coord._battery_user_is_installer = False  # noqa: SLF001
        raise aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )

    coord.client.set_battery_settings_compat = AsyncMock(
        side_effect=_forbidden_after_role_change
    )

    with pytest.raises(ServiceValidationError, match="not permitted"):
        await coord.battery_runtime.async_apply_battery_reserve_only(
            profile="self-consumption",
            reserve=30,
        )


@pytest.mark.asyncio
async def test_battery_reserve_write_direct_helper_translates_auth_and_reraises(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_settings_compat = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord.battery_runtime.async_apply_battery_reserve_only(
            profile="self-consumption",
            reserve=30,
        )

    coord.client.set_battery_settings_compat = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_settings_last_write_mono = time.monotonic() - 10  # noqa: SLF001

    with pytest.raises(aiohttp.ClientResponseError) as err:
        await coord.battery_runtime.async_apply_battery_reserve_only(
            profile="self-consumption",
            reserve=30,
        )
    assert err.value.status == 500
    assert err.value.message == "boom"


@pytest.mark.asyncio
async def test_battery_reserve_write_direct_helper_refreshes_unknown_write_access(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(
        return_value={"data": {"userDetails": {"isOwner": True, "isInstaller": False}}}
    )
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord.battery_runtime.async_apply_battery_reserve_only(
        profile="self-consumption",
        reserve=30,
    )

    coord.client.battery_site_settings.assert_awaited_once()
    coord.client.set_battery_settings_compat.assert_awaited_once_with(
        {"batteryBackupPercentage": 30},
        merged_payload=True,
        strip_devices=True,
    )
    assert coord.battery_user_is_owner is True
    assert coord.battery_write_access_confirmed is True


@pytest.mark.asyncio
async def test_battery_reserve_write_direct_helper_rejects_settings_debounce(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_settings_last_write_mono = time.monotonic()  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})

    with pytest.raises(ServiceValidationError, match="too quickly"):
        await coord.battery_runtime.async_apply_battery_reserve_only(
            profile="self-consumption",
            reserve=30,
        )

    coord.client.set_battery_settings_compat.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_reserve_write_direct_helper_rejects_settings_lock(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_settings_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})

    await coord._battery_settings_write_lock.acquire()  # noqa: SLF001
    try:
        with pytest.raises(ServiceValidationError, match="already in progress"):
            await coord.battery_runtime.async_apply_battery_reserve_only(
                profile="self-consumption",
                reserve=30,
            )
    finally:
        coord._battery_settings_write_lock.release()  # noqa: SLF001

    coord.client.set_battery_settings_compat.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_reserve_write_direct_helper_holds_settings_lock_during_write(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_settings_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    async def _fake_write(*args, **kwargs):
        assert coord._battery_profile_write_lock.locked()  # noqa: SLF001
        assert coord._battery_settings_write_lock.locked()  # noqa: SLF001
        return {}

    coord.client.set_battery_settings_compat = AsyncMock(side_effect=_fake_write)

    await coord.battery_runtime.async_apply_battery_reserve_only(
        profile="self-consumption",
        reserve=30,
    )

    coord.client.set_battery_settings_compat.assert_awaited_once()


@pytest.mark.asyncio
async def test_ai_profile_write_defaults_required_subtype(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord._async_apply_battery_profile(  # noqa: SLF001
        profile="ai_optimisation",
        reserve=10,
        sub_type=None,
    )

    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["profile"] == "ai_optimisation"
    assert kwargs["operation_mode_sub_type"] == "prioritize-energy"


@pytest.mark.asyncio
async def test_battery_profile_unauthorized_translates_to_reauth_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )

    with pytest.raises(ServiceValidationError, match="Reauthenticate"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=30,
        )


@pytest.mark.asyncio
async def test_battery_profile_forbidden_translates_to_http_403_error(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )

    with pytest.raises(ServiceValidationError, match="HTTP 403 Forbidden"):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=30,
        )


@pytest.mark.asyncio
async def test_battery_profile_unexpected_http_error_reraises(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(
        side_effect=aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=500,
            message="boom",
        )
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await coord._async_apply_battery_profile(  # noqa: SLF001
            profile="self-consumption",
            reserve=30,
        )


def test_battery_profile_property_helpers_cover_branches(coordinator_factory) -> None:
    coord = coordinator_factory()

    class BadStr:
        def __str__(self):
            raise ValueError("boom")

    assert coord._normalize_battery_profile_key(BadStr()) is None  # noqa: SLF001
    assert coord._battery_profile_label(BadStr()) is None  # noqa: SLF001
    assert coord._normalize_battery_sub_type(BadStr()) is None  # noqa: SLF001
    assert coord._coerce_optional_int(BadStr()) is None  # noqa: SLF001

    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._battery_backup_percentage = 33  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_reserve = 22  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_ai_opti_savings_mode = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001

    assert coord.battery_pending_requested_at is not None
    assert coord.battery_effective_backup_percentage == 33
    assert coord.battery_effective_operation_mode_sub_type == "prioritize-energy"
    assert coord.battery_pending_operation_mode_sub_type == "prioritize-energy"
    coord.battery_runtime.set_battery_optimistic_profile(
        "ai_optimisation",
        22,
        "prioritize-energy",
    )
    assert coord.battery_effective_operation_mode_sub_type == "prioritize-energy"
    coord.battery_runtime.clear_battery_optimistic_profile()
    assert coord.battery_has_encharge is True
    assert coord.battery_show_battery_backup_percentage is True
    assert "cost_savings" in coord.battery_profile_option_keys
    assert "ai_optimisation" in coord.battery_profile_option_keys
    assert coord.battery_profile_display == "Savings"
    assert coord.battery_effective_profile_display == "Self-Consumption"
    coord._battery_live_profile = "backup_only"  # noqa: SLF001
    assert coord.battery_live_profile == "backup_only"
    assert coord.battery_effective_profile == "self-consumption"
    assert coord.battery_selected_profile == "cost_savings"
    assert "backup_only" in coord.battery_profile_option_keys
    coord._battery_pending_profile = None  # noqa: SLF001
    assert coord.battery_selected_profile == "self-consumption"
    assert coord.battery_effective_profile_display == "Self-Consumption"
    coord._battery_live_profile = None  # noqa: SLF001
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    assert coord.savings_use_battery_after_peak is True

    coord._battery_pending_profile = "ai_optimisation"  # noqa: SLF001
    assert coord.battery_selected_operation_mode_sub_type == "prioritize-energy"
    assert coord.savings_use_battery_after_peak is None

    coord._battery_has_encharge = False  # noqa: SLF001
    assert coord.battery_controls_available is False
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    assert coord.savings_use_battery_after_peak is None
    coord._battery_show_savings_mode = False  # noqa: SLF001
    assert coord.savings_use_battery_switch_available is False
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = False  # noqa: SLF001
    assert coord.battery_reserve_editable is False
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_pending_profile = None  # noqa: SLF001
    coord._battery_profile = None  # noqa: SLF001
    assert coord.battery_reserve_editable is False
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_show_savings_mode = True  # noqa: SLF001
    coord._battery_user_is_owner = False  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    assert coord.savings_use_battery_switch_available is False


def test_battery_reserve_editable_uses_rbd_control_when_present(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=True, locked=False
    )  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = False  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_non_emea_ignores_rbd_control_false(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    assert coord.battery_reserve_editable is True


def test_battery_reserve_editable_prefers_reserve_flag_over_cfg_control(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_cfg_control_show = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is True


def test_battery_reserve_editable_emea_prefers_cfg_control(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = True  # noqa: SLF001
    coord._battery_cfg_control_show = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_emea_uses_rbd_when_cfg_control_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = True  # noqa: SLF001
    coord._battery_cfg_control_show = None  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = True  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_non_emea_uses_rbd_when_reserve_flag_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = None  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_non_emea_uses_cfg_when_other_flags_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = None  # noqa: SLF001
    coord._battery_cfg_control_show = False  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=None, locked=False
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_non_emea_honors_rbd_when_reserve_not_true(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = False  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_non_emea_rejects_invalid_reserve_flag_value(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_is_emea = False  # noqa: SLF001
    coord._battery_show_battery_backup_percentage = "unexpected"  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=False, locked=False
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_reserve_editable_honors_rbd_control_locked(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_rbd_control = BatteryControlCapability(
        show=True, locked=True
    )  # noqa: SLF001

    assert coord.battery_reserve_editable is False


def test_battery_profile_selection_available_false_when_system_task_active(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_system_task = True  # noqa: SLF001

    assert coord.battery_profile_selection_available is False


@pytest.mark.asyncio
async def test_battery_profile_write_blocked_when_system_task_active(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord._battery_system_task = True  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock()

    with pytest.raises(
        ServiceValidationError, match="Battery profile updates are unavailable"
    ):
        await coord.battery_runtime.async_apply_battery_profile(
            profile="self-consumption",
            reserve=20,
        )
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_profile_write_blocked_when_refresh_discovers_system_task(
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
    coord.client.set_battery_profile = AsyncMock()

    with pytest.raises(
        ServiceValidationError, match="Battery profile updates are unavailable"
    ):
        await coord.battery_runtime.async_apply_battery_profile(
            profile="self-consumption",
            reserve=20,
        )
    coord.client.set_battery_profile.assert_not_awaited()


def test_battery_write_access_confirmed_requires_explicit_role(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = None  # noqa: SLF001
    assert coord.battery_write_access_confirmed is False

    coord._battery_user_is_owner = True  # noqa: SLF001
    assert coord.battery_write_access_confirmed is True

    coord._battery_user_is_owner = None  # noqa: SLF001
    coord._battery_user_is_installer = True  # noqa: SLF001
    assert coord.battery_write_access_confirmed is True


def test_charge_from_grid_control_unavailable_when_system_task_active(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_system_task = True  # noqa: SLF001

    assert coord.charge_from_grid_control_available is False


def test_charge_from_grid_schedule_supported_honors_schedule_supported_false(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        locked=False,
        show_day_schedule=True,
        schedule_supported=False,
        force_schedule_supported=True,
    )
    coord._battery_charge_begin_time = 120  # noqa: SLF001
    coord._battery_charge_end_time = 300  # noqa: SLF001

    assert coord.charge_from_grid_schedule_supported is False


def test_battery_cfg_control_enabled_falls_back_to_legacy_scalar(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_cfg_control = BatteryControlCapability(  # noqa: SLF001
        show=True,
        enabled=None,
    )
    coord._battery_cfg_control_enabled = False  # noqa: SLF001

    assert coord.battery_cfg_control_enabled is False


def test_charge_from_grid_force_schedule_supported_falls_back_to_schedule_id(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_charge_from_grid = True  # noqa: SLF001
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_cfg_schedule_id = "cfg-1"  # noqa: SLF001

    assert coord.charge_from_grid_force_schedule_supported is True


def test_battery_pending_age_handles_datetime_failures(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import coordinator as coord_mod

    coord = coordinator_factory()
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001

    monkeypatch.setattr(
        coord_mod.dt_util,
        "utcnow",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert coord.battery_pending_age_seconds is None


def test_additional_battery_and_storm_property_helpers(coordinator_factory) -> None:
    coord = coordinator_factory()

    coord._battery_feature_details = "bad"  # noqa: SLF001
    assert coord.battery_feature_details == {}

    coord._battery_site_status_text = "Normal"  # noqa: SLF001
    coord._battery_site_status_severity = "warning"  # noqa: SLF001
    assert coord.battery_site_status_text == "Normal"
    assert coord.battery_site_status_severity == "warning"

    coord._storm_alerts = "bad"  # noqa: SLF001
    assert coord.storm_alerts == []
    assert coord.battery_profile_polling_interval is None
    assert coord.battery_profile_evse_device is None

    coord._battery_profile_evse_device = {"uuid": "evse-1"}  # noqa: SLF001
    snapshot = coord.battery_profile_evse_device
    assert snapshot == {"uuid": "evse-1"}
    snapshot["uuid"] = "mutated"
    assert coord._battery_profile_evse_device["uuid"] == "evse-1"  # noqa: SLF001


def test_battery_pending_match_and_memory_branches(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is False  # noqa: SLF001

    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_backup_percentage = 25  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is False  # noqa: SLF001

    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "other"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is False  # noqa: SLF001

    coord._battery_pending_sub_type = None  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is False  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "custom-backend-default"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is True  # noqa: SLF001

    coord._battery_pending_sub_type = "regional-saving"  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "another-regional-saving"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is False  # noqa: SLF001

    coord._battery_pending_profile = "ai_optimisation"  # noqa: SLF001
    coord._battery_profile = "ai_optimisation"  # noqa: SLF001
    coord._battery_pending_reserve = 18  # noqa: SLF001
    coord._battery_backup_percentage = 18  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is True  # noqa: SLF001

    coord._remember_battery_reserve(None, 20)  # noqa: SLF001

    class BadProfile:
        def __str__(self):
            raise ValueError("boom")

    coord._remember_battery_reserve(BadProfile(), 20)  # noqa: SLF001
    coord._remember_battery_reserve("regional_special", 20)  # noqa: SLF001


def test_live_battery_mode_profile_normalization(coordinator_factory) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert runtime.normalize_live_battery_mode_profile(BadStr()) is None
    assert runtime.normalize_live_battery_mode_profile("   ") is None
    assert runtime.normalize_live_battery_mode_profile("Full Backup") == "backup_only"
    assert (
        runtime.normalize_live_battery_mode_profile("Self-Consumption")
        == "self-consumption"
    )
    assert runtime.normalize_live_battery_mode_profile("backup_only") == "backup_only"
    assert runtime.normalize_live_battery_mode_profile("not-a-mode") == "not-a-mode"
    assert runtime.normalize_live_battery_mode_profile(None) is None

    coord._battery_profile = "ai_optimisation"  # noqa: SLF001
    assert runtime.normalize_live_battery_mode_profile("Savings") == "ai_optimisation"
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    assert runtime.normalize_live_battery_mode_profile("Savings") == "cost_savings"


def test_battery_status_payload_tracks_live_profile_without_clearing_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_profile = "backup_only"  # noqa: SLF001
    coord._battery_pending_reserve = 100  # noqa: SLF001
    coord._battery_pending_require_exact_settings = False  # noqa: SLF001
    coord._battery_pending_authoritative_confirmation_required = True  # noqa: SLF001

    coord.battery_runtime.parse_battery_status_payload(
        {
            "storages": [
                {
                    "id": "123",
                    "battery_mode": "Full Backup",
                    "current_charge": "75%",
                    "available_energy": "7.5",
                    "max_capacity": "10",
                    "status": "normal",
                }
            ]
        }
    )

    assert coord._battery_live_profile == "backup_only"  # noqa: SLF001
    assert coord._battery_live_profile_label == "Full Backup"  # noqa: SLF001
    assert coord._battery_live_profile_sample_utc is not None  # noqa: SLF001
    assert coord.battery_profile_pending is True
    assert coord.battery_selected_profile == "backup_only"

    coord.battery_runtime.parse_battery_status_payload({"storages": [{"id": "123"}]})
    assert coord._battery_live_profile is None  # noqa: SLF001
    assert coord._battery_live_profile_label is None  # noqa: SLF001
    assert coord._battery_live_profile_sample_utc is None  # noqa: SLF001

    coord._battery_live_profile = "backup_only"  # noqa: SLF001
    coord._battery_live_profile_label = "Full Backup"  # noqa: SLF001
    coord._battery_live_profile_sample_utc = datetime.now(timezone.utc)  # noqa: SLF001
    coord.battery_runtime.parse_battery_status_payload(None)
    assert coord._battery_live_profile is None  # noqa: SLF001
    assert coord._battery_live_profile_label is None  # noqa: SLF001
    assert coord._battery_live_profile_sample_utc is None  # noqa: SLF001


def test_battery_status_payload_clears_pending_when_no_profile_endpoint_value(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "backup_only"  # noqa: SLF001
    coord._battery_pending_reserve = 100  # noqa: SLF001
    coord._battery_pending_require_exact_settings = False  # noqa: SLF001

    coord.battery_runtime.parse_battery_status_payload(
        {
            "storages": [
                {
                    "id": "123",
                    "battery_mode": "Full Backup",
                    "current_charge": "75%",
                }
            ]
        }
    )

    assert coord.battery_profile_pending is False
    assert coord.battery_profile == "backup_only"
    assert coord.battery_effective_profile == "backup_only"


def test_either_configured_or_live_profile_confirms_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_live_profile = "backup_only"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_require_exact_settings = False  # noqa: SLF001

    assert coord._effective_profile_matches_pending() is True  # noqa: SLF001

    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_live_profile = "self-consumption"  # noqa: SLF001
    assert coord._effective_profile_matches_pending() is True  # noqa: SLF001


def test_configured_profile_wins_over_stale_live_profile(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_live_profile = "self-consumption"  # noqa: SLF001

    assert coord.battery_effective_profile == "backup_only"
    assert coord.battery_selected_profile == "backup_only"
    assert coord.battery_effective_profile_display == "Full Backup"
    assert "self-consumption" in coord.battery_profile_option_keys


def test_live_profile_confirmation_promotes_pending_profile(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_backup_percentage = 100  # noqa: SLF001
    coord._battery_live_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_require_exact_settings = False  # noqa: SLF001

    coord.battery_runtime.confirm_battery_pending()

    assert coord.battery_profile_pending is False
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_effective_profile == "self-consumption"
    assert coord.battery_effective_backup_percentage == 20
    assert coord.battery_selected_profile == "self-consumption"
    assert coord.battery_selected_backup_percentage == 20


@pytest.mark.asyncio
async def test_cancel_pending_profile_change_does_not_promote_live_match(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_backup_percentage = 100  # noqa: SLF001
    coord._battery_live_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord.client.cancel_battery_profile_update = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    await coord.async_cancel_pending_profile_change()

    coord.client.cancel_battery_profile_update.assert_awaited_once()
    assert coord.battery_profile_pending is False
    assert coord.battery_profile == "backup_only"
    assert coord.battery_effective_backup_percentage == 100


def test_collect_site_metrics_includes_live_battery_profile(
    coordinator_factory,
) -> None:
    sample_utc = datetime(2026, 4, 25, tzinfo=timezone.utc)
    coord = coordinator_factory()
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_live_profile = "backup_only"  # noqa: SLF001
    coord._battery_live_profile_label = "Full Backup"  # noqa: SLF001
    coord._battery_live_profile_sample_utc = sample_utc  # noqa: SLF001

    metrics = coord.collect_site_metrics()

    assert metrics["battery_profile"] == "self-consumption"
    assert metrics["battery_live_profile"] == "backup_only"
    assert metrics["battery_live_profile_label"] == "Full Backup"
    assert metrics["battery_live_profile_raw"] == "Full Backup"
    assert metrics["battery_live_profile_sample_utc"] == sample_utc.isoformat()


@pytest.mark.asyncio
async def test_split_profile_recovery_nudges_reserve_then_restores(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import battery_runtime as battery_runtime_module

    monkeypatch.setattr(
        battery_runtime_module, "_PROFILE_RECOVERY_RESERVE_RESTORE_DELAY_S", 0
    )
    monkeypatch.setattr(battery_runtime_module, "BATTERY_PROFILE_WRITE_DEBOUNCE_S", 0)
    monkeypatch.setattr(battery_runtime_module, "BATTERY_SETTINGS_WRITE_DEBOUNCE_S", 0)
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_live_profile = "backup_only"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_backup_percentage_min = 5  # noqa: SLF001
    coord._battery_backup_percentage_max = 100  # noqa: SLF001
    coord._battery_show_full_backup = True  # noqa: SLF001
    coord.client.set_battery_settings_compat = AsyncMock(
        return_value={"message": "success"}
    )
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    recovered = await coord.battery_runtime.async_recover_split_battery_profile(
        profile="self-consumption",
        reserve=20,
    )
    task = coord._battery_profile_recovery_restore_task  # noqa: SLF001
    assert task is not None
    await task

    assert recovered is True
    assert coord.client.set_battery_settings_compat.await_count == 2
    first_call, second_call = coord.client.set_battery_settings_compat.await_args_list
    assert first_call.args[0] == {"batteryBackupPercentage": 25}
    assert first_call.kwargs == {"merged_payload": True, "strip_devices": True}
    assert second_call.args[0] == {"batteryBackupPercentage": 20}
    assert second_call.kwargs == {"merged_payload": True, "strip_devices": True}
    assert coord._battery_profile_recovery_restore_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_reserve_only_and_recovery_edge_branches(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import battery_runtime as battery_runtime_module
    from homeassistant.exceptions import ServiceValidationError

    monkeypatch.setattr(
        battery_runtime_module, "_PROFILE_RECOVERY_RESERVE_RESTORE_DELAY_S", 60
    )
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    with pytest.raises(ServiceValidationError, match="unavailable"):
        await runtime.async_apply_battery_reserve_only(profile="", reserve=20)

    assert (
        runtime._profile_recovery_reserve_nudge("backup_only", 100) is None
    )  # noqa: SLF001
    coord._battery_backup_percentage_min = 5  # noqa: SLF001
    coord._battery_backup_percentage_max = 100  # noqa: SLF001
    assert (
        runtime._profile_recovery_reserve_nudge("self-consumption", 100) == 95
    )  # noqa: SLF001
    coord._battery_backup_percentage_min = 20  # noqa: SLF001
    coord._battery_backup_percentage_max = 20  # noqa: SLF001
    assert (
        runtime._profile_recovery_reserve_nudge("self-consumption", 20) is None
    )  # noqa: SLF001

    coord.hass = object()
    runtime._schedule_profile_recovery_reserve_restore(
        "self-consumption", 20
    )  # noqa: SLF001
    first_task = coord._battery_profile_recovery_restore_task  # noqa: SLF001
    runtime._schedule_profile_recovery_reserve_restore(
        "self-consumption", 20
    )  # noqa: SLF001
    second_task = coord._battery_profile_recovery_restore_task  # noqa: SLF001
    await asyncio.sleep(0)
    assert first_task.cancelled() or first_task.done()
    assert second_task is not first_task
    second_task.cancel()
    with suppress(asyncio.CancelledError):
        await second_task


@pytest.mark.asyncio
async def test_profile_recovery_restore_logs_and_clears_failed_restore(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev import battery_runtime as battery_runtime_module

    monkeypatch.setattr(
        battery_runtime_module, "_PROFILE_RECOVERY_RESERVE_RESTORE_DELAY_S", 0
    )
    coord = coordinator_factory()
    coord.hass = object()
    coord.battery_runtime.async_apply_battery_reserve_only = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    coord.battery_runtime._schedule_profile_recovery_reserve_restore(  # noqa: SLF001
        "self-consumption",
        20,
    )
    task = coord._battery_profile_recovery_restore_task  # noqa: SLF001
    await task

    assert coord._battery_profile_recovery_restore_task is None  # noqa: SLF001
    coord.battery_runtime.async_apply_battery_reserve_only.assert_awaited_once_with(
        profile="self-consumption",
        reserve=20,
        require_exact_pending_match=False,
    )


@pytest.mark.asyncio
async def test_split_profile_recovery_skips_when_not_applicable(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})

    assert (
        await coord.battery_runtime.async_recover_split_battery_profile(
            profile="",
            reserve=20,
        )
        is False
    )

    assert (
        await coord.battery_runtime.async_recover_split_battery_profile(
            profile="self-consumption",
            reserve=20,
        )
        is False
    )

    coord._battery_live_profile = "self-consumption"  # noqa: SLF001
    assert (
        await coord.battery_runtime.async_recover_split_battery_profile(
            profile="self-consumption",
            reserve=20,
        )
        is False
    )

    coord._battery_live_profile = "backup_only"  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    assert (
        await coord.battery_runtime.async_recover_split_battery_profile(
            profile="self-consumption",
            reserve=20,
        )
        is False
    )

    coord._battery_profile = "backup_only"  # noqa: SLF001
    coord._battery_live_profile = "self-consumption"  # noqa: SLF001
    assert (
        await coord.battery_runtime.async_recover_split_battery_profile(
            profile="backup_only",
            reserve=100,
        )
        is False
    )
    coord.client.set_battery_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_profile_setter_returns_after_split_recovery(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.battery_runtime.async_recover_split_battery_profile = AsyncMock(
        return_value=True
    )

    await coord.battery_runtime.async_set_system_profile("self-consumption")

    coord.battery_runtime.async_recover_split_battery_profile.assert_awaited_once()
    coord.client.set_battery_profile.assert_not_awaited()


def test_pending_profile_issue_noop_when_already_reported(
    coordinator_factory, mock_issue_registry
) -> None:
    from custom_components.enphase_ev.const import BATTERY_PROFILE_PENDING_TIMEOUT_S

    coord = coordinator_factory()
    mock_issue_registry.deleted.clear()
    coord._battery_profile_issue_reported = True  # noqa: SLF001
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(
        timezone.utc
    ) - timedelta(  # noqa: SLF001
        seconds=BATTERY_PROFILE_PENDING_TIMEOUT_S + 60
    )

    coord._sync_battery_profile_pending_issue()  # noqa: SLF001

    assert mock_issue_registry.created == []
    assert mock_issue_registry.deleted == []


@pytest.mark.asyncio
async def test_battery_profile_setter_validation_and_fallbacks(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.coordinator import ServiceValidationError

    coord = coordinator_factory()
    coord.client.set_battery_profile = AsyncMock(return_value={"message": "success"})
    coord.client.set_battery_settings_compat = AsyncMock(return_value={})
    coord.client.battery_site_settings = AsyncMock(return_value={"data": {}})
    coord.async_request_refresh = AsyncMock()
    coord.kick_fast = MagicMock()

    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord._async_apply_battery_profile(profile="", reserve=10)  # noqa: SLF001

    coord._battery_site_settings_cache_until = 10**12  # noqa: SLF001
    await coord._async_refresh_battery_site_settings()  # noqa: SLF001
    coord.client.battery_site_settings.assert_not_called()

    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.battery_runtime.async_set_system_profile("")

    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="not supported"):
        await coord.battery_runtime.async_set_system_profile("cost_savings")

    coord._battery_profile = None  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="unavailable"):
        await coord.async_set_battery_reserve(30)

    coord._battery_user_is_owner = True  # noqa: SLF001
    coord._battery_user_is_installer = False  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_backup_percentage = 25  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    await coord.async_set_battery_reserve(5)
    args = coord.client.set_battery_settings_compat.await_args.args
    kwargs = coord.client.set_battery_settings_compat.await_args.kwargs
    assert args[0] == {"batteryBackupPercentage": 5}
    assert kwargs == {"merged_payload": True, "strip_devices": True}
    assert coord._battery_pending_sub_type == "prioritize-energy"  # noqa: SLF001

    coord._clear_battery_pending()  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    with pytest.raises(ServiceValidationError, match="Savings profile must be active"):
        await coord.async_set_savings_use_battery_after_peak(False)

    coord._battery_profile = "ai_optimisation"  # noqa: SLF001
    coord._battery_backup_percentage = 10  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord._battery_settings_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord.client.set_battery_settings_compat.reset_mock()
    await coord.async_set_battery_reserve(5)
    args = coord.client.set_battery_settings_compat.await_args.args
    kwargs = coord.client.set_battery_settings_compat.await_args.kwargs
    assert args[0] == {"batteryBackupPercentage": 5}
    assert kwargs == {"merged_payload": True, "strip_devices": True}
    assert coord._battery_pending_sub_type == "prioritize-energy"  # noqa: SLF001

    coord._clear_battery_pending()  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_backup_percentage = None  # noqa: SLF001
    coord._battery_profile_reserve_memory.pop("cost_savings", None)  # noqa: SLF001
    coord._battery_profile_last_write_mono = time.monotonic() - 10  # noqa: SLF001
    coord.client.set_battery_profile.reset_mock()
    await coord.async_set_savings_use_battery_after_peak(True)
    kwargs = coord.client.set_battery_profile.await_args.kwargs
    assert kwargs["battery_backup_percentage"] == 20


def test_profile_only_pending_match_allows_reserve_drift(coordinator_factory) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_sub_type = None  # noqa: SLF001
    coord._battery_pending_require_exact_settings = False  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 45  # noqa: SLF001

    assert coord._effective_profile_matches_pending() is True  # noqa: SLF001


def test_profile_payload_keeps_recent_local_pending_when_backend_still_mismatched(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "backup_only"  # noqa: SLF001
    coord._battery_pending_reserve = 100  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(timezone.utc)  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001

    coord.battery_runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "isBatteryChangePending": False,
            }
        }
    )

    assert coord.battery_profile_pending is True
    assert coord._battery_backend_profile_update_pending is False  # noqa: SLF001
    assert coord._battery_backend_not_pending_observed_at is not None  # noqa: SLF001


def test_settings_payload_clears_stale_local_pending_when_backend_change_completed(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "backup_only"  # noqa: SLF001
    coord._battery_pending_reserve = 100  # noqa: SLF001
    now = datetime.now(timezone.utc)
    coord._battery_pending_requested_at = now - timedelta(
        seconds=FAST_TOGGLE_POLL_HOLD_S + 5
    )  # noqa: SLF001
    coord._battery_backend_not_pending_observed_at = now - timedelta(
        seconds=5
    )  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001

    coord.battery_runtime.parse_battery_settings_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "isBatteryChangePending": False,
            }
        }
    )

    assert coord.battery_profile_pending is False
    assert coord._battery_backend_profile_update_pending is None  # noqa: SLF001
    assert coord._battery_backend_not_pending_observed_at is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_payload_snapshots_are_saved_and_redacted(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_site_settings = AsyncMock(
        return_value={"data": {"showSavingsMode": True}, "userId": "123"}
    )
    coord.client.storm_guard_profile = AsyncMock(
        return_value={
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "stormGuardState": "enabled",
            },
            "token": "secret-token",
        }
    )

    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001
    await coord._async_refresh_storm_guard_profile(force=True)  # noqa: SLF001

    assert coord._battery_site_settings_payload is not None  # noqa: SLF001
    assert (
        coord._battery_site_settings_payload["userId"] == "[redacted]"
    )  # noqa: SLF001
    assert coord._battery_profile_payload is not None  # noqa: SLF001
    assert coord._battery_profile_payload["token"] == "[redacted]"  # noqa: SLF001
    nested = {
        "userId": "123",
        "device_link": "https://enlighten.example/systems/9990001/envoys/200001",
        "connection_details": {
            "interface_ip": {"ethernet": "192.0.2.10"},
        },
        "nested": {
            "Authorization": "Bearer abc",
            "X-XSRF-Token": "xsrf",
            "refresh-token": "refresh",
            "items": [
                {"cookie": "a=b"},
                {"username": "user@example.com"},
                {
                    "default_route": "192.168.1.1 (Ethernet)",
                    "mac_addr": "00:11:22:33:44:55",
                    "ip_addr": "192.0.2.10",
                    "gateway_ip_addr": "192.0.2.1",
                },
                {"safe": "ok"},
            ],
        },
    }
    redacted_nested = coord._redact_battery_payload(nested)  # noqa: SLF001
    assert redacted_nested["userId"] == "[redacted]"
    assert redacted_nested["nested"]["Authorization"] == "[redacted]"
    assert redacted_nested["nested"]["X-XSRF-Token"] == "[redacted]"
    assert redacted_nested["nested"]["refresh-token"] == "[redacted]"
    assert redacted_nested["device_link"] == "[redacted]"
    assert redacted_nested["connection_details"]["interface_ip"] == "[redacted]"
    assert redacted_nested["nested"]["items"][0]["cookie"] == "[redacted]"
    assert redacted_nested["nested"]["items"][1]["username"] == "[redacted]"
    assert redacted_nested["nested"]["items"][2]["default_route"] == "[redacted]"
    assert redacted_nested["nested"]["items"][2]["mac_addr"] == "[redacted]"
    assert redacted_nested["nested"]["items"][2]["ip_addr"] == "[redacted]"
    assert redacted_nested["nested"]["items"][2]["gateway_ip_addr"] == "[redacted]"
    assert redacted_nested["nested"]["items"][3]["safe"] == "ok"


@pytest.mark.asyncio
async def test_battery_payload_snapshots_wrap_non_dict_payloads(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_site_settings = AsyncMock(return_value=["unexpected"])
    coord.client.storm_guard_profile = AsyncMock(return_value=["unexpected"])

    await coord._async_refresh_battery_site_settings(force=True)  # noqa: SLF001
    await coord._async_refresh_storm_guard_profile(force=True)  # noqa: SLF001

    assert coord._battery_site_settings_payload == {
        "value": ["unexpected"]
    }  # noqa: SLF001
    assert coord._battery_profile_payload == {"value": ["unexpected"]}  # noqa: SLF001
