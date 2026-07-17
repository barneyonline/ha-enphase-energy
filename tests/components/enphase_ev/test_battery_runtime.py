from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.battery_runtime import BatteryRuntime
from custom_components.enphase_ev.const import (
    BATTERY_SETTINGS_CACHE_TTL,
    FAST_TOGGLE_POLL_HOLD_S,
    SAVINGS_OPERATION_MODE_SUBTYPE,
)


@pytest.fixture(autouse=True)
def _force_utc_timezone() -> None:
    dt_util.set_default_time_zone(UTC)


def test_battery_runtime_normalizes_labels() -> None:
    runtime = BatteryRuntime(SimpleNamespace())

    assert runtime.normalize_battery_profile_key(" Cost_Savings ") == "cost_savings"
    assert runtime.normalize_battery_profile_key("AI Optimization") == "ai_optimisation"
    assert runtime.normalize_battery_profile_key(None) is None
    assert runtime.battery_profile_label("ai_optimisation") == "AI Optimisation"
    assert runtime.battery_profile_label("backup_only") == "Full Backup"
    assert runtime.battery_profile_label("regional-profile") == "Regional Profile"
    assert runtime.battery_profile_label(None) is None


def test_battery_runtime_pending_helpers_and_matching(
    coordinator_factory, mock_issue_registry
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)

    runtime.set_battery_pending(
        profile="cost_savings",
        reserve=25,
        sub_type=SAVINGS_OPERATION_MODE_SUBTYPE,
        require_exact_settings=False,
    )

    assert coord._battery_pending_profile == "cost_savings"  # noqa: SLF001
    assert coord._battery_pending_reserve == 25  # noqa: SLF001
    assert (
        coord._battery_pending_sub_type == SAVINGS_OPERATION_MODE_SUBTYPE
    )  # noqa: SLF001
    assert coord._battery_pending_requested_at is not None  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    assert runtime.effective_profile_matches_pending() is True

    runtime.clear_battery_pending()

    assert coord._battery_pending_profile is None  # noqa: SLF001
    assert coord._battery_pending_requested_at is None  # noqa: SLF001
    assert coord._battery_backend_profile_update_pending is None  # noqa: SLF001
    assert coord._battery_backend_not_pending_observed_at is None  # noqa: SLF001
    assert mock_issue_registry.created == []


def test_battery_runtime_optimistic_profile_guard_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)

    runtime.set_battery_optimistic_profile("", None, None)
    assert runtime.optimistic_battery_profile() is None

    coord._battery_profile_authoritative_seen_mono = None  # noqa: SLF001
    assert runtime._profile_authoritative_read_is_fresh() is False  # noqa: SLF001

    class BadSeen:
        def __float__(self) -> float:
            raise ValueError("boom")

    coord._battery_profile_authoritative_seen_mono = BadSeen()  # noqa: SLF001
    assert runtime._profile_authoritative_read_is_fresh() is False  # noqa: SLF001

    coord._battery_pending_authoritative_confirmation_required = True  # noqa: SLF001
    coord._battery_pending_requested_mono = BadSeen()  # noqa: SLF001
    coord._battery_profile_authoritative_seen_mono = time.monotonic()  # noqa: SLF001
    assert (  # noqa: SLF001
        runtime._pending_authoritative_confirmation_received() is False
    )
    coord._battery_pending_authoritative_confirmation_required = False  # noqa: SLF001

    runtime.set_battery_optimistic_profile("backup_only", 100, None)
    runtime._note_authoritative_profile_read("backup_only")  # noqa: SLF001
    assert runtime.optimistic_battery_profile() is None

    coord._battery_optimistic_profile = "backup_only"  # noqa: SLF001
    coord._battery_optimistic_until_mono = 0.0  # noqa: SLF001
    assert runtime.optimistic_battery_profile() is None
    assert coord._battery_optimistic_profile is None  # noqa: SLF001

    class BadUntil:
        def __float__(self) -> float:
            raise ValueError("boom")

    coord._battery_optimistic_profile = "backup_only"  # noqa: SLF001
    coord._battery_optimistic_until_mono = BadUntil()  # noqa: SLF001
    assert runtime.optimistic_battery_profile() is None
    assert coord._battery_optimistic_profile is None  # noqa: SLF001


def test_backend_pending_flag_does_not_clear_recent_mismatched_request(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    requested_at = datetime.now(UTC)
    observed_at = requested_at + timedelta(seconds=FAST_TOGGLE_POLL_HOLD_S - 1)
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_profile = "backup_only"  # noqa: SLF001
    coord._battery_pending_reserve = 100  # noqa: SLF001
    coord._battery_pending_requested_at = requested_at  # noqa: SLF001
    coord._battery_polling_interval_s = 45  # noqa: SLF001

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: observed_at,
    )

    runtime.sync_backend_battery_profile_pending(False)

    assert coord._battery_backend_profile_update_pending is False  # noqa: SLF001
    assert coord.battery_profile_pending is True
    assert coord._battery_backend_not_pending_observed_at == observed_at  # noqa: SLF001


def test_backend_pending_flag_clears_stale_mismatched_request_after_grace(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    requested_at = datetime.now(UTC)
    first_false = requested_at + timedelta(seconds=10)
    second_false = requested_at + timedelta(seconds=FAST_TOGGLE_POLL_HOLD_S + 5)
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_profile = "backup_only"  # noqa: SLF001
    coord._battery_pending_reserve = 100  # noqa: SLF001
    coord._battery_pending_requested_at = requested_at  # noqa: SLF001
    coord._battery_polling_interval_s = 45  # noqa: SLF001

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: first_false,
    )
    runtime.sync_backend_battery_profile_pending(False)

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: second_false,
    )
    runtime.sync_backend_battery_profile_pending(False)

    assert coord.battery_profile_pending is False
    assert coord._battery_backend_profile_update_pending is None  # noqa: SLF001
    assert coord._battery_backend_not_pending_observed_at is None  # noqa: SLF001


def test_stale_profile_reads_do_not_revert_accepted_write_after_grace(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    requested_at = datetime.now(UTC)
    second_false = requested_at + timedelta(seconds=FAST_TOGGLE_POLL_HOLD_S + 5)
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_polling_interval_s = 45  # noqa: SLF001

    runtime.set_battery_pending(
        profile="backup_only",
        reserve=100,
        sub_type=None,
        require_exact_settings=False,
    )
    coord._battery_pending_requested_at = requested_at  # noqa: SLF001
    coord._battery_backend_not_pending_observed_at = requested_at  # noqa: SLF001

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: second_false,
    )

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "isBatteryChangePending": False,
            }
        }
    )

    assert coord.battery_profile_pending is True
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_selected_profile == "backup_only"
    assert coord.battery_effective_profile == "backup_only"
    assert coord.battery_effective_backup_percentage == 100

    runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "isBatteryChangePending": False,
            }
        }
    )

    assert coord.battery_profile_pending is True
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_selected_profile == "backup_only"
    assert coord.battery_effective_profile == "backup_only"
    assert coord.battery_effective_backup_percentage == 100

    runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "backup_only",
                "batteryBackupPercentage": 100,
                "isBatteryChangePending": False,
            }
        }
    )

    assert runtime.optimistic_battery_profile() is None
    assert coord.battery_profile_pending is False
    assert coord.battery_profile == "backup_only"
    assert coord.battery_effective_backup_percentage == 100


def test_profile_endpoint_wins_over_stale_battery_settings(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)

    runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "backup_only",
                "batteryBackupPercentage": 100,
            }
        }
    )
    runtime.parse_battery_settings_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
            }
        }
    )

    assert coord.battery_profile == "backup_only"
    assert coord.battery_effective_profile == "backup_only"
    assert coord.battery_effective_backup_percentage == 100


def test_stale_profile_endpoint_allows_battery_settings_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)

    runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "backup_only",
                "batteryBackupPercentage": 100,
            }
        }
    )
    coord._battery_profile_authoritative_seen_mono = (  # noqa: SLF001
        time.monotonic() - BATTERY_SETTINGS_CACHE_TTL - 1
    )

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
            }
        }
    )

    assert coord.battery_profile == "self-consumption"
    assert coord.battery_effective_profile == "self-consumption"
    assert coord.battery_effective_backup_percentage == 20


def test_profile_endpoint_confirms_pending_with_stale_live_profile(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_live_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001

    runtime.set_battery_pending(
        profile="backup_only",
        reserve=100,
        sub_type=None,
        require_exact_settings=False,
    )

    runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "backup_only",
                "batteryBackupPercentage": 100,
                "isBatteryChangePending": True,
            }
        }
    )

    assert runtime.optimistic_battery_profile() is None
    assert coord.battery_profile_pending is False
    assert coord.battery_profile == "backup_only"
    assert coord.battery_live_profile == "self-consumption"
    assert coord.battery_effective_profile == "backup_only"
    assert coord.battery_effective_backup_percentage == 100


def test_battery_settings_target_payload_does_not_confirm_pending_profile(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001

    runtime.set_battery_pending(
        profile="backup_only",
        reserve=100,
        sub_type=None,
        require_exact_settings=False,
    )

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "profile": "backup_only",
                "batteryBackupPercentage": 100,
                "isBatteryChangePending": False,
            }
        }
    )

    assert coord.battery_profile_pending is True
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_selected_profile == "backup_only"
    assert coord.battery_effective_profile == "backup_only"
    assert coord.battery_effective_backup_percentage == 100


def test_backend_not_pending_does_not_promote_unconfirmed_optimistic_profile(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    requested_at = datetime.now(UTC)
    second_false = requested_at + timedelta(seconds=FAST_TOGGLE_POLL_HOLD_S + 5)
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_polling_interval_s = 45  # noqa: SLF001

    runtime.set_battery_pending(
        profile="backup_only",
        reserve=100,
        sub_type=None,
        require_exact_settings=False,
    )
    coord._battery_pending_requested_at = requested_at  # noqa: SLF001
    coord._battery_backend_not_pending_observed_at = requested_at  # noqa: SLF001

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: second_false,
    )

    runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "isBatteryChangePending": False,
            }
        }
    )

    assert coord.battery_profile_pending is True
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_selected_profile == "backup_only"
    assert coord.battery_effective_profile == "backup_only"
    assert coord.battery_effective_backup_percentage == 100


def test_backend_not_pending_keeps_local_write_pending_after_optimistic_expiry(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    requested_at = datetime.now(UTC)
    second_false = requested_at + timedelta(seconds=FAST_TOGGLE_POLL_HOLD_S + 5)
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_polling_interval_s = 45  # noqa: SLF001

    runtime.set_battery_pending(
        profile="backup_only",
        reserve=100,
        sub_type=None,
        require_exact_settings=False,
    )
    coord._battery_pending_requested_at = requested_at  # noqa: SLF001
    coord._battery_backend_not_pending_observed_at = requested_at  # noqa: SLF001
    coord._battery_optimistic_until_mono = 0.0  # noqa: SLF001

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: second_false,
    )

    runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "isBatteryChangePending": False,
            }
        }
    )

    assert runtime.optimistic_battery_profile() is None
    assert coord.battery_profile_pending is True
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_selected_profile == "backup_only"
    assert coord.battery_effective_profile == "self-consumption"
    assert coord.battery_selected_backup_percentage == 100


def test_profile_match_with_stale_reserve_does_not_clear_local_pending(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    requested_at = datetime.now(UTC)
    second_false = requested_at + timedelta(seconds=FAST_TOGGLE_POLL_HOLD_S + 5)
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_polling_interval_s = 45  # noqa: SLF001

    runtime.set_battery_pending(
        profile="self-consumption",
        reserve=30,
        sub_type=None,
        require_exact_settings=True,
    )
    coord._battery_pending_requested_at = requested_at  # noqa: SLF001
    coord._battery_backend_not_pending_observed_at = requested_at  # noqa: SLF001
    coord._battery_optimistic_until_mono = 0.0  # noqa: SLF001

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: second_false,
    )

    runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 20,
                "isBatteryChangePending": False,
            }
        }
    )

    assert runtime.optimistic_battery_profile() is None
    assert coord.battery_profile_pending is True
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_effective_backup_percentage == 20
    assert coord.battery_selected_backup_percentage == 30

    runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 30,
                "isBatteryChangePending": False,
            }
        }
    )

    assert coord.battery_profile_pending is False
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_effective_backup_percentage == 30


def test_prepopulated_local_target_does_not_confirm_before_profile_read(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    requested_at = datetime.now(UTC)
    second_false = requested_at + timedelta(seconds=FAST_TOGGLE_POLL_HOLD_S + 5)
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001
    coord._battery_polling_interval_s = 45  # noqa: SLF001

    runtime.parse_battery_settings_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 30,
            }
        }
    )
    runtime.set_battery_pending(
        profile="self-consumption",
        reserve=30,
        sub_type=None,
        require_exact_settings=True,
    )
    coord._battery_pending_requested_at = requested_at  # noqa: SLF001
    coord._battery_backend_not_pending_observed_at = requested_at  # noqa: SLF001

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: second_false,
    )

    runtime.sync_backend_battery_profile_pending(False)

    assert coord.battery_profile_pending is True
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_effective_backup_percentage == 30

    runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "self-consumption",
                "batteryBackupPercentage": 30,
                "isBatteryChangePending": False,
            }
        }
    )

    assert coord.battery_profile_pending is False
    assert coord.battery_profile == "self-consumption"
    assert coord.battery_effective_backup_percentage == 30


def test_backend_pending_true_resets_not_pending_observation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._battery_backend_not_pending_observed_at = datetime.now(UTC)  # noqa: SLF001

    runtime.sync_backend_battery_profile_pending(True)

    assert coord._battery_backend_profile_update_pending is True  # noqa: SLF001
    assert coord._battery_backend_not_pending_observed_at is None  # noqa: SLF001


def test_backend_pending_false_without_local_pending_resets_observation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._battery_backend_not_pending_observed_at = datetime.now(UTC)  # noqa: SLF001

    runtime.sync_backend_battery_profile_pending(False)

    assert coord._battery_backend_profile_update_pending is False  # noqa: SLF001
    assert coord._battery_backend_not_pending_observed_at is None  # noqa: SLF001


def test_backend_pending_false_clears_when_effective_state_matches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._battery_pending_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_reserve = 20  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(UTC)  # noqa: SLF001
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_backup_percentage = 20  # noqa: SLF001

    runtime.sync_backend_battery_profile_pending(False)

    assert coord.battery_profile_pending is False
    assert coord._battery_backend_profile_update_pending is None  # noqa: SLF001


def test_backend_pending_false_keeps_pending_when_age_unavailable(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._battery_profile = "self-consumption"  # noqa: SLF001
    coord._battery_pending_profile = "backup_only"  # noqa: SLF001
    coord._battery_pending_reserve = 100  # noqa: SLF001
    coord._battery_pending_requested_at = "invalid"  # noqa: SLF001
    coord._battery_backend_not_pending_observed_at = datetime.now(UTC)  # noqa: SLF001

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: datetime.now(UTC),
    )

    runtime.sync_backend_battery_profile_pending(False)

    assert coord.battery_profile_pending is True
    assert coord._battery_backend_profile_update_pending is False  # noqa: SLF001
    assert coord._battery_backend_not_pending_observed_at is not None  # noqa: SLF001


def test_battery_profile_refresh_cache_ttl_tracks_polling_cadence(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._configured_slow_poll_interval = 120  # noqa: SLF001
    coord.update_interval = timedelta(seconds=30)
    coord._battery_polling_interval_s = 60  # noqa: SLF001

    assert (
        runtime._battery_profile_refresh_cache_ttl_seconds(300.0) == 60.0
    )  # noqa: SLF001

    coord.update_interval = None
    coord._battery_polling_interval_s = None  # noqa: SLF001

    assert (
        runtime._battery_profile_refresh_cache_ttl_seconds(300.0) == 120.0
    )  # noqa: SLF001


def test_battery_profile_refresh_cache_ttl_handles_error_and_default_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)

    class BadInterval:
        def total_seconds(self) -> float:
            raise RuntimeError("boom")

    coord._update_interval = BadInterval()  # noqa: SLF001
    coord._configured_slow_poll_interval = "invalid"  # noqa: SLF001
    coord._battery_polling_interval_s = 45  # noqa: SLF001

    assert (
        runtime._battery_profile_refresh_cache_ttl_seconds(300.0) == 45.0
    )  # noqa: SLF001
    coord._battery_polling_interval_s = None  # noqa: SLF001
    assert (
        runtime._backend_not_pending_clear_grace_seconds() == FAST_TOGGLE_POLL_HOLD_S
    )  # noqa: SLF001

    coord._update_interval = object()  # noqa: SLF001
    coord._configured_slow_poll_interval = None  # noqa: SLF001

    assert (
        runtime._battery_profile_refresh_cache_ttl_seconds(300.0) == 300.0
    )  # noqa: SLF001


def test_battery_runtime_matching_handles_exact_savings_subtype_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_require_exact_settings = True  # noqa: SLF001
    coord._battery_pending_reserve = 30  # noqa: SLF001
    coord._battery_backup_percentage = 30  # noqa: SLF001

    coord._battery_pending_sub_type = SAVINGS_OPERATION_MODE_SUBTYPE  # noqa: SLF001
    coord._battery_operation_mode_sub_type = (
        SAVINGS_OPERATION_MODE_SUBTYPE  # noqa: SLF001
    )
    assert runtime.effective_profile_matches_pending() is True

    coord._battery_operation_mode_sub_type = None  # noqa: SLF001
    assert runtime.effective_profile_matches_pending() is False

    coord._battery_pending_sub_type = None  # noqa: SLF001
    coord._battery_operation_mode_sub_type = "other"  # noqa: SLF001
    assert runtime.effective_profile_matches_pending() is True

    coord._battery_operation_mode_sub_type = (
        SAVINGS_OPERATION_MODE_SUBTYPE  # noqa: SLF001
    )
    assert runtime.effective_profile_matches_pending() is False


def test_battery_runtime_target_reserve_and_current_savings_subtype(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = BatteryRuntime(coord)
    coord._battery_profile_reserve_memory = {  # noqa: SLF001
        "cost_savings": 35,
        "ai_optimisation": 12,
    }
    coord._battery_pending_profile = "ai_optimisation"  # noqa: SLF001
    coord._battery_pending_sub_type = SAVINGS_OPERATION_MODE_SUBTYPE  # noqa: SLF001

    assert runtime.target_reserve_for_profile("cost_savings") == 35
    assert runtime.target_reserve_for_profile("ai_optimisation") == 12
    assert runtime.target_reserve_for_profile("backup_only") == 100
    assert runtime.current_savings_sub_type() == SAVINGS_OPERATION_MODE_SUBTYPE

    runtime.remember_battery_reserve("self-consumption", 28)
    assert (
        coord._battery_profile_reserve_memory["self-consumption"] == 28
    )  # noqa: SLF001

    runtime.remember_battery_reserve("custom", 44)
    assert "custom" not in coord._battery_profile_reserve_memory


def test_battery_runtime_remembers_ai_optimisation_reserve_from_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_profile_payload(
        {
            "profile": "self-consumption",
            "batteryBackupPercentage": 20,
            "previousBatteryBackupPercentage": {
                "cost_savings": 49,
                "ai_optimisation": 10,
                "expert": 30,
            },
        }
    )

    assert coord._target_reserve_for_profile("cost_savings") == 49  # noqa: SLF001
    assert coord._target_reserve_for_profile("ai_optimisation") == 10  # noqa: SLF001


def test_battery_runtime_ignores_invalid_previous_reserve_entries(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime

    coord._battery_profile_reserve_memory = {}  # noqa: SLF001

    runtime.remember_previous_battery_reserves(
        {
            "": 15,
            "cost_savings": "invalid",
            "ai_optimisation": 10,
        }
    )

    assert "cost_savings" not in coord._battery_profile_reserve_memory  # noqa: SLF001
    assert (
        coord._battery_profile_reserve_memory["ai_optimisation"] == 10
    )  # noqa: SLF001


def test_battery_runtime_current_savings_subtype_uses_coordinator_property() -> None:
    coordinator = SimpleNamespace(battery_selected_operation_mode_sub_type="other")
    runtime = BatteryRuntime(coordinator)

    assert runtime.current_savings_sub_type() is None
    assert runtime.target_operation_mode_sub_type("cost_savings") is None
    assert (
        runtime.target_operation_mode_sub_type("ai_optimisation")
        == SAVINGS_OPERATION_MODE_SUBTYPE
    )

    coordinator.battery_selected_operation_mode_sub_type = (
        SAVINGS_OPERATION_MODE_SUBTYPE
    )
    assert runtime.current_savings_sub_type() == SAVINGS_OPERATION_MODE_SUBTYPE
    assert (
        runtime.target_operation_mode_sub_type("cost_savings")
        == SAVINGS_OPERATION_MODE_SUBTYPE
    )
    assert (
        runtime.target_operation_mode_sub_type("ai_optimisation")
        == SAVINGS_OPERATION_MODE_SUBTYPE
    )


def test_battery_runtime_set_pending_records_current_time(monkeypatch) -> None:
    requested_at = datetime.now(UTC) + timedelta(minutes=5)
    coordinator = SimpleNamespace(
        _normalize_battery_sub_type=lambda value: value,
        _sync_battery_profile_pending_issue=lambda: None,
    )
    runtime = BatteryRuntime(coordinator)

    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.dt_util.utcnow",
        lambda: requested_at,
    )

    runtime.set_battery_pending(
        profile="self-consumption",
        reserve=15,
        sub_type="ignored",
    )

    assert coordinator._battery_pending_requested_at == requested_at
    assert coordinator._battery_pending_sub_type is None


def test_battery_runtime_transition_helpers_cover_private_and_fallback_paths() -> None:
    private_sync = Mock()
    private_raise = Mock()
    coordinator = SimpleNamespace(
        _normalize_battery_sub_type=lambda value: f"private:{value}",
        _sync_battery_profile_pending_issue=private_sync,
        _current_charge_from_grid_schedule_window=lambda: (11, 22),
        _raise_grid_validation=private_raise,
    )
    runtime = BatteryRuntime(coordinator)

    assert runtime._normalize_battery_sub_type("x") == "private:x"
    runtime._sync_battery_profile_pending_issue()
    private_sync.assert_called_once_with()
    assert runtime._coerce_int("41", default=-1) == 41
    assert runtime._coerce_optional_bool("yes") is True
    assert runtime._coerce_optional_text(" a ") == "a"
    assert runtime._current_schedule_window_from_coordinator() == (11, 22)

    runtime.raise_grid_validation("grid_control_unavailable")
    private_raise.assert_called_once_with(
        "grid_control_unavailable",
        placeholders=None,
        message=None,
    )


def test_battery_runtime_transition_helpers_cover_public_paths() -> None:
    public_sync = Mock()
    public_raise = Mock()
    coordinator = SimpleNamespace(
        normalize_battery_sub_type=lambda value: f"public:{value}",
        sync_battery_profile_pending_issue=public_sync,
        current_charge_from_grid_schedule_window=lambda: (33, 44),
        raise_grid_validation=public_raise,
    )
    runtime = BatteryRuntime(coordinator)

    assert runtime._normalize_battery_sub_type("X") == "public:X"
    runtime._sync_battery_profile_pending_issue()
    public_sync.assert_called_once_with()
    assert runtime._coerce_int("52", default=-1) == 52
    assert runtime._coerce_optional_float("2.5") == pytest.approx(2.5)
    assert runtime._coerce_optional_bool("no") is False
    assert runtime._coerce_optional_text(" A ") == "A"
    assert runtime._current_schedule_window_from_coordinator() == (33, 44)

    runtime.raise_grid_validation("grid_control_unavailable")
    public_raise.assert_called_once_with(
        "grid_control_unavailable",
        placeholders=None,
        message=None,
    )


def test_battery_runtime_transition_helpers_cover_plain_fallback_paths() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    runtime = BatteryRuntime(SimpleNamespace())

    assert runtime._normalize_battery_sub_type("x") is None
    runtime._sync_battery_profile_pending_issue()
    assert runtime._coerce_int("7", default=-1) == 7
    assert runtime._coerce_int(_BadStr(), default=-1) == -1
    assert runtime._coerce_optional_bool("x") is None
    assert runtime._coerce_optional_text(None) is None
    assert runtime._coerce_optional_text(_BadStr()) is None
    assert runtime._coerce_optional_text("   ") is None
    assert runtime._current_schedule_window_from_coordinator() == (120, 300)

    with pytest.raises(ServiceValidationError):
        runtime.raise_grid_validation("grid_control_unavailable")


def test_battery_runtime_top_level_helper_passthroughs_cover_private_hooks() -> None:
    refreshed = Mock()
    runtime = BatteryRuntime(
        SimpleNamespace(
            _coerce_optional_int=lambda value: 7 if value == "7" else None,
            _coerce_optional_float=lambda value: 1.5 if value == "1.5" else None,
            _coerce_optional_kwh=lambda value: 2.5 if value == "2.5" else None,
            _parse_percent_value=lambda value: 55.0 if value == "55" else None,
            _normalize_battery_status_text=lambda value: (
                "normal" if value == "Normal" else None
            ),
            _battery_status_severity_value=lambda value: 2 if value == "normal" else 0,
            _battery_storage_key=lambda payload: payload.get("serial_number"),
            _normalize_battery_id=lambda value: str(value),
            _refresh_cached_topology=refreshed,
            _normalize_battery_grid_mode=lambda value: (
                "on_grid" if value == "on-grid" else None
            ),
            _normalize_minutes_of_day=lambda value: 45 if value == "00:45" else None,
        )
    )

    assert runtime._coerce_optional_int("7") == 7
    assert runtime._coerce_optional_float("1.5") == pytest.approx(1.5)
    assert runtime._coerce_optional_kwh("2.5") == pytest.approx(2.5)
    assert runtime._parse_percent_value("55") == pytest.approx(55.0)
    assert runtime._normalize_battery_status_text("Normal") == "normal"
    assert runtime._battery_status_severity_value("normal") == 2
    assert runtime._battery_storage_key({"serial_number": "BAT-1"}) == "BAT-1"
    assert runtime._normalize_battery_id(3) == "3"
    runtime._refresh_cached_topology()
    refreshed.assert_called_once_with()
    assert runtime._normalize_battery_grid_mode("on-grid") == "on_grid"
    assert runtime._normalize_minutes_of_day("00:45") == 45
    assert runtime._copy_dry_contact_settings_entry({"id": 1}) == {"id": 1}
    assert runtime._dry_contact_settings_looks_like_entry({"id": 1}) is True
    assert runtime._normalize_dry_contact_schedule_windows([{"start": "1"}]) == [
        {"start": "1"}
    ]
    assert runtime._dry_contact_members_for_settings() == []
    assert runtime._match_dry_contact_settings(
        [{"device_uid": "DC-1"}],
        settings_entries=[{"id": 1}],
    ) == ([None], [{"id": 1}])


def test_battery_runtime_top_level_helper_passthroughs_cover_fallback_paths() -> None:
    runtime = BatteryRuntime(SimpleNamespace())
    source = {"id": 1, "nested": {"value": 2}, "items": [{"x": 1}]}

    assert runtime._coerce_optional_int("7") == 7
    assert runtime._coerce_optional_float("1.5") == pytest.approx(1.5)
    assert runtime._coerce_optional_kwh("2.5") is None
    assert runtime._parse_percent_value("55") is None
    assert runtime._normalize_battery_status_text("Normal") is None
    assert runtime._battery_status_severity_value("normal") == 0
    assert runtime._battery_storage_key({"serial_number": "BAT-1"}) is None
    assert runtime._normalize_battery_id(3) is None
    runtime._refresh_cached_topology()
    assert runtime._normalize_battery_grid_mode("on-grid") is None
    assert runtime._normalize_minutes_of_day("00:45") is None
    copied = runtime._copy_dry_contact_settings_entry(source)
    assert copied == source
    assert copied["nested"] is not source["nested"]
    assert copied["items"] is not source["items"]
    assert runtime._dry_contact_settings_looks_like_entry({"id": 1}) is True
    assert runtime._normalize_dry_contact_schedule_windows([{"start": "1"}]) == [
        {"start": "1"}
    ]
    assert runtime._dry_contact_members_for_settings() == []
    assert runtime._match_dry_contact_settings(
        [{"device_uid": "DC-1"}],
        settings_entries=[{"id": 1}],
    ) == ([None], [{"id": 1}])


def test_battery_runtime_dry_contact_member_collection_handles_callable_none_bucket() -> (
    None
):
    runtime = BatteryRuntime(SimpleNamespace(type_bucket=lambda _key: None))

    assert runtime._dry_contact_members_for_settings() == []


def test_battery_runtime_dry_contact_member_collection_skips_non_dict_members() -> None:
    runtime = BatteryRuntime(
        SimpleNamespace(
            type_bucket=lambda key: {"devices": ["bad"]} if key == "dry_contact" else {}
        )
    )

    assert runtime._dry_contact_members_for_settings() == []


def test_battery_runtime_current_schedule_window_plain_fallback_normalizes_values() -> (
    None
):
    runtime = BatteryRuntime(
        SimpleNamespace(
            _battery_charge_begin_time="45",
            _battery_charge_end_time="1800",
        )
    )

    assert runtime.current_charge_from_grid_schedule_window() == (45, 300)


def test_battery_runtime_current_schedule_window_plain_fallback_handles_invalid_value() -> (
    None
):
    runtime = BatteryRuntime(
        SimpleNamespace(
            _battery_charge_begin_time="invalid",
            _battery_charge_end_time="180",
        )
    )

    assert runtime.current_charge_from_grid_schedule_window() == (120, 180)


def test_battery_runtime_pending_subtype_falls_back_to_private_normalizer() -> None:
    coordinator = SimpleNamespace(_normalize_battery_sub_type=lambda value: value)

    assert (
        BatteryRuntime._normalize_pending_sub_type(
            coordinator,
            "cost_savings",
            SAVINGS_OPERATION_MODE_SUBTYPE,
        )
        == SAVINGS_OPERATION_MODE_SUBTYPE
    )
    assert (
        BatteryRuntime._normalize_pending_sub_type(
            SimpleNamespace(),
            "cost_savings",
            SAVINGS_OPERATION_MODE_SUBTYPE,
        )
        is None
    )


def test_battery_runtime_backup_history_parser_handles_invalid_entries() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    def _coerce_int(value, default=-1):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    coordinator = SimpleNamespace(
        _coerce_int=_coerce_int,
        _battery_timezone="Invalid/Zone",
        site_id="1234",
    )
    runtime = BatteryRuntime(coordinator)

    assert runtime.parse_battery_backup_history_payload(None) is None
    assert runtime.parse_battery_backup_history_payload({"histories": "bad"}) is None

    events = runtime.parse_battery_backup_history_payload(
        {
            "histories": [
                None,
                {"duration": "bad", "start_time": "2025-01-01T00:00:00"},
                {"duration": 30, "start_time": _BadStr()},
                {"duration": 30, "start_time": " "},
                {"duration": 30, "start_time": "2025-01-01T00:00:00"},
            ],
            "total_records": 9,
            "total_backup": 99,
        }
    )

    assert events is not None
    assert len(events) == 1
    assert events[0]["start"].tzinfo == UTC


def test_battery_runtime_backup_history_tzinfo_falls_back_to_utc(hass) -> None:
    del hass
    runtime = BatteryRuntime(SimpleNamespace(_battery_timezone=None))
    original_tz = dt_util.DEFAULT_TIME_ZONE
    dt_util.DEFAULT_TIME_ZONE = None
    try:
        assert runtime.backup_history_tzinfo() == UTC
    finally:
        dt_util.DEFAULT_TIME_ZONE = original_tz


def test_battery_runtime_storm_guard_pending_and_profile_branches(
    hass,
    monkeypatch,
) -> None:
    del hass
    coord = SimpleNamespace(
        _storm_guard_pending_state=None,
        _storm_guard_pending_expires_mono=None,
        _storm_guard_state=None,
        _coerce_optional_bool=lambda value: None if value is None else bool(value),
    )
    runtime = BatteryRuntime(coord)

    runtime.set_storm_guard_pending("enabled")
    coord._storm_guard_state = "disabled"  # noqa: SLF001
    coord._storm_guard_pending_expires_mono = None  # noqa: SLF001
    runtime.sync_storm_guard_pending()
    assert coord._storm_guard_pending_state is None  # noqa: SLF001

    runtime.set_storm_guard_pending("enabled")
    coord._storm_guard_state = "disabled"  # noqa: SLF001
    expires_at = coord._storm_guard_pending_expires_mono  # noqa: SLF001
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_runtime.time.monotonic",
        lambda: float(expires_at) + 1,
    )
    runtime.sync_storm_guard_pending()
    assert coord._storm_guard_pending_state is None  # noqa: SLF001

    assert runtime.parse_storm_guard_profile(None) == (None, None)
    assert runtime.parse_storm_guard_profile(
        {"stormGuardState": "enabled", "evseStormEnabled": 1}
    ) == ("enabled", True)


def test_battery_runtime_raise_grid_validation_uses_message() -> None:
    runtime = BatteryRuntime(SimpleNamespace())

    with pytest.raises(ServiceValidationError, match="custom message"):
        runtime.raise_grid_validation(
            "grid_control_unavailable",
            message="custom message",
        )


def test_battery_runtime_parse_status_payload_updates_runtime_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])

    coord.battery_runtime.parse_battery_status_payload(
        {
            "storages": [
                {
                    "serial_number": "BAT-1",
                    "current_charge": 55,
                    "available_energy": 2.2,
                    "max_capacity": 4.0,
                    "status": "normal",
                }
            ]
        }
    )

    assert coord._battery_storage_order == ["BAT-1"]  # noqa: SLF001
    assert coord.battery_aggregate_charge_pct == pytest.approx(55.0)
    assert coord.battery_aggregate_status == "normal"


def test_battery_runtime_parse_status_payload_seeds_ac_battery_fallback(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._battery_has_encharge = False  # noqa: SLF001

    coord.battery_runtime.parse_battery_status_payload(
        {
            "storages": [
                {
                    "id": 110528990,
                    "serial_number": "BAT-AC-1",
                    "current_charge": "1%",
                    "available_energy": 0.01,
                    "max_capacity": 1.24,
                    "statusText": "Normal",
                    "status": "normal",
                    "last_report": 1_776_981_758,
                    "cycle_count": 53,
                    "battery_mode": "Self-Consumption",
                    "rated_power": "N/A",
                    "battery_phase_count": "N/A",
                }
            ]
        }
    )

    assert coord.iter_ac_battery_serials() == ["BAT-AC-1"]
    snapshot = coord.ac_battery_storage("BAT-AC-1")
    assert snapshot is not None
    assert snapshot["battery_id"] == "110528990"
    assert snapshot["current_charge_pct"] == 1.0
    assert snapshot["status_normalized"] == "normal"
    assert snapshot["cycle_count"] == 53
    assert snapshot["last_reported"] == datetime.fromtimestamp(1_776_981_758, UTC)
    assert coord.ac_battery_aggregate_status == "normal"
    assert coord.ac_battery_status_summary["status_source"] == "battery_status"


@pytest.mark.asyncio
async def test_ac_battery_devices_refresh_preserves_status_fallback_rows(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._battery_has_encharge = False  # noqa: SLF001
    coord.client.ac_battery_devices_page = AsyncMock(
        return_value="<html><table id='ac_batteries'></table></html>"
    )

    coord.battery_runtime.parse_battery_status_payload(
        {
            "storages": [
                {
                    "id": 110528990,
                    "serial_number": "BAT-AC-1",
                    "current_charge": "1%",
                    "statusText": "Normal",
                    "status": "normal",
                    "last_report": 1_776_981_758,
                }
            ]
        }
    )

    await coord.battery_runtime.async_refresh_ac_battery_devices(force=True)

    assert coord.iter_ac_battery_serials() == ["BAT-AC-1"]
    assert coord.ac_battery_storage("BAT-AC-1")["battery_id"] == "110528990"
    assert coord.ac_battery_aggregate_status == "normal"
    assert coord.ac_battery_status_summary["status_source"] == "battery_status"
    assert coord._ac_battery_devices_payload["battery_count"] == 1  # noqa: SLF001


def test_battery_runtime_ac_battery_status_fallback_guard_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[])
    coord._battery_has_acb = True  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._selected_type_keys = {"envoy"}  # noqa: SLF001

    coord.battery_runtime._sync_ac_battery_from_battery_status(  # noqa: SLF001
        {"BAT-AC-1": {"serial_number": "BAT-AC-1"}}
    )
    assert coord.iter_ac_battery_serials() == []

    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._type_device_buckets = {}  # noqa: SLF001
    coord.battery_runtime._sync_ac_battery_from_battery_status(  # noqa: SLF001
        {"BAT-AC-1": {"serial_number": "BAT-AC-1"}}
    )
    assert coord.iter_ac_battery_serials() == []

    coord._type_device_buckets = {  # noqa: SLF001
        "ac_battery": {
            "count": 1,
            "devices": [
                "bad-member",
                {"id": "110528990", "serial_number": "BAT-AC-1"},
            ],
        }
    }
    coord.battery_runtime._sync_ac_battery_from_battery_status(  # noqa: SLF001
        {"BAT-SKIP": {"battery_id": "999", "serial_number": "BAT-SKIP"}}
    )
    assert coord.iter_ac_battery_serials() == []

    coord.battery_runtime._sync_ac_battery_from_battery_status(  # noqa: SLF001
        {
            "BAT-SKIP": {"battery_id": "999", "serial_number": "BAT-SKIP"},
            "BAT-AC-1": {
                "battery_id": "110528990",
                "serial_number": "BAT-AC-1",
                "status_text": None,
                "status_normalized": None,
            },
        }
    )

    assert coord.iter_ac_battery_serials() == ["BAT-AC-1"]
    assert coord.ac_battery_aggregate_status == "unknown"


def test_battery_runtime_parse_profile_payload_updates_profile_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_reserve = 15  # noqa: SLF001
    coord._battery_pending_sub_type = SAVINGS_OPERATION_MODE_SUBTYPE  # noqa: SLF001

    coord.battery_runtime.parse_battery_profile_payload(
        {
            "profile": "cost_savings",
            "batteryBackupPercentage": 15,
            "operationModeSubType": SAVINGS_OPERATION_MODE_SUBTYPE,
            "supportsMqtt": True,
            "pollingInterval": 45,
            "evseStormEnabled": True,
            "stormGuardState": "enabled",
            "cfgControl": {
                "show": True,
                "enabled": True,
                "scheduleSupported": True,
                "forceScheduleSupported": False,
            },
            "devices": {
                "iqEvse": [
                    {
                        "uuid": "evse-1",
                        "deviceName": "IQ EV Charger",
                        "profile": "self-consumption",
                        "profileConfig": "full",
                        "chargeMode": "MANUAL",
                        "chargeModeStatus": "COMPLETED",
                        "status": -1,
                        "updatedAt": 12345,
                        "enable": 0,
                    }
                ]
            },
        }
    )

    assert coord.battery_profile == "cost_savings"
    assert coord.battery_profile_pending is False
    assert coord.battery_supports_mqtt is True
    assert coord.storm_evse_enabled is True
    assert coord.storm_guard_state == "enabled"
    assert coord.battery_cfg_control_show is True
    assert coord.battery_cfg_control_enabled is True
    assert coord.battery_cfg_control_schedule_supported is True
    assert coord.battery_cfg_control_force_schedule_supported is False
    assert coord.battery_profile_polling_interval == 45
    assert coord.battery_profile_evse_device is not None
    assert coord.battery_profile_evse_device["uuid"] == "evse-1"


def test_battery_runtime_parse_site_settings_payload_handles_text_edges(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord.battery_runtime.parse_battery_site_settings_payload(
        {
            "data": {
                "showChargeFromGrid": True,
                "countryCode": BadStr(),
                "batteryGridMode": "ImportOnly",
                "featureDetails": {
                    "": True,
                    "RawStringFlag": "enabled-with-note",
                },
            }
        }
    )

    assert coord._battery_show_charge_from_grid is True  # noqa: SLF001
    assert coord.battery_country_code is None
    assert coord.battery_grid_mode == "ImportOnly"
    assert coord.battery_feature_details == {"RawStringFlag": "enabled-with-note"}


def test_battery_runtime_profile_option_passthrough_for_unknown_mode(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_show_charge_from_grid = True  # noqa: SLF001
    coord._battery_profile = "regional_special"  # noqa: SLF001
    coord._battery_profile_reserve_memory["self-consumption"] = 31  # noqa: SLF001

    coord.battery_runtime.parse_battery_profile_payload(
        {"profile": "regional_special", "batteryBackupPercentage": 55}
    )

    options = coord.battery_profile_option_keys
    labels = coord.battery_profile_option_labels

    assert "self-consumption" in options
    assert "regional_special" in options
    assert labels["regional_special"] == "Regional Special"
    assert coord._target_reserve_for_profile("self-consumption") == 31  # noqa: SLF001
    assert (
        "regional_special" not in coord._battery_profile_reserve_memory
    )  # noqa: SLF001


def test_battery_runtime_parse_site_settings_payload_supports_ai_optimisation_flag(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_site_settings_payload(
        {
            "data": {
                "showChargeFromGrid": True,
                "showSavingsMode": False,
                "showAiOptiSavingsMode": True,
                "isEmea": False,
                "showFullBackup": True,
            }
        }
    )

    assert coord._battery_show_ai_optimisation_mode is True  # noqa: SLF001
    assert coord.battery_is_emea is False
    assert coord.battery_profile_option_keys == [
        "self-consumption",
        "ai_optimisation",
        "backup_only",
    ]
    assert coord.battery_profile_option_labels["ai_optimisation"] == "AI Optimisation"


def test_battery_runtime_parse_profile_payload_clears_pending_exact_match(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_reserve = 22  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(UTC)  # noqa: SLF001

    coord.battery_runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "cost_savings",
                "batteryBackupPercentage": 22,
                "operationModeSubType": "prioritize-energy",
                "stormGuardState": "disabled",
                "evseStormEnabled": False,
                "devices": {
                    "iqEvse": [
                        {
                            "uuid": "evse-1",
                            "chargeMode": "MANUAL",
                            "enable": False,
                        }
                    ]
                },
            }
        }
    )

    assert coord.battery_profile_pending is False
    assert coord.battery_pending_profile is None


def test_battery_runtime_parse_profile_payload_branches_and_helpers(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    class BadUuid:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord.battery_runtime.parse_battery_profile_payload([])
    coord.battery_runtime.parse_battery_site_settings_payload([])
    coord.battery_runtime.parse_battery_site_settings_payload(
        {"showChargeFromGrid": True, "showAiOptiSavingsMode": True}
    )

    coord.battery_runtime.parse_battery_profile_payload(
        {
            "profile": "cost_savings",
            "batteryBackupPercentage": 15,
            "operationModeSubType": "prioritize-energy",
            "supportsMqtt": True,
            "pollingInterval": 45,
            "evseStormEnabled": True,
            "stormGuardState": "enabled",
            "cfgControl": {
                "show": True,
                "enabled": True,
                "scheduleSupported": True,
                "forceScheduleSupported": False,
            },
            "devices": {
                "iqEvse": [
                    "bad",
                    {"chargeMode": "MANUAL"},
                    {"uuid": BadUuid(), "chargeMode": "MANUAL", "enable": True},
                    {
                        "uuid": "evse-1",
                        "deviceName": "IQ EV Charger",
                        "profile": "self-consumption",
                        "profileConfig": "full",
                        "chargeMode": "MANUAL",
                        "chargeModeStatus": "COMPLETED",
                        "status": -1,
                        "updatedAt": 12345,
                        "enable": 0,
                    },
                ]
            },
        }
    )
    assert coord.battery_profile == "cost_savings"
    assert coord._battery_polling_interval_s == 45  # noqa: SLF001
    assert coord.battery_supports_mqtt is True
    assert coord.storm_evse_enabled is True
    assert coord.storm_guard_state == "enabled"
    assert coord.battery_cfg_control_show is True
    assert coord.battery_cfg_control_enabled is True
    assert coord.battery_cfg_control_schedule_supported is True
    assert coord.battery_cfg_control_force_schedule_supported is False
    assert coord.battery_profile_evse_device is not None
    assert coord.battery_profile_evse_device["uuid"] == "evse-1"
    assert coord.battery_profile_evse_device["device_name"] == "IQ EV Charger"
    profile_devices_success = (
        coord._battery_profile_devices_last_success_mono
    )  # noqa: SLF001
    assert isinstance(profile_devices_success, float)

    coord.battery_runtime.parse_battery_profile_payload({"unexpected": True})
    assert (  # noqa: SLF001
        coord._battery_profile_devices_last_success_mono == profile_devices_success
    )

    coord.battery_runtime.parse_battery_profile_payload(
        {"profile": "backup_only", "batteryBackupPercentage": 80}
    )
    assert coord.battery_effective_backup_percentage == 100
    assert coord._battery_profile_devices == []  # noqa: SLF001
    assert coord.battery_profile_devices_payload() is None
    assert (  # noqa: SLF001
        coord._battery_profile_devices_last_success_mono >= profile_devices_success
    )

    coord._battery_profile_devices = [  # noqa: SLF001
        {"uuid": "stale", "chargeMode": "MANUAL", "enable": True}
    ]
    coord.battery_runtime.parse_battery_profile_payload({"devices": {"iqEvse": []}})
    assert coord._battery_profile_devices == []  # noqa: SLF001

    coord._battery_profile_devices = [  # noqa: SLF001
        {"chargeMode": "MANUAL", "enable": True},
        {"uuid": "1", "enable": False},
        {"uuid": " 1 ", "enable": True},
        {"uuid": "   ", "enable": True},
        {"uuid": "2", "chargeMode": "MANUAL", "enable": True},
        {"uuid": "3", "chargeMode": "SCHEDULED", "enable": None},
    ]

    class _BadUuid:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord._battery_profile_devices.append(  # noqa: SLF001
        {"uuid": _BadUuid(), "enable": True}
    )
    payload = coord._battery_profile_devices_payload()  # noqa: SLF001
    assert payload is not None
    assert len(payload) == 3
    assert payload[0]["deviceType"] == "iqEvse"
    assert payload[0].get("chargeMode") is None
    assert payload[1]["chargeMode"] == "MANUAL"
    assert payload[2]["chargeMode"] == "SCHEDULED"
    assert "enable" not in payload[2]

    assert coord._target_reserve_for_profile("backup_only") == 100  # noqa: SLF001
    coord._battery_profile_reserve_memory.pop("cost_savings", None)  # noqa: SLF001
    assert coord._target_reserve_for_profile("cost_savings") == 20  # noqa: SLF001
    coord._battery_profile_reserve_memory.pop("ai_optimisation", None)  # noqa: SLF001
    assert coord._target_reserve_for_profile("ai_optimisation") == 10  # noqa: SLF001
    assert coord._target_reserve_for_profile("regional_special") == 20  # noqa: SLF001
    assert coord._current_savings_sub_type() is None  # noqa: SLF001
    coord._battery_pending_profile = "cost_savings"  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    assert coord._current_savings_sub_type() == "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_profile = "ai_optimisation"  # noqa: SLF001
    assert coord._current_savings_sub_type() == "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_sub_type = "other"  # noqa: SLF001
    assert coord._current_savings_sub_type() is None  # noqa: SLF001


def test_battery_runtime_ai_profile_parses_and_clears_pending(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._battery_pending_profile = "ai_optimisation"  # noqa: SLF001
    coord._battery_pending_reserve = 10  # noqa: SLF001
    coord._battery_pending_sub_type = "prioritize-energy"  # noqa: SLF001
    coord._battery_pending_requested_at = datetime.now(UTC)  # noqa: SLF001

    coord.battery_runtime.parse_battery_profile_payload(
        {
            "data": {
                "profile": "ai_optimisation",
                "batteryBackupPercentage": 10,
                "operationModeSubType": "prioritize-energy",
            }
        }
    )

    assert coord.battery_profile == "ai_optimisation"
    assert coord.battery_effective_operation_mode_sub_type == "prioritize-energy"
    assert coord.battery_profile_display == "AI Optimisation"
    assert coord.battery_profile_pending is False


def test_battery_runtime_parse_status_payload_aggregates_and_skips_excluded(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_status_payload(
        {
            "current_charge": "20%",
            "available_energy": 3,
            "max_capacity": 10,
            "available_power": 7.68,
            "max_power": 7.68,
            "included_count": 2,
            "excluded_count": 1,
            "storages": [
                {
                    "id": 1,
                    "serial_number": "BAT-1",
                    "current_charge": "40%",
                    "available_energy": 2,
                    "max_capacity": 5,
                    "status": "normal",
                    "statusText": "Normal",
                    "excluded": False,
                },
                {
                    "id": 2,
                    "serial_number": "BAT-2",
                    "current_charge": "20%",
                    "available_energy": 1,
                    "max_capacity": 5,
                    "status": "warning",
                    "statusText": "Warning",
                    "excluded": False,
                },
                {
                    "id": 3,
                    "serial_number": "BAT-3",
                    "current_charge": "99%",
                    "available_energy": 9,
                    "max_capacity": 10,
                    "status": "error",
                    "statusText": "Error",
                    "excluded": True,
                },
            ],
        }
    )

    assert coord.iter_battery_serials() == ["BAT-1", "BAT-2"]
    assert coord.battery_storage("BAT-1")["current_charge_pct"] == 40
    assert coord.battery_storage("BAT-1")["id"] == "1"
    assert coord.battery_storage("BAT-1")["battery_id"] == "1"
    assert coord.battery_storage("BAT-3") is None
    assert coord.battery_aggregate_charge_pct == 30.0
    assert coord.battery_aggregate_status == "warning"
    details = coord.battery_aggregate_status_details
    assert details["aggregate_charge_source"] == "computed"
    assert details["included_count"] == 2
    assert details["contributing_count"] == 2
    assert details["missing_energy_capacity_keys"] == []
    assert details["excluded_count"] == 1
    assert details["per_battery_status"]["BAT-1"] == "normal"
    assert details["per_battery_status"]["BAT-2"] == "warning"
    assert details["worst_storage_key"] == "BAT-2"


def test_battery_runtime_parse_status_payload_rounds_kwh_fields(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_status_payload(
        {
            "available_energy": "1.239",
            "max_capacity": "2.996",
            "storages": [
                {
                    "serial_number": "BAT-1",
                    "available_energy": "1.239",
                    "max_capacity": "2.996",
                    "status": "normal",
                    "excluded": False,
                }
            ],
        }
    )

    snapshot = coord.battery_storage("BAT-1")
    assert snapshot is not None
    assert snapshot["available_energy_kwh"] == 1.24
    assert snapshot["max_capacity_kwh"] == 3.0
    details = coord.battery_aggregate_status_details
    assert details["available_energy_kwh"] == 1.24
    assert details["max_capacity_kwh"] == 3.0
    assert details["site_available_energy_kwh"] == 1.24
    assert details["site_max_capacity_kwh"] == 3.0


def test_battery_runtime_parse_status_payload_site_soc_fallbacks(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_status_payload(
        {
            "current_charge": "48%",
            "storages": [
                {
                    "id": 1,
                    "serial_number": "BAT-1",
                    "current_charge": "48%",
                    "available_energy": None,
                    "max_capacity": None,
                    "status": "normal",
                    "excluded": False,
                }
            ],
        }
    )

    assert coord.battery_aggregate_charge_pct == 48.0
    assert coord.battery_aggregate_status == "normal"
    details = coord.battery_aggregate_status_details
    assert details["aggregate_charge_source"] == "site_current_charge"
    assert details["included_count"] == 1
    assert details["contributing_count"] == 0
    assert details["missing_energy_capacity_keys"] == ["BAT-1"]

    coord.battery_runtime.parse_battery_status_payload(
        {
            "current_charge": "55%",
            "storages": [
                {
                    "id": 1,
                    "serial_number": "BAT-1",
                    "current_charge": "40%",
                    "available_energy": 2.0,
                    "max_capacity": 5.0,
                    "status": "normal",
                    "excluded": False,
                },
                {
                    "id": 2,
                    "serial_number": "BAT-2",
                    "current_charge": "70%",
                    "available_energy": None,
                    "max_capacity": 5.0,
                    "status": "normal",
                    "excluded": False,
                },
            ],
        }
    )

    assert coord.battery_aggregate_charge_pct == 55.0
    details = coord.battery_aggregate_status_details
    assert details["aggregate_charge_source"] == "site_current_charge"
    assert details["included_count"] == 2
    assert details["contributing_count"] == 1
    assert details["missing_energy_capacity_keys"] == ["BAT-2"]

    coord.battery_runtime.parse_battery_status_payload(
        {
            "storages": [
                {
                    "id": 1,
                    "serial_number": "BAT-1",
                    "current_charge": "40%",
                    "available_energy": 2.0,
                    "max_capacity": 5.0,
                    "status": "normal",
                    "excluded": False,
                },
                {
                    "id": 2,
                    "serial_number": "BAT-2",
                    "current_charge": "70%",
                    "available_energy": None,
                    "max_capacity": 5.0,
                    "status": "normal",
                    "excluded": False,
                },
            ],
        }
    )

    assert coord.battery_aggregate_charge_pct is None
    details = coord.battery_aggregate_status_details
    assert details["aggregate_charge_source"] == "unknown"
    assert details["included_count"] == 2
    assert details["contributing_count"] == 1
    assert details["missing_energy_capacity_keys"] == ["BAT-2"]


def test_battery_runtime_parse_status_payload_edge_shapes(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_status_payload("bad")
    assert coord.iter_battery_serials() == []
    assert coord.battery_aggregate_status is None

    coord.battery_runtime.parse_battery_status_payload(
        {
            "current_charge": "12%",
            "storages": [
                "bad",
                {"excluded": False},
                {"id": 9, "excluded": False, "statusText": "Unknown"},
                {
                    "id": "10",
                    "serial_number": "BAT-10",
                    "current_charge": "15%",
                    "available_energy": 0.5,
                    "max_capacity": 1.0,
                    "status": None,
                    "statusText": None,
                    "excluded": False,
                },
            ],
        }
    )
    assert "id_9" in coord.iter_battery_serials()
    assert coord.battery_storage("id_9")["status_normalized"] == "unknown"
    assert coord.battery_storage("id_9")["id"] == "9"
    assert coord.battery_aggregate_charge_pct == 12.0
    details = coord.battery_aggregate_status_details
    assert details["aggregate_charge_source"] == "site_current_charge"
    assert details["contributing_count"] == 1
    assert details["missing_energy_capacity_keys"] == ["id_9"]
    assert coord.battery_aggregate_status == "unknown"


def test_battery_runtime_parse_status_payload_prefers_status_text_when_raw_unknown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_status_payload(
        {
            "storages": [
                {
                    "serial_number": "BAT-1",
                    "current_charge": "50%",
                    "available_energy": 2.5,
                    "max_capacity": 5.0,
                    "status": "mystery_code",
                    "statusText": "Normal",
                    "excluded": False,
                }
            ]
        }
    )

    snapshot = coord.battery_storage("BAT-1")
    assert snapshot is not None
    assert snapshot["status_normalized"] == "normal"
    assert coord.battery_aggregate_status == "normal"


def test_battery_runtime_parse_site_settings_payload_sets_has_acb(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.battery_runtime.parse_battery_site_settings_payload(
        {"data": {"hasAcb": True, "hasEncharge": False}}
    )

    assert coord.battery_has_acb is True
    assert coord.battery_has_encharge is False


def test_battery_runtime_parse_ac_battery_devices_and_telemetry(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._battery_timezone = "Australia/Melbourne"  # noqa: SLF001

    devices_html = """
    <table id="ac_batteries">
      <tr>
        <td><a href="/systems/12345/ac_batteries/67890">BAT-AC-1</a></td>
        <td>ACB-Model</td>
        <td>Single Phase</td>
        <td>48%</td>
        <td>123</td>
        <td>Normal</td>
        <td><a class="sleep">Sleep Mode Off</a></td>
        <td>
          <select>
            <option value="20" selected="selected">20-25%</option>
          </select>
        </td>
      </tr>
    </table>
    """
    telemetry_html = """
    <div>
      Last Report
      <span class="formatted-value">0.26</span>
      <span class="units">kW</span>
      <span>(Charging)</span>
      State of Charge <span class="value">49%</span>
      Charge Cycles <span class="value">124</span>
      04/09/2026 01:15 PM
    </div>
    """

    coord.battery_runtime.parse_ac_battery_devices_page(devices_html)
    snapshot = coord.ac_battery_storage("BAT-AC-1")
    assert snapshot is not None
    assert snapshot["battery_id"] == "67890"
    assert snapshot["status_normalized"] == "normal"
    assert coord.ac_battery_selected_sleep_min_soc == 20
    assert coord.ac_battery_sleep_state == "off"

    merged = coord.battery_runtime.parse_ac_battery_show_stat_data(
        "BAT-AC-1", "67890", telemetry_html
    )
    coord._ac_battery_data = {"BAT-AC-1": merged}  # noqa: SLF001
    coord._ac_battery_order = ["BAT-AC-1"]  # noqa: SLF001
    coord.battery_runtime._refresh_ac_battery_summary()  # noqa: SLF001

    snapshot = coord.ac_battery_storage("BAT-AC-1")
    assert snapshot is not None
    assert snapshot["power_w"] == 260.0
    assert snapshot["operating_mode"] == "Charging"
    assert snapshot["current_charge_pct"] == 49.0
    assert snapshot["cycle_count"] == 124
    assert coord.ac_battery_status_summary["power_w"] == 260.0
    assert coord.ac_battery_summary_sample_utc is not None


def test_ac_battery_runtime_helper_branches(coordinator_factory, monkeypatch) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime._ac_battery_runtime  # noqa: SLF001

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert runtime._ac_battery_text(None) is None  # noqa: SLF001
    assert runtime._ac_battery_text(BadStr()) is None  # noqa: SLF001
    assert runtime._ac_battery_sleep_state(None) is None  # noqa: SLF001
    assert runtime._ac_battery_sleep_state("cancel") == "pending"  # noqa: SLF001
    assert runtime._ac_battery_sleep_state("wake") == "on"  # noqa: SLF001
    assert runtime._ac_battery_sleep_state("other") is None  # noqa: SLF001
    assert runtime._ac_battery_parse_timestamp(None) is None  # noqa: SLF001
    assert runtime._ac_battery_parse_timestamp("bad timestamp") is None  # noqa: SLF001
    assert (
        runtime._ac_battery_parse_timestamp("13/40/2026 01:15 PM") is None
    )  # noqa: SLF001

    coord._battery_timezone = "Invalid/Timezone"  # noqa: SLF001
    fallback_timestamp = runtime._ac_battery_parse_timestamp(  # noqa: SLF001
        "04/09/2026 01:15 PM"
    )
    assert fallback_timestamp == datetime(2026, 4, 9, 13, 15, tzinfo=UTC)

    assert runtime._ac_battery_parse_float(None) is None  # noqa: SLF001
    assert runtime._ac_battery_parse_float("abc") is None  # noqa: SLF001
    assert runtime._ac_battery_parse_float("1.2.3") is None  # noqa: SLF001
    assert runtime._ac_battery_parse_int(None) is None  # noqa: SLF001

    monkeypatch.setattr(
        runtime, "_ac_battery_parse_float", lambda _value: object(), raising=False
    )
    assert runtime._ac_battery_parse_int("1") is None  # noqa: SLF001
    assert runtime._ac_battery_key() is None  # noqa: SLF001

    coord._ac_battery_aggregate_status_details = None  # noqa: SLF001
    assert runtime._preserve_ac_battery_status_fallback() is False  # noqa: SLF001
    coord._ac_battery_aggregate_status_details = {  # noqa: SLF001
        "status_source": "battery_status"
    }
    coord._ac_battery_data = None  # noqa: SLF001
    assert runtime._preserve_ac_battery_status_fallback() is False  # noqa: SLF001
    coord._ac_battery_data = {"bad": "not-a-dict"}  # noqa: SLF001
    assert runtime._preserve_ac_battery_status_fallback() is False  # noqa: SLF001

    coord._ac_battery_data = {"BAT-AC-1": {"serial_number": "BAT-AC-1"}}  # noqa: SLF001
    coord._ac_battery_order = []  # noqa: SLF001
    coord.battery_runtime.parse_ac_battery_devices_page(None)
    assert coord.iter_ac_battery_serials() == ["BAT-AC-1"]


def test_ac_battery_runtime_parse_devices_page_edge_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runtime = coord.battery_runtime._ac_battery_runtime  # noqa: SLF001
    coord._ac_battery_selected_sleep_min_soc = 35  # noqa: SLF001

    coord.battery_runtime.parse_ac_battery_devices_page(None)
    assert coord.iter_ac_battery_serials() == []
    assert coord.ac_battery_selected_sleep_min_soc is None

    html = """
    <table id="ac_batteries">
      <tr><td>short</td></tr>
      <tr>
        <td> </td>
        <td>ACB-Model</td>
        <td>Single Phase</td>
        <td>47%</td>
        <td>122</td>
        <td>Normal</td>
        <td><a class="wake">Wake</a></td>
      </tr>
      <tr>
        <td><a href="/systems/12345/ac_batteries/11111">BAT-AC-1</a></td>
        <td>ACB-Model</td>
        <td>Single Phase</td>
        <td>48%</td>
        <td>123</td>
        <td>Unknown state</td>
        <td><a class="sleep">Sleep</a></td>
      </tr>
      <tr>
        <td><a href="/systems/12345/ac_batteries/22222">BAT-AC-2</a></td>
        <td>ACB-Model</td>
        <td>Single Phase</td>
        <td>50%</td>
        <td>124</td>
        <td>Normal</td>
        <td><a class="wake">Wake</a></td>
      </tr>
      <tr>
        <td><a href="/systems/12345/ac_batteries/33333"></a></td>
        <td>ACB-Model</td>
        <td>Single Phase</td>
        <td>51%</td>
        <td>125</td>
        <td>Normal</td>
        <td><a class="cancel">Cancel</a></td>
      </tr>
      <tr>
        <td><a href="/systems/12345/ac_batteries/44444">BAT-AC-4</a></td>
        <td>ACB-Model</td>
        <td>Single Phase</td>
        <td>52%</td>
        <td>126</td>
        <td> </td>
        <td><a class="sleep">Sleep</a></td>
      </tr>
    </table>
    """

    runtime.parse_ac_battery_devices_page(html)

    assert coord.iter_ac_battery_serials() == [
        "BAT-AC-1",
        "BAT-AC-2",
        "id_33333",
        "BAT-AC-4",
    ]
    assert coord.ac_battery_aggregate_status == "unknown"
    assert coord.ac_battery_sleep_state == "pending"
    assert coord.ac_battery_status_summary["sleep_state_map"]["id_33333"] == "pending"
    assert coord.ac_battery_storage("BAT-AC-4")["status_normalized"] == "unknown"

    mixed_sleep_html = """
        <table id="ac_batteries">
          <tr><td><a href="/systems/12345/ac_batteries/1">BAT-1</a></td><td>x</td><td>x</td><td>1%</td><td>1</td><td>Normal</td><td><a class="sleep">Sleep</a></td></tr>
          <tr><td><a href="/systems/12345/ac_batteries/2">BAT-2</a></td><td>x</td><td>x</td><td>1%</td><td>1</td><td>Normal</td><td><a class="wake">Wake</a></td></tr>
        </table>
        """
    runtime.parse_ac_battery_devices_page(mixed_sleep_html)
    assert coord.ac_battery_sleep_state == "mixed"


def test_ac_battery_runtime_parse_show_stat_data_and_summary_edge_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._ac_battery_data = {  # noqa: SLF001
        "BAT-AC-1": {"serial_number": "BAT-AC-1"},
        "bad": "not-a-dict",
    }
    coord._ac_battery_order = ["bad", "BAT-AC-1"]  # noqa: SLF001

    merged = coord.battery_runtime.parse_ac_battery_show_stat_data(
        "BAT-AC-1", "67890", ""
    )
    assert merged["battery_id"] == "67890"

    coord._ac_battery_data["BAT-AC-1"] = {  # noqa: SLF001
        "serial_number": "BAT-AC-1",
        "power_w": 100,
        "last_reported": datetime(2026, 4, 9, 1, 0, tzinfo=UTC),
    }
    coord.battery_runtime._refresh_ac_battery_summary()  # noqa: SLF001
    assert coord.ac_battery_status_summary["power_w"] == 100.0

    coord._ac_battery_order = []  # noqa: SLF001
    coord.battery_runtime._refresh_ac_battery_summary()  # noqa: SLF001
    assert coord.ac_battery_status_summary["power_w"] is None


@pytest.mark.asyncio
async def test_battery_runtime_refresh_and_control_ac_battery(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001

    devices_html = """
    <table id="ac_batteries">
      <tr>
        <td><a href="/systems/12345/ac_batteries/67890">BAT-AC-1</a></td>
        <td>ACB-Model</td>
        <td>Single Phase</td>
        <td>48%</td>
        <td>123</td>
        <td>Warning</td>
        <td><a class="wake">Sleep Mode On</a></td>
        <td>
          <select><option value="25" selected="selected">25-30%</option></select>
        </td>
      </tr>
    </table>
    """
    telemetry_html = """
    <div>
      Last Report
      <span class="formatted-value">120</span>
      <span class="units">W</span>
      <span>(Discharging)</span>
      State of Charge <span class="value">48%</span>
      Charge Cycles <span class="value">123</span>
      04/09/2026 01:15 PM
    </div>
    """
    coord.client.ac_battery_devices_page = AsyncMock(return_value=devices_html)
    coord.client.ac_battery_show_stat_data = AsyncMock(return_value=telemetry_html)
    coord.client.ac_battery_events_page = AsyncMock(return_value="<html>events</html>")
    coord.client.set_ac_battery_sleep = AsyncMock(
        return_value=SimpleNamespace(status=302, location="/systems/12345/devices")
    )
    coord.client.set_ac_battery_wake = AsyncMock(
        return_value=SimpleNamespace(status=302, location="/systems/12345/devices")
    )

    await coord.battery_runtime.async_refresh_ac_battery_devices()
    await coord.battery_runtime.async_refresh_ac_battery_telemetry()
    await coord.battery_runtime.async_refresh_ac_battery_events(force=True)

    assert coord.iter_ac_battery_serials() == ["BAT-AC-1"]
    assert coord.ac_battery_storage("BAT-AC-1")["power_w"] == 120.0
    assert (
        coord._ac_battery_events_payloads["records"][0]["html_excerpt"]
        == "<html>events</html>"
    )  # noqa: SLF001

    await coord.battery_runtime.async_set_ac_battery_target_soc(30)
    coord.client.set_ac_battery_sleep.assert_awaited_with("67890", 30)

    await coord.battery_runtime.async_set_ac_battery_sleep_mode(False)
    coord.client.set_ac_battery_wake.assert_awaited_with("67890")
    assert coord._ac_battery_last_command["action"] == "wake"  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_runtime_ac_battery_refresh_guard_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001

    runtime = coord.battery_runtime
    coord._ac_battery_data = {"BAT-AC-1": {"battery_id": "67890"}}  # noqa: SLF001
    coord._ac_battery_order = ["BAT-AC-1"]  # noqa: SLF001

    coord._selected_type_keys = set()  # noqa: SLF001
    await runtime.async_refresh_ac_battery_devices(force=True)
    assert coord.iter_ac_battery_serials() == []

    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._ac_battery_devices_cache_until = time.monotonic() + 60  # noqa: SLF001
    coord.client.ac_battery_devices_page = AsyncMock()
    await runtime.async_refresh_ac_battery_devices()
    coord.client.ac_battery_devices_page.assert_not_called()

    coord._ac_battery_devices_cache_until = None  # noqa: SLF001
    coord.client.ac_battery_devices_page = None
    await runtime.async_refresh_ac_battery_devices(force=True)

    coord.client.ac_battery_devices_page = AsyncMock(return_value="<html></html>")
    coord._endpoint_family_should_run = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    coord._endpoint_family_can_use_stale = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    await runtime.async_refresh_ac_battery_devices(force=True)
    assert coord.iter_ac_battery_serials() == []

    coord._endpoint_family_should_run = lambda *_args, **_kwargs: True  # type: ignore[method-assign]  # noqa: SLF001
    coord.redact_battery_payload = lambda _payload: ["not-a-dict"]  # type: ignore[assignment]
    await runtime.async_refresh_ac_battery_devices(force=True)
    assert coord._ac_battery_devices_payload == {
        "value": ["not-a-dict"]
    }  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_runtime_ac_battery_device_refresh_failure_clears_stale_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._ac_battery_data = {  # noqa: SLF001
        "BAT-AC-1": {"serial_number": "BAT-AC-1", "battery_id": "67890"}
    }
    coord._ac_battery_order = ["BAT-AC-1"]  # noqa: SLF001
    coord._ac_battery_aggregate_status = "warning"  # noqa: SLF001
    coord._ac_battery_aggregate_status_details = {"battery_count": 1}  # noqa: SLF001
    coord._ac_battery_devices_payload = {
        "records": [{"serial_number": "BAT-AC-1"}]
    }  # noqa: SLF001
    coord._ac_battery_devices_html_payload = {
        "records": [{"serial_number": "BAT-AC-1"}]
    }  # noqa: SLF001
    coord.client.ac_battery_devices_page = AsyncMock(side_effect=RuntimeError("boom"))

    await coord.battery_runtime.async_refresh_ac_battery_devices(force=True)

    health = coord._endpoint_family_state("ac_battery_devices")  # noqa: SLF001
    assert health.consecutive_failures == 1
    assert coord.iter_ac_battery_serials() == []
    assert coord.ac_battery_aggregate_status is None
    assert coord._ac_battery_devices_payload is None  # noqa: SLF001
    assert coord._ac_battery_devices_html_payload is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_runtime_ac_battery_telemetry_refresh_failure_clears_only_telemetry(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._ac_battery_data = {  # noqa: SLF001
        "BAT-AC-1": {
            "serial_number": "BAT-AC-1",
            "battery_id": "67890",
            "current_charge_pct": 48.0,
            "cycle_count": 123,
            "power_w": 120.0,
            "operating_mode": "Discharging",
            "last_reported": datetime(2026, 4, 9, 3, 15, tzinfo=UTC),
        }
    }
    coord._ac_battery_order = ["BAT-AC-1"]  # noqa: SLF001
    coord._ac_battery_aggregate_status_details = {  # noqa: SLF001
        "battery_count": 1,
        "power_w": 120.0,
        "power_map_w": {"BAT-AC-1": 120.0},
        "reporting_count": 1,
        "latest_reported_utc": "2026-04-09T03:15:00+00:00",
    }
    coord._ac_battery_power_w = 120.0  # noqa: SLF001
    coord._ac_battery_summary_sample_utc = datetime(
        2026, 4, 9, 3, 20, tzinfo=UTC
    )  # noqa: SLF001
    coord._ac_battery_telemetry_payloads = {
        "records": [{"serial": "BAT-AC-1"}]
    }  # noqa: SLF001
    coord.client.ac_battery_show_stat_data = AsyncMock(
        side_effect=RuntimeError("telemetry boom")
    )

    await coord.battery_runtime.async_refresh_ac_battery_telemetry(force=True)

    health = coord._endpoint_family_state("ac_battery_telemetry")  # noqa: SLF001
    assert health.consecutive_failures == 1
    snapshot = coord.ac_battery_storage("BAT-AC-1")
    assert snapshot is not None
    assert snapshot["current_charge_pct"] == 48.0
    assert snapshot["cycle_count"] == 123
    assert "power_w" not in snapshot
    assert "operating_mode" not in snapshot
    assert "last_reported" not in snapshot
    assert coord.ac_battery_status_summary["power_w"] is None
    assert coord._ac_battery_telemetry_payloads is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_runtime_ac_battery_telemetry_and_events_guard_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._ac_battery_data = {  # noqa: SLF001
        "bad": "not-a-dict",
        "missing-id": {"serial_number": "missing-id"},
        "BAT-AC-1": {"serial_number": "BAT-AC-1", "battery_id": "67890"},
    }
    coord._ac_battery_order = ["bad", "missing-id", "BAT-AC-1"]  # noqa: SLF001
    coord.client.ac_battery_show_stat_data = AsyncMock(return_value="<div>ok</div>")
    coord.redact_battery_payload = lambda _payload: {"not": "a-list"}  # type: ignore[assignment]

    coord._ac_battery_telemetry_cache_until = time.monotonic() + 60  # noqa: SLF001
    await coord.battery_runtime.async_refresh_ac_battery_telemetry()
    coord.client.ac_battery_show_stat_data.assert_not_called()

    coord._ac_battery_telemetry_cache_until = None  # noqa: SLF001
    coord.client.ac_battery_show_stat_data = None
    await coord.battery_runtime.async_refresh_ac_battery_telemetry(force=True)

    coord.client.ac_battery_show_stat_data = AsyncMock(return_value="<div>ok</div>")
    await coord.battery_runtime.async_refresh_ac_battery_telemetry(force=True)
    assert coord._ac_battery_telemetry_payloads == {
        "value": {"not": "a-list"}
    }  # noqa: SLF001

    coord._endpoint_family_should_run = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    coord._endpoint_family_can_use_stale = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    await coord.battery_runtime.async_refresh_ac_battery_telemetry(force=True)
    assert coord._ac_battery_power_w is None  # noqa: SLF001

    coord._endpoint_family_should_run = lambda *_args, **_kwargs: True  # type: ignore[method-assign]  # noqa: SLF001
    coord.client.ac_battery_events_page = None
    await coord.battery_runtime.async_refresh_ac_battery_events(force=True)
    assert coord._ac_battery_events_payloads is None  # noqa: SLF001

    coord.client.ac_battery_events_page = AsyncMock(return_value="<html>events</html>")
    coord.redact_battery_payload = lambda _payload: {"bad": "shape"}  # type: ignore[assignment]
    await coord.battery_runtime.async_refresh_ac_battery_events(force=True)
    assert coord._ac_battery_events_payloads == {
        "value": {"bad": "shape"}
    }  # noqa: SLF001

    coord._endpoint_family_should_run = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    coord._endpoint_family_can_use_stale = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    await coord.battery_runtime.async_refresh_ac_battery_events(force=True)
    assert coord._ac_battery_events_payloads is None  # noqa: SLF001

    coord._endpoint_family_should_run = lambda *_args, **_kwargs: True  # type: ignore[method-assign]  # noqa: SLF001
    coord.client.ac_battery_events_page = AsyncMock(side_effect=RuntimeError("events"))
    await coord.battery_runtime.async_refresh_ac_battery_events(force=True)
    assert coord._ac_battery_events_payloads is None  # noqa: SLF001

    coord._selected_type_keys = {"envoy"}  # noqa: SLF001
    await coord.battery_runtime.async_refresh_ac_battery_telemetry(force=True)
    await coord.battery_runtime.async_refresh_ac_battery_events(force=True)
    assert coord._ac_battery_telemetry_payloads is None  # noqa: SLF001
    assert coord._ac_battery_events_payloads is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_runtime_ac_battery_sleep_mode_edge_branches(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord.client.set_ac_battery_sleep = AsyncMock(
        return_value=SimpleNamespace(status=302, location="/systems/12345/devices")
    )
    coord.client.set_ac_battery_wake = AsyncMock(
        return_value=SimpleNamespace(status=302, location="/systems/12345/devices")
    )
    coord.battery_runtime.async_refresh_ac_battery_devices = AsyncMock()
    coord.battery_runtime.async_refresh_ac_battery_telemetry = AsyncMock()

    with pytest.raises(ServiceValidationError, match="No AC Battery devices"):
        await coord.battery_runtime.async_set_ac_battery_sleep_mode(True)

    coord._ac_battery_data = {  # noqa: SLF001
        "bad": "value",
        "missing-id": {"serial_number": "missing-id"},
        "BAT-AC-1": {"serial_number": "BAT-AC-1", "battery_id": "67890"},
    }
    coord._ac_battery_selected_sleep_min_soc = None  # noqa: SLF001
    await coord.battery_runtime.async_set_ac_battery_sleep_mode(True)

    coord.client.set_ac_battery_sleep.assert_awaited_once_with("67890", 20)
    assert coord._ac_battery_last_command["sleep_min_soc"] == 20  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_runtime_ac_battery_target_soc_triggers_sleep_when_active(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._ac_battery_sleep_state = "pending"  # noqa: SLF001
    coord.battery_runtime._ac_battery_runtime.async_set_ac_battery_sleep_mode = (
        AsyncMock()
    )  # noqa: SLF001

    await coord.battery_runtime.async_set_ac_battery_target_soc(30)

    assert coord._ac_battery_selected_sleep_min_soc == 30  # noqa: SLF001
    coord.battery_runtime._ac_battery_runtime.async_set_ac_battery_sleep_mode.assert_awaited_once_with(
        True
    )  # noqa: SLF001


def test_coordinator_ac_battery_property_edge_branches(coordinator_factory) -> None:
    coord = coordinator_factory()

    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord._ac_battery_aggregate_status = BadStr()  # noqa: SLF001
    coord._ac_battery_sleep_state = BadStr()  # noqa: SLF001
    coord._ac_battery_order = [
        "BAT-1",
        "MISSING",
        BadStr(),
        "BAT-2",
        "",
    ]  # noqa: SLF001
    coord._ac_battery_data = {"BAT-1": {}, "BAT-2": {}}  # noqa: SLF001

    assert coord.ac_battery_aggregate_status is None
    assert coord.ac_battery_sleep_state is None
    assert coord.iter_ac_battery_serials() == ["BAT-1", "BAT-2"]

    coord._ac_battery_data = None  # type: ignore[assignment]  # noqa: SLF001
    assert coord.ac_battery_storage("BAT-1") is None

    coord._ac_battery_data = {"BAT-1": {}}  # noqa: SLF001
    assert coord.ac_battery_storage(BadStr()) is None
    assert coord.ac_battery_storage("") is None
    assert coord.ac_battery_storage("missing") is None


@pytest.mark.asyncio
async def test_coordinator_ac_battery_diagnostic_and_control_delegates(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._ac_battery_devices_payload = {}  # noqa: SLF001
    coord._ac_battery_telemetry_payloads = {}  # noqa: SLF001
    coord._ac_battery_events_payloads = {}  # noqa: SLF001
    coord.battery_runtime.async_refresh_ac_battery_devices = AsyncMock()
    coord.battery_runtime.async_refresh_ac_battery_telemetry = AsyncMock()
    coord.battery_runtime.async_refresh_ac_battery_events = AsyncMock()
    coord.battery_runtime.async_set_ac_battery_sleep_mode = AsyncMock()
    coord.battery_runtime.async_set_ac_battery_target_soc = AsyncMock()

    await coord.async_ensure_ac_battery_diagnostics()
    coord.battery_runtime.async_refresh_ac_battery_devices.assert_not_called()

    coord._ac_battery_events_payloads = None  # noqa: SLF001
    await coord.async_ensure_ac_battery_diagnostics()
    coord.battery_runtime.async_refresh_ac_battery_devices.assert_awaited_once_with(
        force=True
    )
    coord.battery_runtime.async_refresh_ac_battery_telemetry.assert_awaited_once_with(
        force=True
    )
    coord.battery_runtime.async_refresh_ac_battery_events.assert_awaited_once_with(
        force=True
    )

    await coord.async_set_ac_battery_sleep_mode(True)
    await coord.async_set_ac_battery_target_soc(25)
    coord.battery_runtime.async_set_ac_battery_sleep_mode.assert_awaited_with(True)
    coord.battery_runtime.async_set_ac_battery_target_soc.assert_awaited_with(25)


@pytest.mark.asyncio
async def test_battery_runtime_refresh_status_stores_redacted_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_status = AsyncMock(
        return_value={
            "current_charge": "48%",
            "token": "secret",
            "storages": [
                {"serial_number": "BAT-1", "current_charge": "48%", "excluded": False}
            ],
        }
    )

    await coord.battery_runtime.async_refresh_battery_status()

    assert coord.battery_status_payload is not None
    assert coord.battery_status_payload["token"] == "[redacted]"
    assert coord.iter_battery_serials() == ["BAT-1"]


@pytest.mark.asyncio
async def test_battery_runtime_refresh_status_wraps_non_dict_redacted_payload(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_status = AsyncMock(return_value=["unexpected"])  # type: ignore[list-item]

    await coord.battery_runtime.async_refresh_battery_status()

    assert coord.battery_status_payload == {"value": ["unexpected"]}


@pytest.mark.asyncio
async def test_battery_runtime_refresh_status_uses_success_cache_ttl(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.client.battery_status = AsyncMock(
        return_value={"storages": [{"serial_number": "BAT-1", "current_charge": 48}]}
    )

    await coord.battery_runtime.async_refresh_battery_status()

    assert coord.client.battery_status.await_count == 1
    assert coord._battery_status_cache_until is not None  # noqa: SLF001

    await coord.battery_runtime.async_refresh_battery_status()

    assert coord.client.battery_status.await_count == 1


@pytest.mark.asyncio
async def test_battery_runtime_refresh_status_skips_during_endpoint_cooldown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    health = coord._endpoint_family_state("battery_status")  # noqa: SLF001
    health.next_retry_mono = time.monotonic() + 300
    health.cooldown_active = True
    coord.client.battery_status = AsyncMock(side_effect=AssertionError("no fetch"))

    await coord.battery_runtime.async_refresh_battery_status()

    coord.client.battery_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_battery_runtime_optional_refreshes_respect_cooldown_and_clear_stale_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    backup_health = coord._endpoint_family_state(
        "battery_backup_history"
    )  # noqa: SLF001
    backup_health.next_retry_mono = time.monotonic() + 300
    backup_health.cooldown_active = True
    coord._battery_backup_history_cache_until = None  # noqa: SLF001
    coord.client.battery_backup_history = AsyncMock(
        side_effect=AssertionError("unused")
    )
    await coord.battery_runtime.async_refresh_battery_backup_history()
    coord.client.battery_backup_history.assert_not_awaited()

    site_health = coord._endpoint_family_state("battery_site_settings")  # noqa: SLF001
    site_health.next_retry_mono = time.monotonic() + 300
    site_health.cooldown_active = True
    coord._battery_site_settings_cache_until = None  # noqa: SLF001
    coord.client.battery_site_settings = AsyncMock(side_effect=AssertionError("unused"))
    await coord.battery_runtime.async_refresh_battery_site_settings()
    coord.client.battery_site_settings.assert_not_awaited()

    grid_health = coord._endpoint_family_state("grid_control_check")  # noqa: SLF001
    grid_health.next_retry_mono = time.monotonic() + 300
    grid_health.cooldown_active = True
    grid_health.last_success_mono = time.monotonic() - 500
    coord._grid_control_check_cache_until = None  # noqa: SLF001
    coord._grid_control_supported = True  # noqa: SLF001
    coord._grid_control_disable = False  # noqa: SLF001
    coord._grid_control_active_download = True  # noqa: SLF001
    coord._grid_control_sunlight_backup_system_check = True  # noqa: SLF001
    coord._grid_control_grid_outage_check = True  # noqa: SLF001
    coord._grid_control_user_initiated_toggle = True  # noqa: SLF001
    await coord.battery_runtime.async_refresh_grid_control_check(force=True)
    assert coord._grid_control_supported is None  # noqa: SLF001
    assert coord._grid_control_disable is None  # noqa: SLF001
    assert coord._grid_control_active_download is None  # noqa: SLF001

    dry_health = coord._endpoint_family_state("dry_contact_settings")  # noqa: SLF001
    dry_health.next_retry_mono = time.monotonic() + 300
    dry_health.cooldown_active = True
    dry_health.last_success_mono = time.monotonic() - 2_000
    coord._dry_contact_settings_cache_until = None  # noqa: SLF001
    coord._dry_contact_settings_supported = True  # noqa: SLF001
    await coord.battery_runtime.async_refresh_dry_contact_settings()
    assert coord._dry_contact_settings_supported is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_battery_runtime_grid_control_refresh_keeps_recent_state_during_cooldown(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    grid_health = coord._endpoint_family_state("grid_control_check")  # noqa: SLF001
    grid_health.next_retry_mono = time.monotonic() + 300
    grid_health.cooldown_active = True
    grid_health.last_success_mono = time.monotonic() - 240
    coord._grid_control_check_last_success_mono = (
        grid_health.last_success_mono
    )  # noqa: SLF001
    coord._grid_control_check_cache_until = None  # noqa: SLF001
    coord._grid_control_supported = True  # noqa: SLF001
    coord._grid_control_disable = False  # noqa: SLF001
    coord._grid_control_active_download = False  # noqa: SLF001
    coord._grid_control_sunlight_backup_system_check = False  # noqa: SLF001
    coord._grid_control_grid_outage_check = False  # noqa: SLF001
    coord._grid_control_user_initiated_toggle = False  # noqa: SLF001

    await coord.battery_runtime.async_refresh_grid_control_check(force=True)

    assert coord._grid_control_supported is True  # noqa: SLF001
    assert coord._grid_control_disable is False  # noqa: SLF001
    assert coord._grid_control_active_download is False  # noqa: SLF001


def test_battery_runtime_grid_control_refresh_due_requests_state_invalidation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    grid_health = coord._endpoint_family_state("grid_control_check")  # noqa: SLF001
    grid_health.next_retry_mono = time.monotonic() + 300
    grid_health.cooldown_active = True
    grid_health.last_success_mono = time.monotonic() - 500
    coord._grid_control_check_cache_until = None  # noqa: SLF001
    coord._grid_control_supported = True  # noqa: SLF001
    coord.client.grid_control_check = AsyncMock(side_effect=AssertionError("unused"))

    assert coord.battery_runtime.grid_control_check_refresh_due() is False
    assert coord.battery_runtime.grid_control_check_refresh_due(force=True) is True

    coord.client.grid_control_check = None
    assert coord.battery_runtime.grid_control_check_refresh_due(force=True) is True

    coord.client.grid_control_check = AsyncMock()
    coord._endpoint_family_should_run = lambda *args, **kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    coord._endpoint_family_can_use_stale = lambda *args, **kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    assert coord.battery_runtime.grid_control_check_refresh_due(force=True) is True


@pytest.mark.asyncio
async def test_battery_runtime_forced_grid_control_refresh_clears_expired_state(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._grid_control_supported = True  # noqa: SLF001
    coord._grid_control_disable = False  # noqa: SLF001
    coord._grid_control_active_download = True  # noqa: SLF001
    coord._grid_control_sunlight_backup_system_check = True  # noqa: SLF001
    coord._grid_control_grid_outage_check = True  # noqa: SLF001
    coord._grid_control_user_initiated_toggle = True  # noqa: SLF001
    health = coord._endpoint_family_state("grid_control_check")  # noqa: SLF001
    health.cooldown_active = True
    coord._endpoint_family_should_run = lambda *args, **kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    coord._endpoint_family_can_use_stale = lambda *args, **kwargs: False  # type: ignore[method-assign]  # noqa: SLF001

    await coord.battery_runtime.async_refresh_grid_control_check(force=True)

    assert coord._grid_control_supported is None  # noqa: SLF001
    assert coord._grid_control_disable is None  # noqa: SLF001
    assert coord._grid_control_active_download is None  # noqa: SLF001
    assert coord._grid_control_sunlight_backup_system_check is None  # noqa: SLF001
    assert coord._grid_control_grid_outage_check is None  # noqa: SLF001
    assert coord._grid_control_user_initiated_toggle is None  # noqa: SLF001


def test_battery_runtime_dry_contact_refresh_due_requests_state_invalidation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    dry_health = coord._endpoint_family_state("dry_contact_settings")  # noqa: SLF001
    dry_health.next_retry_mono = time.monotonic() + 300
    dry_health.cooldown_active = True
    dry_health.last_success_mono = time.monotonic() - 2_000
    coord._dry_contact_settings_cache_until = None  # noqa: SLF001
    coord._dry_contact_settings_supported = True  # noqa: SLF001
    coord.client.dry_contacts_settings = AsyncMock(side_effect=AssertionError("unused"))

    assert coord.battery_runtime.dry_contact_settings_refresh_due() is True


def test_battery_runtime_due_predicates_cover_cached_and_missing_fetcher_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    future = time.monotonic() + 300

    coord._battery_settings_cache_until = future  # noqa: SLF001
    assert coord.battery_runtime.battery_settings_refresh_due() is False

    coord._ac_battery_devices_cache_until = future  # noqa: SLF001
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord.client.ac_battery_devices_page = AsyncMock(
        side_effect=AssertionError("unused")
    )
    assert coord.battery_runtime.ac_battery_devices_refresh_due() is False


@pytest.mark.asyncio
async def test_battery_runtime_ac_battery_refresh_due_requests_cleanup(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._selected_type_keys = {"ac_battery"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._ac_battery_data = {  # noqa: SLF001
        "BAT-AC-1": {"serial_number": "BAT-AC-1", "battery_id": "67890"}
    }
    coord._ac_battery_order = ["BAT-AC-1"]  # noqa: SLF001
    coord._ac_battery_devices_payload = {
        "records": [{"serial_number": "BAT-AC-1"}]
    }  # noqa: SLF001
    health = coord._endpoint_family_state("ac_battery_devices")  # noqa: SLF001
    health.next_retry_mono = time.monotonic() + 300
    health.cooldown_active = True
    coord._endpoint_family_can_use_stale = lambda *_args, **_kwargs: False  # type: ignore[method-assign]  # noqa: SLF001
    coord.client.ac_battery_devices_page = AsyncMock(
        side_effect=AssertionError("unused")
    )

    assert coord.battery_runtime.ac_battery_devices_refresh_due() is True
    await coord.battery_runtime.async_refresh_ac_battery_devices()

    coord.client.ac_battery_devices_page.assert_not_called()
    assert coord.iter_ac_battery_serials() == []


@pytest.mark.asyncio
async def test_battery_runtime_optional_refreshes_handle_early_return_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    coord.client.battery_status = None
    await coord.battery_runtime.async_refresh_battery_status()

    coord.client.battery_backup_history = None
    await coord.battery_runtime.async_refresh_battery_backup_history()

    coord._endpoint_family_should_run = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda family, force=False: family != "battery_settings"
    )
    coord.client.battery_settings_details = AsyncMock(
        side_effect=AssertionError("unused")
    )
    await coord.battery_runtime.async_refresh_battery_settings()
    coord.client.battery_settings_details.assert_not_awaited()

    coord._endpoint_family_should_run = lambda *args, **kwargs: True  # type: ignore[method-assign]  # noqa: SLF001
    coord.client.battery_settings_details = None
    await coord.battery_runtime.async_refresh_battery_settings()

    coord._endpoint_family_should_run = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda family, force=False: family != "battery_schedules"
    )
    coord.client.battery_schedules = AsyncMock(side_effect=AssertionError("unused"))
    await coord.battery_runtime.async_refresh_battery_schedules()
    coord.client.battery_schedules.assert_not_awaited()

    coord._endpoint_family_should_run = lambda *args, **kwargs: True  # type: ignore[method-assign]  # noqa: SLF001
    coord.client.battery_schedules = None
    await coord.battery_runtime.async_refresh_battery_schedules()

    coord.client.battery_site_settings = None
    await coord.battery_runtime.async_refresh_battery_site_settings()

    coord._grid_control_supported = True  # noqa: SLF001
    coord._grid_control_disable = False  # noqa: SLF001
    coord._grid_control_active_download = True  # noqa: SLF001
    coord._grid_control_sunlight_backup_system_check = True  # noqa: SLF001
    coord._grid_control_grid_outage_check = True  # noqa: SLF001
    coord._grid_control_user_initiated_toggle = True  # noqa: SLF001
    coord.client.grid_control_check = None
    await coord.battery_runtime.async_refresh_grid_control_check(force=True)
    assert coord._grid_control_supported is None  # noqa: SLF001
    assert coord._grid_control_disable is None  # noqa: SLF001
    assert coord._grid_control_active_download is None  # noqa: SLF001
    assert coord._grid_control_sunlight_backup_system_check is None  # noqa: SLF001
    assert coord._grid_control_grid_outage_check is None  # noqa: SLF001
    assert coord._grid_control_user_initiated_toggle is None  # noqa: SLF001

    coord._dry_contact_settings_supported = True  # noqa: SLF001
    coord.client.dry_contacts_settings = None
    await coord.battery_runtime.async_refresh_dry_contact_settings()
    assert coord._dry_contact_settings_supported is None  # noqa: SLF001

    coord.client.storm_guard_profile = None
    await coord.battery_runtime.async_refresh_storm_guard_profile()

    coord._endpoint_family_should_run = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda family, force=False: family != "storm_alert"
    )
    coord.client.storm_guard_alert = AsyncMock(side_effect=AssertionError("unused"))
    await coord.battery_runtime.async_refresh_storm_alert()
    coord.client.storm_guard_alert.assert_not_awaited()

    coord._endpoint_family_should_run = lambda *args, **kwargs: True  # type: ignore[method-assign]  # noqa: SLF001
    coord.client.storm_guard_alert = None
    await coord.battery_runtime.async_refresh_storm_alert()


@pytest.mark.asyncio
async def test_battery_runtime_async_set_grid_connection_uses_runtime_grid_mode() -> (
    None
):
    coordinator = SimpleNamespace()
    runtime = BatteryRuntime(coordinator)
    runtime.async_set_grid_mode = AsyncMock()  # type: ignore[assignment]

    await runtime.async_set_grid_connection(True, otp="1234")

    runtime.async_set_grid_mode.assert_awaited_once_with("on_grid", "1234")
